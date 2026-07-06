"""
시장 국면 분류기 (Market Regime Classifier)
- 추세장(Trending): 모멘텀 전략 유지
- 횡보장(Sideways): 역추세 전략 (과매도 반등 우선)
- 하락장(Bear):    포지션 축소 + 방어적 운용

판단 지표:
  1. 코스피 20일 MA 기울기 (추세 방향)
  2. 코스피 60일 MA 대비 위치 (중기 추세)
  3. 20일 변동성 (시장 불안정성)
  4. VIX (거시 에이전트 연동)
  5. 코스피 52주 고점 대비 낙폭 (약세장 여부)
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


REGIME_CACHE_FILE = Path("data/market_regime.json")
CACHE_TTL_HOURS = 6  # 캐시 유효시간


class MarketRegimeClassifier:
    """시장 국면 분류기"""

    # 국면 상수
    TRENDING_UP   = "trending_up"    # 상승 추세장
    TRENDING_DOWN = "trending_down"  # 하락 추세장
    SIDEWAYS      = "sideways"       # 횡보장
    BEAR          = "bear"           # 약세장 (급락)

    def classify(self, use_cache: bool = True) -> dict:
        """시장 국면 분류 실행"""

        # 캐시 확인
        if use_cache:
            cached = self._load_cache()
            if cached:
                logger.info(f"시장 국면 캐시 사용: {cached.get('regime')}")
                return cached

        logger.info("시장 국면 분류 시작")

        # 코스피 데이터 수집
        kospi_data = self._fetch_kospi_data()
        if not kospi_data:
            return self._default_result("데이터 수집 실패")

        # VIX 데이터 (거시 에이전트 캐시에서)
        vix = self._get_vix_from_cache()

        # 국면 판단
        result = self._analyze(kospi_data, vix)

        # 캐시 저장
        self._save_cache(result)
        logger.info(
            f"시장 국면: {result['regime']} "
            f"(점수:{result['regime_score']}, "
            f"MA기울기:{result['ma_slope_pct']:.2f}%)"
        )
        return result

    def _fetch_kospi_data(self) -> dict | None:
        """코스피 일봉 데이터 수집 (삼성전자 대용 → 코스피 ETF)"""
        try:
            from src.api.kis_client import KISClient
            kis = KISClient()

            # 코스피200 ETF (069500) 일봉으로 코스피 방향성 파악
            # 실제 코스피 지수 API가 안 되므로 대형 ETF 대용
            candles = kis.get_daily_ohlcv("069500", days=80)

            if len(candles) < 60:
                return None

            closes = [c["close"] for c in candles]
            highs  = [c["high"]  for c in candles]
            lows   = [c["low"]   for c in candles]

            cur = closes[-1]

            # 이동평균
            ma20  = sum(closes[-20:]) / 20
            ma60  = sum(closes[-60:]) / 60
            ma5   = sum(closes[-5:])  / 5

            # MA 기울기 (5일 전 MA20 대비)
            ma20_5ago = sum(closes[-25:-5]) / 20
            ma_slope  = (ma20 - ma20_5ago) / ma20_5ago * 100

            # 변동성 (20일 ATR 비율)
            trs = []
            for i in range(1, len(candles)):
                h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
                trs.append(max(h-l, abs(h-pc), abs(l-pc)))
            atr20 = sum(trs[-20:]) / 20
            vol_ratio = atr20 / cur * 100  # ATR/Price %

            # 52주 고점 대비 낙폭
            high_52w  = max(highs[-252:]) if len(highs) >= 252 else max(highs)
            drawdown  = (cur - high_52w) / high_52w * 100

            # 정배열 여부
            is_aligned = cur > ma5 > ma20 > ma60

            return {
                "current": cur,
                "ma5": ma5,
                "ma20": ma20,
                "ma60": ma60,
                "ma_slope_pct": ma_slope,
                "vol_ratio_pct": vol_ratio,
                "drawdown_pct": drawdown,
                "is_aligned": is_aligned,
                "atr20": atr20,
            }

        except Exception as e:
            logger.warning(f"코스피 데이터 수집 실패: {e}")
            return None

    def _get_vix_from_cache(self) -> float:
        """거시 에이전트 결과에서 VIX 추출"""
        try:
            macro_file = Path("data/macro_cache.json")
            if macro_file.exists():
                with open(macro_file) as f:
                    macro = json.load(f)
                mdata = macro.get("market_data", {})
                return mdata.get("VIX", {}).get("value", 20.0)
        except Exception:
            pass
        return 20.0  # 기본값

    def _analyze(self, d: dict, vix: float) -> dict:
        """지표 종합 → 국면 판단"""
        score = 0  # 양수=상승, 음수=하락

        signals = []

        # ── 1. MA 기울기 ─────────────────────────────────
        slope = d["ma_slope_pct"]
        if slope > 1.0:
            score += 3
            signals.append(f"MA상승기울기 +{slope:.1f}%")
        elif slope > 0.3:
            score += 1
            signals.append(f"MA완만상승 +{slope:.1f}%")
        elif slope < -1.0:
            score -= 3
            signals.append(f"MA하락기울기 {slope:.1f}%")
        elif slope < -0.3:
            score -= 1
            signals.append(f"MA완만하락 {slope:.1f}%")
        else:
            signals.append(f"MA횡보 {slope:.1f}%")

        # ── 2. 현재가 vs MA60 (중기 추세) ────────────────
        cur, ma60 = d["current"], d["ma60"]
        above_ma60 = (cur - ma60) / ma60 * 100
        if above_ma60 > 3:
            score += 2
            signals.append(f"MA60상방 +{above_ma60:.1f}%")
        elif above_ma60 > 0:
            score += 1
            signals.append(f"MA60소폭상방 +{above_ma60:.1f}%")
        elif above_ma60 < -5:
            score -= 3
            signals.append(f"MA60하방 {above_ma60:.1f}%")
        else:
            score -= 1
            signals.append(f"MA60소폭하방 {above_ma60:.1f}%")

        # ── 3. 정배열 ─────────────────────────────────────
        if d["is_aligned"]:
            score += 2
            signals.append("이동평균 정배열")
        else:
            score -= 1
            signals.append("정배열 미충족")

        # ── 4. 52주 고점 대비 낙폭 ───────────────────────
        dd = d["drawdown_pct"]
        if dd < -20:
            score -= 4
            signals.append(f"52주고점 낙폭 {dd:.1f}% (약세장)")
        elif dd < -10:
            score -= 2
            signals.append(f"52주고점 낙폭 {dd:.1f}%")
        elif dd > -5:
            score += 1
            signals.append(f"52주고점 근접 {dd:.1f}%")

        # ── 5. VIX ───────────────────────────────────────
        if vix > 30:
            score -= 3
            signals.append(f"VIX공포 {vix:.1f}")
        elif vix > 20:
            score -= 1
            signals.append(f"VIX경계 {vix:.1f}")
        elif vix < 15:
            score += 1
            signals.append(f"VIX안정 {vix:.1f}")

        # ── 6. 변동성 ─────────────────────────────────────
        vol = d["vol_ratio_pct"]
        if vol > 2.0:
            score -= 1
            signals.append(f"고변동성 {vol:.1f}%")
        elif vol < 0.8:
            score += 1
            signals.append(f"저변동성 {vol:.1f}%")

        # ── 국면 결정 ─────────────────────────────────────
        if score >= 5:
            regime = self.TRENDING_UP
            regime_label = "📈 상승 추세장"
            strategy = "모멘텀 전략 최대 활용"
            picks_multiplier = 1.2
            position_adj = 1.1
            preferred_types = ["대형우량주", "반도체", "2차전지", "성장주"]
            avoid_types = []
        elif score >= 2:
            regime = self.TRENDING_UP
            regime_label = "📈 완만한 상승장"
            strategy = "모멘텀 전략 유지"
            picks_multiplier = 1.0
            position_adj = 1.0
            preferred_types = ["대형우량주", "중형성장주"]
            avoid_types = []
        elif score >= -1:
            regime = self.SIDEWAYS
            regime_label = "↔️ 횡보장"
            strategy = "추천 대폭 축소, 고확신 종목만 (실데이터: 평균 -5.7%)"
            picks_multiplier = 0.3
            position_adj = 0.5
            preferred_types = ["중소형테마", "개별모멘텀"]
            avoid_types = ["고베타주"]
        elif score >= -4:
            regime = self.TRENDING_DOWN
            regime_label = "📉 하락 추세장"
            strategy = "방어주/배당주 우선, 매수 자제"
            picks_multiplier = 0.5
            position_adj = 0.6
            preferred_types = ["방산", "통신", "금융", "배당주"]
            avoid_types = ["성장주", "2차전지", "테마주"]
        else:
            regime = self.BEAR
            regime_label = "🔴 약세장 (급락)"
            strategy = "매수 중단 권고, 현금 100%"
            picks_multiplier = 0.2
            position_adj = 0.3
            preferred_types = ["현금", "인버스"]
            avoid_types = ["전종목"]

        return {
            "regime": regime,
            "regime_label": regime_label,
            "regime_score": score,
            "strategy": strategy,
            "picks_multiplier": picks_multiplier,
            "position_adj": position_adj,
            "preferred_types": preferred_types,
            "avoid_types": avoid_types,
            "signals": signals,
            "vix": vix,
            "ma_slope_pct": round(d["ma_slope_pct"], 2),
            "above_ma60_pct": round(above_ma60, 2),
            "drawdown_pct": round(d["drawdown_pct"], 2),
            "vol_ratio_pct": round(d["vol_ratio_pct"], 2),
            "is_aligned": d["is_aligned"],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def _default_result(self, reason: str = "") -> dict:
        return {
            "regime": self.SIDEWAYS,
            "regime_label": "↔️ 판단 불가 (기본값)",
            "regime_score": 0,
            "strategy": "기본 전략 유지",
            "picks_multiplier": 1.0,
            "position_adj": 1.0,
            "preferred_types": [],
            "avoid_types": [],
            "signals": [reason],
            "vix": 20.0,
            "ma_slope_pct": 0,
            "above_ma60_pct": 0,
            "drawdown_pct": 0,
            "vol_ratio_pct": 1.0,
            "is_aligned": False,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    def _load_cache(self) -> dict | None:
        if not REGIME_CACHE_FILE.exists():
            return None
        try:
            with open(REGIME_CACHE_FILE) as f:
                data = json.load(f)
            updated = datetime.strptime(data["updated_at"], "%Y-%m-%d %H:%M")
            if datetime.now() - updated < timedelta(hours=CACHE_TTL_HOURS):
                return data
        except Exception:
            pass
        return None

    def _save_cache(self, result: dict):
        REGIME_CACHE_FILE.parent.mkdir(exist_ok=True)
        with open(REGIME_CACHE_FILE, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
