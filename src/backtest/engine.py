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
        self.final_picks = 5

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
                candles = self.kis.get_daily_ohlcv(ticker, days=300)
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

        for idx, date in enumerate(trading_dates):
            candidates = []

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

                if total >= self.min_score:
                    cur_candle = data["candle_by_date"].get(date)
                    if cur_candle and cur_candle["close"] >= 500:
                        candidates.append({
                            "ticker": ticker,
                            "date": date,
                            "score": total,
                            "tech_score": tech_result["score"],
                            "supply_score": supply_score,
                            "entry_price": cur_candle["close"],
                        })

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

        return trades

    def _simulate_single_trade(self, candidate: dict, candle_by_date: dict,
                               trading_dates: list, start_idx: int) -> dict | None:
        """단일 종목 매수→청산 시뮬레이션"""
        entry_date = None
        entry_price = 0
        stop = target = 0

        for offset in range(1, self.holding_days + 2):
            if start_idx + offset >= len(trading_dates):
                return None
            date = trading_dates[start_idx + offset]
            candle = candle_by_date.get(date)
            if not candle:
                continue

            if entry_date is None:
                # 익일 시가 매수
                entry_date = date
                entry_price = candle["open"]
                stop = entry_price * self.stop_loss
                target = entry_price * self.take_profit
                continue

            # 손절/익절 체크 (고가/저가 기준)
            if candle["low"] <= stop:
                exit_price = stop
                exit_reason = "stop_loss"
            elif candle["high"] >= target:
                exit_price = target
                exit_reason = "take_profit"
            else:
                continue

            return {
                "ticker": candidate["ticker"],
                "score": candidate["score"],
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_date": date,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "return_pct": (exit_price - entry_price) / entry_price * 100,
                "holding_days": offset - 1,
            }

        # holding_days 초과 → 종가 청산
        if entry_date:
            last_idx = min(start_idx + self.holding_days + 1, len(trading_dates) - 1)
            last_date = trading_dates[last_idx]
            last_candle = candle_by_date.get(last_date)
            if last_candle:
                return {
                    "ticker": candidate["ticker"],
                    "score": candidate["score"],
                    "entry_date": entry_date,
                    "entry_price": entry_price,
                    "exit_date": last_date,
                    "exit_price": last_candle["close"],
                    "exit_reason": "time_exit",
                    "return_pct": (last_candle["close"] - entry_price) / entry_price * 100,
                    "holding_days": self.holding_days,
                }
        return None

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
