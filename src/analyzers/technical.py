"""
기술적 분석 모듈
- 이동평균, RSI, MACD, 볼린저밴드, 거래량 분석
- 차트 패턴 스코어링
"""
import numpy as np
import pandas as pd
from loguru import logger


class TechnicalAnalyzer:
    """기술적 지표 계산 및 차트 패턴 분석"""

    def analyze(self, candles: list[dict], market_ret20: float | None = None) -> dict:
        """전체 기술적 분석 수행 → 종합 점수 반환

        market_ret20: 지수의 최근 20일 수익률(%). 주어지면 상대강도(RS)를 계산한다.
        """
        if len(candles) < 20:
            return {"score": 0, "signals": [], "error": "데이터 부족"}

        df = pd.DataFrame(candles)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["open"] = df["open"].astype(float)

        signals = []
        score = 0

        # ── 이동평균 ──────────────────────────────
        ma5 = self._sma(df["close"], 5)
        ma20 = self._sma(df["close"], 20)
        ma60 = self._sma(df["close"], 60) if len(df) >= 60 else None
        ma120 = self._sma(df["close"], 120) if len(df) >= 120 else None
        cur_price = df["close"].iloc[-1]

        # 정배열 체크 (5 > 20 > 60 > 120)
        alignment_score = 0
        if ma5 and ma20 and cur_price > ma5 > ma20:
            alignment_score += 2
        if ma60 and ma20 > ma60:
            alignment_score += 1
        if ma120 and ma60 and ma60 > ma120:
            alignment_score += 1
        score += alignment_score
        if alignment_score >= 3:
            signals.append({"type": "positive", "name": "이동평균 정배열", "detail": f"5MA>{20}MA>{60}MA"})

        # 골든크로스 체크 (최근 5일 내)
        if len(df) >= 25:
            prev_ma5 = self._sma(df["close"].iloc[:-1], 5)
            prev_ma20 = self._sma(df["close"].iloc[:-1], 20)
            if prev_ma5 and prev_ma20 and prev_ma5 < prev_ma20 and ma5 > ma20:
                score += 3
                signals.append({"type": "positive", "name": "골든크로스 발생", "detail": "5MA/20MA 골든크로스"})

        # ── RSI ──────────────────────────────────
        rsi = self._rsi(df["close"], 14)
        if rsi:
            if 40 <= rsi <= 60:
                score += 1
                signals.append({"type": "neutral", "name": "RSI 중립", "detail": f"RSI={rsi:.1f}"})
            elif rsi < 35:
                score += 2
                signals.append({"type": "positive", "name": "RSI 과매도 반등", "detail": f"RSI={rsi:.1f} (과매도 구간)"})
            elif rsi > 70:
                score -= 1
                signals.append({"type": "negative", "name": "RSI 과매수", "detail": f"RSI={rsi:.1f}"})

        # ── MACD ─────────────────────────────────
        macd_result = self._macd(df["close"])
        if macd_result:
            macd, signal_line, hist = macd_result
            if hist > 0 and macd > signal_line:
                score += 2
                signals.append({"type": "positive", "name": "MACD 상승", "detail": f"히스토그램={hist:.2f}"})
            elif hist < 0 and hist > self._prev_hist(df["close"]):
                score += 1
                signals.append({"type": "positive", "name": "MACD 수렴", "detail": "히스토그램 개선 중"})

        # ── 볼린저밴드 ────────────────────────────
        bb_result = self._bollinger(df["close"])
        if bb_result:
            upper, mid, lower = bb_result
            bb_position = (cur_price - lower) / (upper - lower) if upper != lower else 0.5
            if bb_position < 0.3:
                score += 2
                signals.append({"type": "positive", "name": "볼린저밴드 하단", "detail": f"밴드 위치={bb_position:.1%}"})
            elif bb_position > 0.9:
                score -= 1
                signals.append({"type": "negative", "name": "볼린저밴드 상단 돌파", "detail": f"밴드 위치={bb_position:.1%}"})

        # ── 거래량 분석 ───────────────────────────
        vol_ma20 = df["volume"].rolling(20).mean().iloc[-1]
        cur_vol = df["volume"].iloc[-1]
        vol_ratio = cur_vol / vol_ma20 if vol_ma20 > 0 else 0

        # 79건 실거래 검증: 거래량비가 낮을수록 성과가 좋았음
        #   <1.0배 → 승률 68% / +0.35%,  2.0~4.0배 → 승률 44% / -4.17%
        # 거래량 급증일 매수 = 관심 집중 후 단기 반전 구간 매수. 가점이 아니라 감점 대상.
        if vol_ratio >= 4.0:
            score -= 2
            signals.append({"type": "negative", "name": "거래량 폭증", "detail": f"평균 대비 {vol_ratio:.1f}배 (소진/상투 위험)"})
        elif vol_ratio >= 2.0:
            score -= 1
            signals.append({"type": "negative", "name": "거래량 과열", "detail": f"평균 대비 {vol_ratio:.1f}배"})
        elif vol_ratio <= 1.2:
            score += 1
            signals.append({"type": "positive", "name": "거래량 안정", "detail": f"평균 대비 {vol_ratio:.1f}배 (조용한 매집 구간)"})

        # ── 변동성 수축 (VCP) ─────────────────────
        # ATR5/ATR20 이 1 미만 = 최근 변동성이 줄어드는 중 → 에너지 응축 구간.
        # 79건 검증에서 0.8~1.0 구간이 유일하게 승률 60%를 넘긴 구간이었음
        # (1.0~1.3 → 25%, 1.3 이상 → 11%)
        atr5 = self._atr(df, 5)
        atr20 = self._atr(df, 20)
        atr_ratio = (atr5 / atr20) if (atr5 and atr20) else None
        if atr_ratio is not None:
            if 0.8 <= atr_ratio <= 1.0:
                score += 2
                signals.append({"type": "positive", "name": "변동성 수축(VCP)", "detail": f"ATR5/ATR20={atr_ratio:.2f}"})
            elif atr_ratio < 0.8:
                score += 1
                signals.append({"type": "positive", "name": "변동성 급수축", "detail": f"ATR5/ATR20={atr_ratio:.2f}"})
            elif atr_ratio >= 1.3:
                score -= 2
                signals.append({"type": "negative", "name": "변동성 확대", "detail": f"ATR5/ATR20={atr_ratio:.2f} (급등락 진행 중)"})

        # ── 20MA 이격도 (과열 추격 방지) ───────────
        # 79건 검증: 이격 10~20% → 3일 -12.2%, 20% 이상 → -9.3%, 0% 이하 → -3.6%
        disparity = ((cur_price - ma20) / ma20 * 100) if ma20 else None
        if disparity is not None:
            if disparity >= 20:
                score -= 2
                signals.append({"type": "negative", "name": "20MA 과대이격", "detail": f"+{disparity:.1f}% (되돌림 위험)"})
            elif disparity >= 10:
                score -= 1
                signals.append({"type": "negative", "name": "20MA 이격 확대", "detail": f"+{disparity:.1f}%"})

        # ── 20일 상승률 (급등 후 추격 방지) ────────
        ret20 = None
        if len(df) >= 21:
            past = df["close"].iloc[-21]
            if past > 0:
                ret20 = (cur_price - past) / past * 100
                if ret20 >= 50:
                    score -= 2
                    signals.append({"type": "negative", "name": "단기 급등", "detail": f"20일 +{ret20:.0f}% (고점 추격 위험)"})
                elif ret20 >= 25:
                    score -= 1
                    signals.append({"type": "negative", "name": "단기 상승 과다", "detail": f"20일 +{ret20:.0f}%"})

        # ── 상대강도 (RS: 지수 대비 초과 수익) ─────
        rs = None
        if ret20 is not None and market_ret20 is not None:
            rs = ret20 - market_ret20
            # 지수보다 약한 종목은 매수 대상에서 후순위
            if rs < -5:
                score -= 1
                signals.append({"type": "negative", "name": "지수 대비 약세", "detail": f"RS {rs:+.1f}%p"})
            elif 0 <= rs <= 25:
                # 지수를 이기되 과열은 아닌 구간이 이상적
                score += 1
                signals.append({"type": "positive", "name": "지수 대비 강세", "detail": f"RS {rs:+.1f}%p"})

        # ── 52주 신고가 근접 ──────────────────────
        # 기존에는 +2 가점이었으나, 실거래 검증 결과 '이미 다 간 종목'을 잡아내는
        # 신호로 작동했음. 이격도가 이미 벌어진 상태에서의 신고가는 감점 처리.
        if len(df) >= 60:
            high_60 = df["high"].rolling(60).max().iloc[-1]
            if cur_price >= high_60 * 0.98:
                if disparity is not None and disparity >= 10:
                    score -= 1
                    signals.append({"type": "negative", "name": "과열 상태 신고가", "detail": f"20MA 이격 +{disparity:.1f}%"})
                else:
                    signals.append({"type": "neutral", "name": "신고가 근접", "detail": f"현재가 = 60일 고가의 {cur_price/high_60:.1%}"})

        # ── 연속 양봉 ─────────────────────────────
        # 가점 제거: screener._calc_consecutive_up_adj 가 이미 연속 상승을 감점하고 있어
        # 서로 상충했음. 여기서는 정보 신호로만 남긴다.
        recent = df.tail(3)
        consec_bull = all(recent["close"] > recent["open"])
        if consec_bull:
            signals.append({"type": "neutral", "name": "연속 양봉", "detail": "최근 3일 연속 양봉"})

        # ── 지지선 근처 ───────────────────────────
        if ma20:
            dist_from_ma20 = abs(cur_price - ma20) / ma20
            if dist_from_ma20 < 0.02 and cur_price >= ma20:
                score += 1
                signals.append({"type": "positive", "name": "20MA 지지", "detail": f"20MA 근접 ({dist_from_ma20:.1%})"})

        atr14 = self._atr(df, 14)
        atr_pct = (atr14 / cur_price * 100) if (atr14 and cur_price) else None

        return {
            "score": max(0, min(score, 15)),  # 0~15 정규화
            "signals": signals,
            "indicators": {
                "rsi": round(rsi, 1) if rsi else None,
                "macd_hist": round(macd_result[2], 3) if macd_result else None,
                "vol_ratio": round(vol_ratio, 1),
                "ma5": round(ma5, 0) if ma5 else None,
                "ma20": round(ma20, 0) if ma20 else None,
                "bb_position": round(bb_position, 3) if bb_result else None,
                "price": cur_price,
                # ── 스크리너 하드필터/기록용 추가 지표 ──
                "atr": round(atr14, 1) if atr14 else None,
                "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
                "atr_ratio": round(atr_ratio, 2) if atr_ratio is not None else None,
                "disparity20": round(disparity, 2) if disparity is not None else None,
                "ret20": round(ret20, 2) if ret20 is not None else None,
                "rs": round(rs, 2) if rs is not None else None,
                "above_ma20": bool(ma20 and cur_price > ma20),
            },
        }

    # ─────────────────────────────────────────
    # 보조 계산 함수들
    # ─────────────────────────────────────────
    def _sma(self, series: pd.Series, period: int) -> float | None:
        if len(series) < period:
            return None
        return series.rolling(period).mean().iloc[-1]

    def _rsi(self, series: pd.Series, period: int = 14) -> float | None:
        if len(series) < period + 1:
            return None
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]

    def _macd(self, series: pd.Series, fast=12, slow=26, signal=9):
        if len(series) < slow + signal:
            return None
        ema_fast = series.ewm(span=fast, adjust=False).mean()
        ema_slow = series.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line
        return macd_line.iloc[-1], signal_line.iloc[-1], hist.iloc[-1]

    def _prev_hist(self, series: pd.Series) -> float:
        result = self._macd(series.iloc[:-1])
        return result[2] if result else 0

    def _atr(self, df: pd.DataFrame, period: int = 14) -> float | None:
        """ATR (Average True Range) - 단순평균 방식"""
        if len(df) < period + 1:
            return None
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ], axis=1).max(axis=1)
        val = tr.rolling(period).mean().iloc[-1]
        return float(val) if pd.notna(val) else None

    def _bollinger(self, series: pd.Series, period=20, std=2):
        if len(series) < period:
            return None
        mid = series.rolling(period).mean().iloc[-1]
        std_val = series.rolling(period).std().iloc[-1]
        return mid + std * std_val, mid, mid - std * std_val
