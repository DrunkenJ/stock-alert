"""
백테스팅 엔진
- 과거 KIS 일봉 데이터로 스크리닝 재현
- 수급+기술 점수 기반 선정 (AI 제외, 비용 절감)
- 성과 지표 자동 계산
"""
import os
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger

from src.api.kis_client import KISClient
from src.analyzers.technical import TechnicalAnalyzer
from src.analyzers.supply_demand import SupplyDemandAnalyzer


class BacktestEngine:
    """간이 백테스팅 엔진"""

    def __init__(self, start_date: str, end_date: str, universe: list[str] = None):
        """
        start_date/end_date: YYYYMMDD
        universe: 백테스트 대상 티커 리스트 (미지정 시 DB 전체)
        """
        self.start_date = start_date
        self.end_date = end_date
        self.kis = KISClient()
        self.tech = TechnicalAnalyzer()
        self.sd = SupplyDemandAnalyzer()

        # 파라미터 (실전과 동일)
        self.tech_weight = 0.4
        self.supply_weight = 0.6
        self.min_score = 4.0
        self.stop_loss = 0.97
        self.take_profit = 1.06
        self.holding_days = 3
        self.max_hold_days = 7      # 분할 익절/트레일링이 붙어 실전(trade_simulator)과 동일
        self.final_picks = 5
        self._filter_stats = {}
        self._score_pass = 0

        if universe:
            self.universe = universe
        else:
            self.universe = self._load_universe()

        logger.info(f"백테스트 준비: {start_date}~{end_date}, 유니버스 {len(self.universe)}종목")

    def _load_universe(self) -> list[str]:
        """stock_db에서 전종목 티커 로드"""
        from src.utils.stock_db import get_db
        db = get_db()
        db.load()
        tickers = [t for t in db._ticker_to_name.keys() if t.isdigit() and len(t) == 6]
        tickers = [t for t in tickers if not t.startswith("1")]  # ETF 제외
        return tickers

    def run(self) -> dict:
        """백테스팅 실행"""
        logger.info("=" * 60)
        logger.info(f"백테스팅 시작: {self.start_date} ~ {self.end_date}")
        logger.info(f"유니버스: {len(self.universe)}종목 / 예상 시간: {len(self.universe) * 2 // 60}분")
        logger.info("=" * 60)

        logger.info("[1/3] 일봉 + 수급 데이터 수집 중...")
        price_data = self._fetch_all_data()
        logger.info(f"  → {len(price_data)}종목 데이터 수집 완료")

        logger.info("[2/3] 날짜별 스크리닝 재현...")
        trades = self._simulate_trades(price_data)
        logger.info(f"  → 총 {len(trades)}건 거래 시뮬레이션")

        logger.info("[3/3] 성과 분석...")
        metrics = self._calculate_metrics(trades)

        return {
            "period": f"{self.start_date} ~ {self.end_date}",
            "universe_size": len(self.universe),
            "trades": trades,
            "metrics": metrics,
        }

    def _fetch_all_data(self) -> dict:
        """전종목 일봉 데이터 + 수급 데이터 수집
        - 저장된 수급 히스토리 우선 사용 (정확한 당일 수급)
        - 없으면 현재 API 조회 (최근 30일치)
        """
        from src.utils.supply_collector import get_available_dates, get_supply_for_date

        # 사용 가능한 수급 히스토리 날짜 확인
        available_dates = get_available_dates()
        if available_dates:
            logger.info(f"  수급 히스토리 활용: {available_dates[0]} ~ {available_dates[-1]} ({len(available_dates)}일)")
        else:
            logger.info("  수급 히스토리 없음 → 현재 API 데이터 사용 (정확도 제한)")

        result = {}
        total = len(self.universe)

        for i, ticker in enumerate(self.universe):
            try:
                candles = self.kis.get_daily_ohlcv_long(ticker, days=300)
                if len(candles) < 60:
                    continue

                candle_by_date = {c["date"]: c for c in candles}

                # 수급 데이터: 히스토리 우선, 없으면 API
                investor_by_date = {}

                if available_dates:
                    # 저장된 히스토리에서 날짜별 수급 구성
                    for date in available_dates:
                        supply = get_supply_for_date(date)
                        if ticker in supply:
                            t_data = supply[ticker]
                            investor_by_date[date] = {
                                "date": date,
                                "foreign": t_data.get("foreign", 0),
                                "inst": t_data.get("inst", 0),
                                "indiv": t_data.get("indiv", 0),
                            }

                # 히스토리에 없는 날짜는 API로 보완
                if not investor_by_date:
                    investor = self.kis.get_investor_trend(ticker, days=30)
                    investor_by_date = {
                        d["date"]: d for d in investor.get("detail", [])
                    }
                    time.sleep(0.2)

                result[ticker] = {
                    "candles": candles,
                    "candle_by_date": candle_by_date,
                    "investor_by_date": investor_by_date,
                }

                if (i + 1) % 20 == 0:
                    logger.info(f"  진행: {i+1}/{total} ({(i+1)*100//total}%)")
                time.sleep(0.1)

            except Exception as e:
                logger.debug(f"  [{ticker}] 실패: {e}")

        return result

    def _simulate_trades(self, price_data: dict) -> list[dict]:
        """날짜별로 스크리닝 → 매수 → 청산 시뮬레이션"""
        start = datetime.strptime(self.start_date, "%Y%m%d")
        end = datetime.strptime(self.end_date, "%Y%m%d")

        all_dates = set()
        for data in price_data.values():
            for c in data["candles"]:
                all_dates.add(c["date"])
        trading_dates = sorted([d for d in all_dates
                                if start <= datetime.strptime(d, "%Y%m%d") <= end])

        trades = []

        # 라이브와 동일한 국면/브레드스 게이트를 적용한다.
        # 이게 없으면 백테스트는 약세장에도 매일 5종목씩 사들여
        # 실전보다 훨씬 공격적인 결과를 낸다.
        regime_timeline = self._build_regime_timeline(trading_dates)
        gated_days = {"regime": 0, "breadth": 0}
        base_picks = self.final_picks
        base_min   = self.min_score

        for idx, date in enumerate(trading_dates):
            candidates = []
            breadth_above = breadth_total = 0

            # 국면별 파라미터 (추천 종목 수 / 최소 점수 / 가중치)
            regime = regime_timeline.get(date)
            if regime:
                from src.analyzers.regime_classifier import regime_params
                rp = regime_params(regime, base_picks, base_min)
                self.final_picks   = rp["final_picks"]
                self.min_score     = rp["min_score"]
                self.tech_weight   = rp["tech_weight"]
                self.supply_weight = rp["supply_weight"]

            for ticker, data in price_data.items():
                past_candles = [c for c in data["candles"] if c["date"] <= date]
                if len(past_candles) < 20:
                    continue

                try:
                    tech_result = self.tech.analyze(past_candles[-60:])
                except Exception:
                    continue

                # 이 날짜까지의 수급 데이터
                investor_detail = [
                    d for d in data["investor_by_date"].values()
                    if d["date"] <= date
                ]
                investor_detail.sort(key=lambda x: x["date"], reverse=True)
                investor_detail = investor_detail[:20]

                if not investor_detail:
                    continue

                foreign_net = sum(d["foreign"] for d in investor_detail[:5])
                inst_net = sum(d["inst"] for d in investor_detail[:5])

                foreign_consec = 0
                for d in investor_detail:
                    if d["foreign"] > 0:
                        foreign_consec += 1
                    else:
                        break

                inst_consec = 0
                for d in investor_detail:
                    if d["inst"] > 0:
                        inst_consec += 1
                    else:
                        break

                # 간이 수급 점수
                supply_score = 0
                if foreign_net > 0:
                    supply_score += 3 if foreign_net > 500000 else 1
                if inst_net > 0:
                    supply_score += 3 if inst_net > 300000 else 1
                if foreign_net > 0 and inst_net > 0:
                    supply_score += 3
                if foreign_consec >= 3:
                    supply_score += 2
                if inst_consec >= 3:
                    supply_score += 2

                total = tech_result["score"] * self.tech_weight + supply_score * self.supply_weight

                # 브레드스는 하드필터 이전에 전수 집계 (유니버스 건강도)
                _ind = tech_result.get("indicators", {})
                if _ind.get("ma20"):
                    breadth_total += 1
                    if _ind.get("above_ma20"):
                        breadth_above += 1

                # 실전 screener 의 진입 과열 필터를 동일하게 적용
                # (이게 없으면 백테스트가 구 진입 기준을 재현해 검증이 무의미해진다)
                if not self._passes_entry_filters(_ind):
                    continue

                if total >= self.min_score:
                    self._score_pass += 1
                    cur_candle = data["candle_by_date"].get(date)
                    if cur_candle and cur_candle["close"] >= 500:
                        candidates.append({
                            "ticker": ticker,
                            "date": date,
                            "score": total,
                            "tech_score": tech_result["score"],
                            "supply_score": supply_score,
                            "atr_pct": tech_result.get("indicators", {}).get("atr_pct"),
                            "entry_price": cur_candle["close"],
                        })

            if not self._passes_breadth(breadth_above, breadth_total):
                gated_days["breadth"] += 1
                continue

            candidates = self._apply_vol_band(candidates)
            candidates.sort(key=lambda x: -x["score"])
            selected = candidates[:self.final_picks]

            for sel in selected:
                trade = self._simulate_single_trade(
                    sel, price_data[sel["ticker"]]["candle_by_date"], trading_dates, idx
                )
                if trade:
                    trades.append(trade)

            if (idx + 1) % 20 == 0:
                logger.info(f"  시뮬레이션: {idx+1}/{len(trading_dates)}일 ({len(trades)}건)")

        self.final_picks, self.min_score = base_picks, base_min
        if regime_timeline:
            from collections import Counter
            dist = Counter(r.get("regime", "?") for r in regime_timeline.values())
            logger.info(f"  국면 분포: {dict(dist)}")
        logger.info(f"  브레드스 미달로 건너뛴 날: {gated_days['breadth']}일 / {len(trading_dates)}일")
        _st = self._filter_stats
        _ev = _st.get("평가", 1)
        logger.info("  진입필터 탈락(중복포함): " + " ".join(
            f"{k}={v}({v*100//_ev}%)" for k, v in sorted(_st.items(), key=lambda x: -x[1])))
        # 점수 미달 집계
        logger.info(f"  점수 통과(필터통과 중): {self._score_pass}/{_st.get('통과',0)}")

        return trades

    # ── 국면 / 브레드스 게이트 ────────────────────────────
    def _build_regime_timeline(self, trading_dates: list) -> dict:
        """날짜별 시장 국면을 과거 데이터로 재현

        regime_classifier 는 코스피200 ETF(069500) 일봉만 쓰므로 소급 재현이 된다.
        VIX 는 과거값을 구할 수 없어 중립값(20.0)으로 고정한다 - 실전 대비
        국면 판정이 다소 관대해질 수 있다는 한계는 남는다.
        """
        from src.analyzers.regime_classifier import MarketRegimeClassifier

        clf = MarketRegimeClassifier()
        try:
            candles = self.kis.get_daily_ohlcv_long("069500", days=400)
        except Exception as e:
            logger.warning(f"국면 재현용 지수 데이터 실패: {e}")
            return {}
        if len(candles) < 80:
            logger.warning("국면 재현용 지수 데이터 부족 - 게이트 비활성")
            return {}

        timeline = {}
        for date in trading_dates:
            past = [c for c in candles if c["date"] <= date]
            if len(past) < 60:
                continue
            try:
                timeline[date] = clf._analyze(self._index_features(past), vix=20.0)
            except Exception:
                continue
        return timeline

    @staticmethod
    def _index_features(candles: list) -> dict:
        """regime_classifier._fetch_kospi_data 와 동일한 피처 계산 (특정 시점 기준)"""
        closes = [c["close"] for c in candles]
        highs  = [c["high"]  for c in candles]
        cur = closes[-1]

        ma5   = sum(closes[-5:]) / 5
        ma20  = sum(closes[-20:]) / 20
        ma60  = sum(closes[-60:]) / 60
        ma20_5ago = sum(closes[-25:-5]) / 20
        ma_slope = (ma20 - ma20_5ago) / ma20_5ago * 100

        trs = []
        for i in range(1, len(candles)):
            h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr20 = sum(trs[-20:]) / 20

        high_52w = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        return {
            "current": cur, "ma5": ma5, "ma20": ma20, "ma60": ma60,
            "ma_slope_pct": ma_slope,
            "vol_ratio_pct": atr20 / cur * 100,
            "drawdown_pct": (cur - high_52w) / high_52w * 100,
            "is_aligned": cur > ma5 > ma20 > ma60,
            "atr20": atr20,
        }

    def _passes_breadth(self, above: int, total: int) -> bool:
        """후보 풀의 20MA 상회 비율 게이트 (screener._check_breadth 와 동일)"""
        if total < 10:
            return True
        pct = above / total * 100
        return pct >= float(os.getenv("MIN_MARKET_BREADTH", "50"))

    def _passes_entry_filters(self, ind: dict) -> bool:
        """screener._analyze_single 의 하드필터와 동일 (env 값 공유)"""
        checks = [
            ("RSI",   ind.get("rsi"),         float(os.getenv("MAX_ENTRY_RSI", "70"))),
            ("거래량비", ind.get("vol_ratio"),   float(os.getenv("MAX_ENTRY_VOL_RATIO", "1.5"))),
            ("ATR",   ind.get("atr_pct"),     float(os.getenv("MAX_ENTRY_ATR_PCT", "20.0"))),
            ("이격도",  ind.get("disparity20"), float(os.getenv("MAX_ENTRY_DISPARITY", "10.0"))),
            ("20일상승", ind.get("ret20"),      float(os.getenv("MAX_ENTRY_RET20", "25.0"))),
        ]
        self._filter_stats["평가"] = self._filter_stats.get("평가", 0) + 1
        ok = True
        for name, v, lim in checks:
            if v is not None and v > lim:
                self._filter_stats[name] = self._filter_stats.get(name, 0) + 1
                ok = False
        if ok:
            self._filter_stats["통과"] = self._filter_stats.get("통과", 0) + 1
        return ok

    def _apply_vol_band(self, candidates: list[dict]) -> list[dict]:
        """변동성 국면별 ATR 상한 (screener._filter_by_volatility 와 동일)"""
        if not candidates or os.getenv("ENTRY_ATR_MODE", "band").lower() == "off":
            return candidates
        from src.utils.volatility_regime import apply_volatility_filter
        passed, _ = apply_volatility_filter(
            candidates, get_atr=lambda c: c.get("atr_pct"),
            final_picks=self.final_picks)
        return passed

    def _simulate_single_trade(self, candidate: dict, candle_by_date: dict,
                               trading_dates: list, start_idx: int) -> dict | None:
        """단일 종목 매수→청산 시뮬레이션"""
        entry_date = None
        entry_price = 0
        stop = target = 0

        # ── 진입: 익일 시가 ──────────────────────────────
        if start_idx + 1 >= len(trading_dates):
            return None
        entry_date = trading_dates[start_idx + 1]
        entry_candle = candle_by_date.get(entry_date)
        if not entry_candle:
            return None
        entry_price = entry_candle["open"]
        if entry_price <= 0:
            return None

        # ── 진입 시점의 손절/익절 (실전 entry_calculator 와 동일 규칙) ──
        # 예전에는 여기서 고정 -3%/+6% 를 썼다. 분할 익절·트레일링·본전 상향이
        # 전부 빠져 있어서 백테스트가 실제 청산 전략을 전혀 재현하지 못했고,
        # ATR 연동 모드 검증이 불가능했다.
        from src.utils.entry_calculator import _get_rr_by_score
        from src.utils.exit_policy import calc_stop_distance, simulate_exit

        atr = self._calc_atr(candle_by_date, trading_dates, start_idx)
        stop_mult, target_mult = _get_rr_by_score(candidate.get("score", 0))

        if atr > 0:
            stop_dist = calc_stop_distance(entry_price, atr, stop_mult)
            rr = target_mult / stop_mult if stop_mult else 1.5
            stop   = int(entry_price - stop_dist)
            target = int(entry_price + stop_dist * rr)
        else:
            stop   = int(entry_price * self.stop_loss)
            target = int(entry_price * self.take_profit)

        # ── 진입 다음날부터의 경로 ───────────────────────
        path = []
        for offset in range(2, len(trading_dates) - start_idx):
            c = candle_by_date.get(trading_dates[start_idx + offset])
            if c:
                path.append(c)
            if len(path) >= self.max_hold_days:
                break
        if not path:
            return None

        r = simulate_exit(entry_price, atr, stop, target, path,
                          max_hold_days=self.max_hold_days)

        exit_idx = min(start_idx + 1 + r["holding_days"], len(trading_dates) - 1)
        return {
            "ticker": candidate["ticker"],
            "score": candidate["score"],
            "entry_date": entry_date,
            "entry_price": entry_price,
            "exit_date": trading_dates[exit_idx],
            "exit_reason": r["close_reason"],
            "return_pct": r["realized_pct"],
            "max_favorable_pct": r["max_favorable_pct"],
            "atr_pct": round(atr / entry_price * 100, 2) if entry_price else 0,
            "holding_days": r["holding_days"],
        }

    def _calc_atr(self, candle_by_date: dict, trading_dates: list,
                  start_idx: int, period: int = 14) -> float:
        """진입 직전까지의 캔들로 ATR 계산 (미래 데이터 사용 금지)"""
        window = []
        for i in range(max(0, start_idx - period), start_idx + 1):
            c = candle_by_date.get(trading_dates[i])
            if c:
                window.append(c)
        if len(window) < 2:
            return 0.0

        trs = []
        for prev, cur in zip(window, window[1:]):
            trs.append(max(cur["high"] - cur["low"],
                           abs(cur["high"] - prev["close"]),
                           abs(cur["low"] - prev["close"])))
        return sum(trs) / len(trs) if trs else 0.0

    def _calculate_metrics(self, trades: list[dict]) -> dict:
        """성과 지표 계산"""
        if not trades:
            return {"error": "거래 없음"}

        import statistics
        returns = [t["return_pct"] for t in trades]

        total_return = sum(returns)
        avg_return = statistics.mean(returns)
        win_trades = [r for r in returns if r > 0]
        loss_trades = [r for r in returns if r <= 0]
        win_rate = len(win_trades) / len(returns) * 100
        avg_win = statistics.mean(win_trades) if win_trades else 0
        avg_loss = statistics.mean(loss_trades) if loss_trades else 0

        profit_factor = abs(sum(win_trades) / sum(loss_trades)) if loss_trades and sum(loss_trades) != 0 else 0

        if len(returns) > 1:
            std = statistics.stdev(returns)
            sharpe = (avg_return / std) * (252 ** 0.5) if std > 0 else 0
        else:
            sharpe = 0

        # MDD (누적 수익률 기준)
        cumulative = []
        total = 0
        for r in returns:
            total += r
            cumulative.append(total)
        peak = cumulative[0]
        max_dd = 0
        for v in cumulative:
            peak = max(peak, v)
            dd = v - peak
            max_dd = min(max_dd, dd)

        exit_reasons = {}
        for t in trades:
            r = t["exit_reason"]
            exit_reasons[r] = exit_reasons.get(r, 0) + 1

        return {
            "total_trades": len(trades),
            "win_trades": len(win_trades),
            "loss_trades": len(loss_trades),
            "win_rate_pct": round(win_rate, 2),
            "total_return_pct": round(total_return, 2),
            "avg_return_pct": round(avg_return, 2),
            "avg_win_pct": round(avg_win, 2),
            "avg_loss_pct": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "exit_reasons": exit_reasons,
        }

    def save_report(self, result: dict, filename: str = None):
        """백테스트 결과 저장"""
        if not filename:
            filename = f"backtest_{self.start_date}_{self.end_date}.json"
        path = Path("data") / filename
        path.parent.mkdir(exist_ok=True)
        with open(path, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        logger.info(f"백테스트 리포트 저장: {path}")
