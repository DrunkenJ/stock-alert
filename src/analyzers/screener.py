def _calc_score_confidence(base: float, *adjs) -> str:
    """점수 신뢰도 평가"""
    total_adj = sum(abs(a) for a in adjs)
    if total_adj <= 0.3:
        return "높음 ⭐⭐⭐"
    elif total_adj <= 1.0:
        return "보통 ⭐⭐"
    else:
        return "낮음 ⭐"


"""
종목 스크리닝 엔진
- 거래량 상위 → 수급 분석 → 기술적 분석 → AI 평가 → 최종 랭킹
"""
import os
import time
from loguru import logger
from dotenv import load_dotenv

from src.api.kis_client import KISClient
from src.analyzers.technical import TechnicalAnalyzer
from src.analyzers.supply_demand import SupplyDemandAnalyzer
from src.analyzers.ai_evaluator import AIEvaluator

load_dotenv()


class StockScreener:
    """종목 스크리닝 파이프라인"""

    def __init__(self):
        self.kis = KISClient()
        self.tech = TechnicalAnalyzer()
        self.sd = SupplyDemandAnalyzer()
        self.ai = AIEvaluator()

        self.top_n = int(os.getenv("TOP_N_STOCKS", "50"))
        self.final_picks = int(os.getenv("FINAL_PICKS", "5"))
        self.min_volume = int(os.getenv("MIN_VOLUME", "100000"))
        self.min_score = float(os.getenv("MIN_FINAL_SCORE", "4.0"))
        self.tech_weight = 0.4
        self.supply_weight = 0.6

    def run(self) -> list[dict]:
        """전체 스크리닝 파이프라인 실행"""
        logger.info("=" * 60)
        logger.info("종목 스크리닝 시작")
        logger.info("=" * 60)

        # 시장 국면 확인 + 전략 조정
        regime = self._get_regime()
        self._apply_regime_strategy(regime)
        self._current_regime = regime  # 분석 중 참조용

        # 학습된 규칙 적용 (실패 패턴 반영)
        self._apply_learned_rules()

        # Step 1: 거래량 상위 종목 수집
        logger.info("[1/4] 후보 종목 수집 중...")
        candidates = self._collect_candidates()
        logger.info(f"  → 후보 {len(candidates)}개 수집")

        if not candidates:
            logger.warning("후보 종목 없음 - 스크리닝 중단")
            return []

        # Step 2: 각 종목 상세 분석
        logger.info("[2/4] 종목별 상세 분석 중...")
        analyzed = []
        self._breadth_total = 0
        self._breadth_above = 0
        for i, ticker in enumerate(candidates[:self.top_n]):
            try:
                stock = self._analyze_single(ticker)
                if stock:
                    analyzed.append(stock)
                logger.debug(f"  [{i+1}/{min(len(candidates), self.top_n)}] {ticker} 분석 완료")
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"  [{ticker}] 분석 실패: {e}")

        logger.info(f"  → 분석 완료: {len(analyzed)}개")

        # ── 시장 브레드스 게이트 ──────────────────────────
        # 국면분류기는 지수를 보지만 실제 매매 대상은 개별 종목 풀이다.
        # 79건 기간 중 67건이 'trending_up' 판정이었으나 유니버스는 20일 -15%였다.
        # → 후보 풀 자체의 건강도(20MA 위 비율)로 직접 게이트를 건다.
        if not self._check_breadth():
            return []

        # ── 변동성 국면별 ATR 컷 ──────────────────────────
        analyzed = self._filter_by_volatility(analyzed)

        # Step 3: 점수 기반 1차 필터
        logger.info(f"[3/4] 1차 점수 필터링... ({len(analyzed)}개 → 상위 {self.final_picks * 3}개)")
        analyzed.sort(key=lambda x: x["total_score"], reverse=True)
        top_candidates = analyzed[:self.final_picks * 3]

        # Step 4: AI 최종 평가
        logger.info(f"[4/4] AI 최종 평가 중... ({len(top_candidates)}개)")
        final = []
        for stock in top_candidates:
            try:
                ai_result = self.ai.evaluate_stock(stock)
                stock["ai_eval"] = ai_result
                stock["final_score"] = stock["total_score"] * 0.6 + ai_result["ai_score"] * 0.4

                if ai_result.get("recommendation") in ["강력매수", "매수"]:
                    final.append(stock)
            except Exception as e:
                logger.warning(f"AI 평가 실패 ({stock.get('name','')}): {e}")

        final.sort(key=lambda x: x["final_score"], reverse=True)

        # 최소 점수 기준 적용 (AI 반영 전·후 둘 다)
        qualified = self._apply_score_floor(final)

        # ── 섹터 다변화 필터 ───────────────────────────────
        try:
            from src.analyzers.sector_news import filter_by_sector_diversity
            max_per_sector = int(os.getenv("MAX_PER_SECTOR", "2"))
            pre_filtered = filter_by_sector_diversity(
                qualified[:self.final_picks * 3], max_per_sector
            )
        except Exception as e:
            logger.warning(f"섹터 필터 오류: {e}")
            pre_filtered = qualified

        # ── 뉴스 감성 필터 ────────────────────────────────
        try:
            from src.analyzers.sector_news import apply_news_filter
            pre_filtered = apply_news_filter(
                pre_filtered[:self.final_picks * 2],
                max_news_per_run=self.final_picks * 2,
            )
        except Exception as e:
            logger.warning(f"뉴스 필터 오류: {e}")

        # ── 갭 과열 제외 ────────────────────────────────
        # entry_calculator 는 갭 7%+ 를 "매수 보류(reject)"로 판정하면서도
        # 지정가 기준을 전일종가로 바꿀 뿐 추천에서 빼지는 않았다. 판정을 읽는
        # 코드가 어디에도 없어서 사실상 효력이 없었고, 그 사이 갭 이전 가격에
        # 산 것처럼 기록돼 성과가 부풀었다. 여기서 실제로 걷어낸다.
        # 슬라이스보다 앞에 두어, 보류 종목이 추천 자리를 잡아먹지 않게 한다.
        gap_rejected = []
        try:
            from src.utils.entry_calculator import gap_status_of
            kept = []
            for pick in pre_filtered:
                status, gap_pct = gap_status_of(pick)
                if status == "reject":
                    pick["_gap_pct"] = gap_pct
                    gap_rejected.append(pick)
                    logger.info(f"  [{pick['ticker']}] {pick['name']} "
                                f"시가 갭 +{gap_pct:.1f}% - 매수 보류")
                else:
                    kept.append(pick)
            pre_filtered = kept
        except Exception as e:
            logger.warning(f"갭 과열 판정 오류: {e}")

        # ── 시장 비중 조절 (코스피/코스닥) ─────────────────
        # 뉴스·갭 필터가 뺄 만큼 뺀 다음에 고른다. 예전처럼 앞에서 final_picks 개로
        # 잘라두면, 뒤 필터가 한 종목을 뺐을 때 그 자리를 채울 후보가 남지 않는다.
        pre_filtered = self._apply_market_balance(pre_filtered)

        result = pre_filtered[:self.final_picks]

        # ── 추적 종목 자동 등록 ──────────────────────────
        # ① 추천 초과 종목 (6위 이하, 점수 높은 것)
        # ② 갭 과열로 보류된 종목 (눌림목 오면 재진입 후보)
        try:
            from src.utils.watchlist import get_watchlist_manager
            wm = get_watchlist_manager()
            # 추천 초과 종목 (final_picks 이후 ~ final_picks+3)
            overflow = pre_filtered[self.final_picks:self.final_picks + 3]
            for pick in overflow:
                wm.add(
                    ticker=pick["ticker"],
                    name=pick["name"],
                    score=pick.get("final_score", 0),
                    entry_price=pick.get("price", 0),
                    reason=f"추천 초과 (점수 {pick.get('final_score',0):.1f})"
                )
            for pick in gap_rejected:
                wm.add(
                    ticker=pick["ticker"],
                    name=pick["name"],
                    score=pick.get("final_score", 0),
                    entry_price=pick.get("price", 0),
                    reason=f"갭 +{pick.get('_gap_pct', 0):.1f}% 보류 (눌림목 대기)"
                )
        except Exception as e:
            logger.debug(f"추적 자동 등록 오류: {e}")

        if gap_rejected:
            logger.info(f"갭 과열 보류 {len(gap_rejected)}종목: "
                        f"{[p['name'] for p in gap_rejected]} → 추적 등록")

        if not result:
            logger.warning(f"최소 점수({self.min_score}) 충족 종목 없음 - 알림 스킵")
            return result

        # 포지션 사이징 적용
        try:
            from src.utils.position_sizer import calculate_position_sizes
            try:
                from src.utils.trade_simulator import get_simulator
                open_pct = get_simulator().open_exposure_pct()
            except Exception as e:
                logger.warning(f"보유 비중 조회 실패 - 보유분 0% 로 계산: {e}")
                open_pct = 0.0
            result = calculate_position_sizes(result, open_exposure_pct=open_pct)
        except Exception as e:
            logger.warning(f"포지션 사이징 오류: {e}")

        # 매수가 전략 계산 (ATR 포함)
        try:
            from src.utils.entry_calculator import calculate_entries
            result = calculate_entries(result)
        except Exception as e:
            logger.warning(f"매수가 계산 오류: {e}")

        # _candles 제거 (Discord 전송 불필요 데이터)
        for s in result:
            s.pop("_candles", None)

        logger.info(f"최종 선정 ({len(result)}개): {[s['name'] for s in result]}")
        return result

    def _register_names(self, stocks: list[dict]) -> int:
        """랭킹 API 응답의 종목명을 stock_db에 즉시 저장"""
        try:
            from src.utils.stock_db import get_db
            db = get_db()
            if not db._loaded:
                db.load()
            added = 0
            for s in stocks:
                ticker = s.get("ticker", "")
                name = s.get("name", "")
                if ticker and name and ticker not in db._ticker_to_name:
                    db._db[name] = ticker
                    db._ticker_to_name[ticker] = name
                    added += 1
            if added > 0:
                db._save_to_file()
                logger.info(f"  종목 DB 보완: {added}개 추가")
            return added
        except Exception as e:
            logger.debug(f"DB 보완 실패: {e}")
            return 0

    def _collect_candidates(self) -> list[str]:
        """후보 종목 수집 + 종목명 DB에 즉시 등록

        79건 실거래 검증 결과, 후보를 '거래량 상위'에서만 뽑는 것이 손실의
        주원인이었다. 픽 시점에 이미 20일간 중앙값 +19% 상승(33%는 +30% 이상),
        ATR이 주가의 6%를 넘는 종목이 91%로, 급등 소진 구간을 매수하는 구조였다.
        → 시가총액 상위 풀을 1차 소스로 쓰고, 거래량 상위는 보조 소스로 제한한다.
        """
        mode = os.getenv("UNIVERSE_MODE", "mixed").lower()
        vol_ratio_env = float(os.getenv("VOLUME_UNIVERSE_RATIO", "0.3"))
        tickers: list[str] = []
        seen: set[str] = set()

        def add_all(stocks: list[dict], limit: int) -> int:
            n = 0
            for s in stocks:
                if n >= limit:
                    break
                t = s.get("ticker", "")
                if t and t not in seen:
                    seen.add(t)
                    tickers.append(t)
                    n += 1
            return n

        # ── 1차 소스: 시가총액 상위 (안정적 유니버스) ──────
        if mode in ("mixed", "marketcap"):
            cap_quota = self.top_n if mode == "marketcap" else \
                int(self.top_n * (1 - vol_ratio_env))
            got = 0
            # 랭킹 API는 호출당 30건까지만 반환 → 코스피/코스닥을 각각 조회
            for market_code in (self.kis.MCAP_KOSDAQ, self.kis.MCAP_KOSPI):
                if got >= cap_quota:
                    break
                try:
                    cap_stocks = self.kis.get_market_cap_ranking(
                        market=market_code, top_n=cap_quota
                    )
                    if not cap_stocks:
                        continue
                    self._register_names(cap_stocks)
                    got += add_all(cap_stocks, cap_quota - got)
                except Exception as e:
                    logger.debug(f"시총 상위 조회 실패({market_code}): {e}")
            if got:
                logger.info(f"  시총 상위: {got}개")
            else:
                logger.warning("  시총 상위 조회 결과 없음 - 거래량 상위로 대체")

        # ── 2차 소스: 거래량 상위 (보조) ──────────────────
        if mode in ("mixed", "volume") or not tickers:
            remain = max(0, self.top_n - len(tickers))
            if remain:
                try:
                    vol_stocks = self.kis.get_volume_ranking(market="J", top_n=remain * 2)
                    self._register_names(vol_stocks)
                    got = add_all(vol_stocks, remain)
                    logger.info(f"  거래량 상위: {got}개")
                except Exception as e:
                    logger.error(f"거래량 조회 실패: {e}")

        return tickers

    def _basic_filter(self, tickers: list[str]) -> list[str]:
        return tickers

    def _analyze_single(self, ticker: str) -> dict | None:
        """단일 종목 전체 분석"""
        price_data = self.kis.get_stock_price(ticker)

        # ── ETF/ETN/스팩 제외 ─────────────────────────────
        if not ticker.isdigit() or len(ticker) != 6:
            return None
        if ticker.startswith("1"):
            return None
        name = price_data.get("name", "")
        etf_keywords = [
            "KODEX", "TIGER", "ARIRANG", "KINDEX", "HANARO", "RISE", "ACE", "KOSEF",
            "FOCUS", "TREX", "SOL", "히어로", "마이다스", "파워",
            "스팩", "SPAC", "ETF", "ETN", "레버리지", "인버스",
        ]
        if any(x in name for x in etf_keywords):
            return None

        # ── 관리종목 / 시장경고 제외 ──────────────────────
        # KIS 응답에 이미 실려 오는 플래그인데 여태 아무도 읽지 않았다.
        # 거래정지·상장폐지로 갈 수 있는 종목이라 가격·시총 필터보다 앞에 둔다.
        if price_data.get("is_managed"):
            logger.debug(f"  [{ticker}] {name} 관리종목 - 제외")
            return None
        warn = str(price_data.get("market_warn", "00"))
        if warn != "00":
            label = {"01": "투자주의", "02": "투자경고", "03": "투자위험"}.get(warn, warn)
            logger.debug(f"  [{ticker}] {name} {label} 지정 - 제외")
            return None

        # ── 주가 필터 (저가주 제외) ───────────────────────
        min_price = int(os.getenv("MIN_STOCK_PRICE", "3000"))
        if price_data["price"] < min_price:
            return None

        # ── 시가총액 필터 (소형주 제외) ──────────────────
        min_cap = int(os.getenv("MIN_MARKET_CAP", "50000000000"))  # 기본 500억
        market_cap = price_data.get("market_cap", 0)
        if 0 < market_cap < min_cap:
            return None

        # 시가 조회 (당일 시가 - 매수 참고가)
        open_price = price_data.get("open_price", price_data["price"])

        # ── 호가 스프레드 필터 (슬리피지 방지) ─────────
        spread_pct = self._calc_spread(price_data)
        max_spread = float(os.getenv("MAX_SPREAD_PCT", "0.5"))
        if spread_pct > max_spread:
            logger.debug(f"  [{ticker}] 호가 스프레드 {spread_pct:.2f}% 초과 - 제외")
            return None

        candles = self.kis.get_daily_ohlcv(ticker, days=300)
        if len(candles) < 20:
            return None

        # ── 52주 고점 위치 점수 보정 ───────────────────
        high_52w_adj = self._calc_52w_position_adj(candles, price_data["price"])

        # ── 실적 발표 임박 체크 ────────────────────────
        earnings_warning = self._check_earnings_imminent(ticker)
        if earnings_warning.get("skip"):
            logger.debug(f"  [{ticker}] {earnings_warning.get('reason')} - 제외")
            return None

        investor = self.kis.get_investor_trend(ticker, days=20)

        # 상대강도(RS) 계산용 지수 수익률 - 조회 실패 시 None (RS 생략)
        market_name = price_data.get("market") or self._detect_market(ticker)
        try:
            from src.utils.index_data import get_market_ret20
            market_ret20 = get_market_ret20(market_name)
        except Exception:
            market_ret20 = None

        tech_result = self.tech.analyze(candles, market_ret20=market_ret20)
        _ind = tech_result.get("indicators", {})

        # ── 시장 브레드스 집계 (필터 적용 전 전수 집계) ──
        # 하드필터로 걸러지기 전에 세어야 유니버스 전체의 건강도를 반영한다
        if _ind.get("ma20"):
            self._breadth_total = getattr(self, "_breadth_total", 0) + 1
            if _ind.get("above_ma20"):
                self._breadth_above = getattr(self, "_breadth_above", 0) + 1

        # ── 과열 추격 제외 (79건 백데이터 검증 필터) ────
        # 픽의 3일 알파가 -5.18%p (다음날 상승확률 28%). 원인은 급등 소진 구간 매수.
        # 아래 필터 조합으로 알파 -5.18%p → -0.89%p (82% 개선) 확인.
        max_rsi = float(os.getenv("MAX_ENTRY_RSI", "70"))
        _rsi = _ind.get("rsi")
        if _rsi is not None and _rsi > max_rsi:
            logger.debug(f"  [{ticker}] RSI {_rsi:.1f} 과매수 - 추격 제외")
            return None

        # 거래량비: <1.0배 승률 68%/+0.35% vs >2.0배 승률 44%/-4.17%
        max_vol_ratio = float(os.getenv("MAX_ENTRY_VOL_RATIO", "1.5"))
        _vr = _ind.get("vol_ratio")
        if _vr is not None and _vr > max_vol_ratio:
            logger.debug(f"  [{ticker}] 거래량 평균 대비 {_vr:.1f}배 급증 - 추격 제외")
            return None

        # ATR 절대 상한은 '명백한 이상치'만 걸러내는 백스톱으로만 쓴다.
        # 실질적인 변동성 컷은 run()에서 후보 풀 대비 상대 백분위로 적용한다
        # (절대값 컷은 시장 변동성 국면이 바뀌면 유니버스를 통째로 비운다).
        max_atr_pct = float(os.getenv("MAX_ENTRY_ATR_PCT", "20.0"))
        _atr_pct = _ind.get("atr_pct")
        if _atr_pct is not None and _atr_pct > max_atr_pct:
            logger.debug(f"  [{ticker}] ATR {_atr_pct:.1f}% 초고변동성 - 제외")
            return None

        # 20MA 이격도 상한: 이격 10~20% 구간 3일 -12.2%, 20%+ -9.3%
        max_disparity = float(os.getenv("MAX_ENTRY_DISPARITY", "10.0"))
        _disp = _ind.get("disparity20")
        if _disp is not None and _disp > max_disparity:
            logger.debug(f"  [{ticker}] 20MA 이격 +{_disp:.1f}% 과열 - 제외")
            return None

        # 20일 상승률 상한: 급등 후 추격 매수 차단
        max_ret20 = float(os.getenv("MAX_ENTRY_RET20", "25.0"))
        _r20 = _ind.get("ret20")
        if _r20 is not None and _r20 > max_ret20:
            logger.debug(f"  [{ticker}] 20일 +{_r20:.0f}% 급등 후 - 추격 제외")
            return None

        supply_result = self.sd.analyze(investor, price_data)

        # 52주 고점 위치에 따른 점수 보정 적용
        total_score = (
            tech_result["score"] * self.tech_weight
            + supply_result["score"] * self.supply_weight
            + high_52w_adj
        )

        # [약점 9번] 수급 패턴 점수 반영
        supply_pattern = supply_result.get("supply_pattern", {})
        pattern_adj = supply_pattern.get("adj", 0)
        total_score += pattern_adj

        # [약점 2번] 3일 룰: 3일 연속 상승 종목 감점
        consecutive_up_adj = self._calc_consecutive_up_adj(candles)
        total_score += consecutive_up_adj

        # [약점 5번] 국면별 종목 유형 보정
        current_regime = getattr(self, "_current_regime", {})
        regime_adj = self._calc_regime_type_adj(
            price_data.get("name", ""), current_regime
        )
        total_score += regime_adj

        # [2단계] 학습된 섹터 가중치 적용
        learned_adj = self._apply_learned_sector_adj(price_data.get("name", ""))
        total_score += learned_adj

        return {
            "ticker": ticker,
            "name": price_data["name"],
            "price": price_data["price"],
            "open_price": price_data.get("open_price", price_data["price"]),
            "change_rate": price_data["change_rate"],
            "volume": price_data["volume"],
            "market_cap": price_data["market_cap"],
            "market": price_data.get("market") or self._detect_market(ticker),
            # KRX 업종. sector_news.classify_sector 가 키워드 매칭보다 우선 사용한다.
            "sector_krx": price_data.get("sector_krx", ""),
            "tech_score": tech_result["score"],
            "tech_signals": tech_result["signals"],
            "supply_score": supply_result["score"],
            "supply_signals": supply_result["signals"],
            "supply_summary": supply_result["summary"],
            "supply_pattern": supply_result.get("supply_pattern", {}),
            "indicators": tech_result.get("indicators", {}),
            "total_score": total_score,
            # [약점 10번] 점수 신뢰도 (보정값 합계로 안정성 판단)
            "score_confidence": _calc_score_confidence(
                total_score, high_52w_adj, consecutive_up_adj,
                pattern_adj, regime_adj
            ),
            "_candles": candles,
        }

    def _filter_by_volatility(self, analyzed: list[dict]) -> list[dict]:
        """시장 변동성 국면을 판정하고 국면별 ATR 상한을 적용

        절대 임계값 고정 방식은 국면이 바뀌면 무너진다 (5.0% 상한이
        2026-08 시장에서 후보 50개 중 2개만 남겨 5일 연속 추천 0개).
        상세 근거는 src/utils/volatility_regime.py 참조.
        """
        if not analyzed or os.getenv("ENTRY_ATR_MODE", "band").lower() == "off":
            return analyzed

        from src.utils.volatility_regime import apply_volatility_filter

        passed, vol = apply_volatility_filter(
            analyzed,
            get_atr=lambda s: s.get("indicators", {}).get("atr_pct"),
            final_picks=self.final_picks,
        )
        self._vol_regime = vol
        return passed

    def _apply_score_floor(self, final: list[dict]) -> list[dict]:
        """최소 점수 하한 - AI 반영 후(final_score)와 반영 전(total_score) 둘 다

        백테스트는 이 하한을 AI 반영 전 점수에 건다. 라이브는 AI 가 섞인
        final_score 에만 걸어서, 백테스트가 한 번도 사지 않은 저점수 종목을
        AI 점수로 끌어올려 사고 있었다(2026-09 환산 3.8~5.0 종목들).
        두 탈락 사유 모두 로그를 남긴다. 예전에는 final_score 미달이 조용히 빠져서
        AI 가 '매수'를 준 종목이 왜 사라졌는지 알 수 없었다.
        APPLY_PRE_AI_FLOOR=0 이면 예전 동작(final_score 만).
        """
        pre_ai_floor = os.getenv("APPLY_PRE_AI_FLOOR", "1") != "0"
        qualified = []
        for s in final:
            tag = f"[{s.get('ticker', '')}] {s.get('name', '')}"
            if s["final_score"] < self.min_score:
                logger.info(f"  {tag} 최종 점수 {s['final_score']:.2f} < {self.min_score:.1f} - 제외")
                continue
            if pre_ai_floor and s.get("total_score", 0) < self.min_score:
                logger.info(f"  {tag} AI 전 점수 {s.get('total_score', 0):.1f} < {self.min_score:.1f} - 제외")
                continue
            qualified.append(s)
        return qualified

    def _check_breadth(self) -> bool:
        """시장 브레드스(20MA 상회 비율)로 매매 가능 여부 판단

        비율이 임계치 미만이면 개별 종목 점수와 무관하게 당일 픽을 중단한다.
        (하락장에서 롱온리 추격 매수를 막는 최종 안전장치)

        기본은 전 유니버스 기준(전일 종가, 20:05 산출)이다. 예전에는 랭킹으로 뽑힌
        후보 34~37개만 셌는데, 원래 강한 종목들이라 50~85% 가 나왔다(같은 기간
        전 유니버스 12~28%). 임계값 50 은 전 유니버스 기준 백테스트에서 고른 값이라
        라이브 게이트가 사실상 거의 막지 않고 있었다.
        BREADTH_POPULATION=candidates 면 예전 방식.
        """
        min_breadth = float(os.getenv("MIN_MARKET_BREADTH", "50"))
        population = os.getenv("BREADTH_POPULATION", "universe").lower()

        ub = None
        if population == "universe":
            from src.utils.market_breadth import load_universe_breadth
            ub = load_universe_breadth()
            if not ub:
                logger.warning("전 유니버스 브레드스 없음/오래됨 - 후보 풀 기준으로 대체")

        if ub:
            breadth, above, total = ub["pct"], ub["above"], ub["total"]
            basis = f"전 유니버스, {ub['date']} 종가"
        else:
            total = getattr(self, "_breadth_total", 0)
            above = getattr(self, "_breadth_above", 0)
            if total < 10:
                logger.debug(f"브레드스 표본 부족({total}개) - 게이트 미적용")
                return True
            breadth = above / total * 100
            basis = "후보 풀"

        self._breadth_pct = breadth
        if breadth < min_breadth:
            logger.warning(
                f"시장 브레드스 {breadth:.0f}% ({basis}, 기준 {min_breadth:.0f}%) - "
                f"{total}개 중 20MA 위 {above}개. 당일 추천 중단"
            )
            return False

        logger.info(f"  시장 브레드스: {breadth:.0f}% ({above}/{total}, {basis}) - 통과")
        return True

    def _get_regime(self) -> dict:
        """현재 시장 국면 조회 (캐시 우선)"""
        try:
            from src.analyzers.regime_classifier import MarketRegimeClassifier
            classifier = MarketRegimeClassifier()
            return classifier.classify(use_cache=True)
        except Exception as e:
            logger.debug(f"국면 조회 실패: {e}")
            return {"regime": "sideways", "picks_multiplier": 1.0,
                    "position_adj": 1.0, "regime_label": "판단 불가",
                    "strategy": "기본 전략"}

    def _apply_regime_strategy(self, regime: dict):
        """국면에 따라 스크리너 파라미터 조정 (매핑은 regime_classifier 단일 정의)"""
        from src.analyzers.regime_classifier import regime_params

        p = regime_params(
            regime,
            base_picks=int(os.getenv("FINAL_PICKS", "5")),
            base_min=float(os.getenv("MIN_FINAL_SCORE", "4.0")),
        )
        self.final_picks   = p["final_picks"]
        self.min_score     = p["min_score"]
        self.tech_weight   = p["tech_weight"]
        self.supply_weight = p["supply_weight"]

        logger.info(
            f"시장 국면 적용: {regime.get('regime_label','')} → "
            f"추천종목={self.final_picks}개, 최소점수={self.min_score:.1f}, "
            f"전략={regime.get('strategy','')}"
        )

    def _calc_consecutive_up_adj(self, candles: list) -> float:
        """
        [약점 2번] 3일 룰 감점
        3일 연속 상승 → -0.5점 (과열 신호)
        5일 연속 상승 → -1.5점 (강한 과열)
        1~2일 조정 후 반등 → +0.3점 (눌림목 매수)
        """
        if len(candles) < 6:
            return 0.0

        recent = candles[-6:]
        up_days = 0
        down_days = 0

        for i in range(1, len(recent)):
            if recent[i]["close"] > recent[i-1]["close"]:
                if down_days == 0:
                    up_days += 1
                else:
                    break
            else:
                down_days += 1

        if up_days >= 5:
            return -1.5
        elif up_days >= 3:
            return -0.5
        elif down_days in [1, 2] and recent[-1]["close"] > recent[-2]["close"]:
            return +0.3  # 1~2일 조정 후 반등
        return 0.0

    def _calc_regime_type_adj(self, name: str, regime: dict) -> float:
        """
        [약점 5번] 국면별 종목 유형 보정
        - 선호 섹터: +0.5점
        - 회피 섹터: -1.0점
        """
        if not regime or not name:
            return 0.0

        preferred = regime.get("preferred_types", [])
        avoid     = regime.get("avoid_types", [])

        from src.analyzers.sector_news import SECTOR_KEYWORDS
        stock_sector = "기타"
        for sector, keywords in SECTOR_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                stock_sector = sector
                break

        if "전종목" in avoid:
            return -2.0
        if stock_sector in avoid or any(a in name for a in avoid):
            return -1.0
        if stock_sector in preferred or any(p in name for p in preferred):
            return +0.5
        return 0.0

    def _calc_spread(self, price_data: dict) -> float:
        """호가 스프레드 비율 계산 (없으면 시총 기반 추정)"""
        # 호가 정보가 있으면 직접 계산
        ask = price_data.get("ask", 0)
        bid = price_data.get("bid", 0)
        price = price_data.get("price", 0)
        if ask > 0 and bid > 0 and price > 0:
            return (ask - bid) / price * 100

        # 호가 없으면 시총으로 추정 (소형주일수록 스프레드 큼)
        cap = price_data.get("market_cap", 0)
        if cap > 1_000_000_000_000:    # 1조 이상
            return 0.05
        elif cap > 500_000_000_000:    # 5천억 이상
            return 0.15
        elif cap > 100_000_000_000:    # 1천억 이상
            return 0.30
        elif cap > 50_000_000_000:     # 500억 이상
            return 0.45
        else:
            return 0.80                # 시총 작으면 스프레드 큼

    def _calc_52w_position_adj(self, candles: list, current: int) -> float:
        """
        52주 고점 위치에 따른 점수 보정
        - 80~95% 구간: +0.5점 (눌림목 매수 우호)
        - 95% 이상: -0.5점 (고점 매수 위험)
        - 50% 미만: -1.0점 (약세 종목 회피)
        """
        if not candles or len(candles) < 60:
            return 0.0

        # 최근 252일 (52주) 고점
        period = candles[-252:] if len(candles) >= 252 else candles
        high_52w = max(c["high"] for c in period)

        if high_52w <= 0:
            return 0.0

        position_pct = current / high_52w * 100

        if position_pct >= 95:
            return -0.5      # 신고가 근접 → 위험
        elif position_pct >= 80:
            return +0.5      # 눌림목 → 우호
        elif position_pct >= 65:
            return +0.2      # 중간
        elif position_pct >= 50:
            return 0.0       # 보통
        else:
            return -1.0      # 약세 종목

    def _check_earnings_imminent(self, ticker: str) -> dict:
        """
        실적 발표 임박 체크 (DART API)
        D-3 이내 발표 예정 종목은 제외
        """
        # DART API는 별도 API 키 필요 - 기본은 비활성화
        # ENABLE_DART_CHECK=true 일 때만 동작
        if os.getenv("ENABLE_DART_CHECK", "false").lower() != "true":
            return {"skip": False}

        try:
            import requests
            from datetime import datetime, timedelta

            api_key = os.getenv("DART_API_KEY", "")
            if not api_key:
                return {"skip": False}

            # 최근 7일 공시 조회
            today = datetime.now()
            start = (today - timedelta(days=7)).strftime("%Y%m%d")
            end = today.strftime("%Y%m%d")

            # 종목코드를 corp_code로 변환은 별도 매핑 필요
            # 여기서는 키워드 기반 간이 체크
            url = f"https://opendart.fss.or.kr/api/list.json"
            params = {
                "crtfc_key": api_key,
                "bgn_de": start,
                "end_de": end,
                "stock_code": ticker,
                "pblntf_ty": "A",  # 정기공시
            }
            resp = requests.get(url, params=params, timeout=5)
            data = resp.json()

            if data.get("status") == "000":
                items = data.get("list", [])
                for item in items:
                    title = item.get("report_nm", "")
                    if any(kw in title for kw in ["사업보고서", "분기보고서", "반기보고서"]):
                        return {
                            "skip": True,
                            "reason": f"실적 공시 임박: {title}",
                        }

        except Exception as e:
            logger.debug(f"DART 체크 오류 ({ticker}): {e}")

        return {"skip": False}

    def _apply_market_balance(self, picks: list[dict]) -> list[dict]:
        """시장 비중 조절 - 코스닥에 상한을 두고 나머지는 점수순으로 채운다

        · 코스닥 상한: 약세·하락장 20% / 횡보 30% / 정상·상승 40% (최소 1자리는 허용)
        · 앞 final_picks 개를 고르고, 밀려난 종목은 점수순으로 뒤에 붙여 돌려준다
          (추적 종목 등록이 그 뒤쪽을 쓴다).

        예전 구현의 문제 둘:
        ① 코스닥 최소 1자리를 '강제'하고 코스피를 final_picks - 1 로 묶어서, 약세장
           (추천 1개)에서는 코스피 자리가 0이 됐다. 코스닥 후보가 하나라도 있으면
           점수가 더 높은 코스피를 제치고 코스닥이 뽑혔다 — 문서의 의도와 정반대.
        ② 뉴스·갭 필터보다 앞에서 목록을 final_picks 개로 잘라, 뒤 필터가 뺀 자리를
           채울 수 없었다. 2026-09-10 드라이런: 두 자리에 솔브레인·삼성전자만 남기고
           SK하이닉스를 조용히 버린 뒤, 삼성전자가 뉴스 악재로 빠져 1종목만 추천됐다.
        """
        if not picks:
            return picks

        target = self.final_picks
        regime_name = self._get_regime().get("regime", "sideways")
        if regime_name in ("bear", "trending_down"):
            kosdaq_ratio = 0.2
        elif regime_name == "sideways":
            kosdaq_ratio = 0.3
        else:
            kosdaq_ratio = 0.4
        max_kosdaq = max(1, round(target * kosdaq_ratio))

        by_score = sorted(picks, key=lambda x: x.get("final_score", 0), reverse=True)
        selected, deferred, n_kosdaq = [], [], 0
        for p in by_score:
            if len(selected) >= target:
                deferred.append(p)
                continue
            if p.get("market") == "KOSDAQ":
                if n_kosdaq >= max_kosdaq:
                    deferred.append(p)
                    continue
                n_kosdaq += 1
            selected.append(p)

        # 코스닥 상한 때문에 자리가 남았는데 코스피 후보가 없으면 밀린 종목으로 채운다
        if len(selected) < target and deferred:
            fill = deferred[:target - len(selected)]
            selected += fill
            deferred = deferred[len(fill):]

        bumped = [p for p in deferred if by_score.index(p) < target]
        if bumped:
            logger.info(
                f"  시장 비중 조절({regime_name}, 코스닥 최대 {max_kosdaq}/{target}): "
                f"{[p.get('name', '') for p in bumped]} 후순위로"
            )
        return selected + deferred

    def _apply_learned_rules(self):
        """[2단계] 시뮬레이션에서 학습된 규칙 적용"""
        try:
            from src.utils.failure_analyzer import get_failure_analyzer
            analyzer = get_failure_analyzer()
            rules = analyzer.get_learned_rules()
            if not rules:
                self._excluded_sectors = []
                self._boost_sectors = {}
                return

            self._excluded_sectors = rules.get("excluded_sectors", [])
            self._boost_sectors    = rules.get("boost_sectors", {})

            if self._excluded_sectors:
                logger.info(f"학습된 제외 섹터: {self._excluded_sectors}")
            if self._boost_sectors:
                logger.info(f"학습된 강화 섹터: {self._boost_sectors}")
        except Exception as e:
            logger.debug(f"학습된 규칙 로드 오류: {e}")
            self._excluded_sectors = []
            self._boost_sectors = {}

    def _apply_learned_sector_adj(self, name: str) -> float:
        """학습된 섹터 가중치 적용"""
        boost_sectors = getattr(self, "_boost_sectors", {})
        excluded = getattr(self, "_excluded_sectors", [])

        from src.analyzers.sector_news import SECTOR_KEYWORDS
        stock_sector = "기타"
        for sector, keywords in SECTOR_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                stock_sector = sector
                break

        if stock_sector in excluded:
            return -3.0  # 학습된 손실 섹터는 강력 제외
        if stock_sector in boost_sectors:
            return boost_sectors[stock_sector]
        return 0.0

    def _detect_market(self, ticker: str) -> str:
        """price_data에 대표시장명이 없을 때만 쓰는 최후 폴백 (부정확함에 유의)"""
        return "KOSDAQ" if ticker.startswith("0") else "KOSPI"
