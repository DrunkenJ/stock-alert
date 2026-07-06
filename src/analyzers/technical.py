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

    def analyze(self, candles: list[dict]) -> dict:
        """전체 기술적 분석 수행 → 종합 점수 반환"""
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

        # 실거래 데이터 분석 결과 거래량 급증 종목일수록 수익률이 오히려 나빴음
        # (이미 상투/소진된 뒤에 스크리닝에 잡히는 경우가 많음) - 과도한 가점 축소,
        # 극단적 급증은 소진 신호로 보고 감점
        if vol_ratio >= 4.0:
            score -= 1
            signals.append({"type": "negative", "name": "거래량 과열", "detail": f"평균 대비 {vol_ratio:.1f}배 (소진 위험)"})
        elif vol_ratio >= 2.0:
            score += 1
            signals.append({"type": "positive", "name": "거래량 증가", "detail": f"평균 대비 {vol_ratio:.1f}배"})
        elif vol_ratio >= 1.5:
            score += 1
            signals.append({"type": "positive", "name": "거래량 증가", "detail": f"평균 대비 {vol_ratio:.1f}배"})

        # ── 52주 신고가 근접 ──────────────────────
        if len(df) >= 60:
            high_60 = df["high"].rolling(60).max().iloc[-1]
            if cur_price >= high_60 * 0.98:
                score += 2
                signals.append({"type": "positive", "name": "52주 신고가 근접", "detail": f"현재가 = 고가의 {cur_price/high_60:.1%}"})

        # ── 연속 양봉 ─────────────────────────────
        recent = df.tail(3)
        consec_bull = all(recent["close"] > recent["open"])
        if consec_bull:
            score += 1
            signals.append({"type": "positive", "name": "연속 양봉", "detail": "최근 3일 연속 양봉"})

        # ── 지지선 근처 ───────────────────────────
        if ma20:
            dist_from_ma20 = abs(cur_price - ma20) / ma20
            if dist_from_ma20 < 0.02 and cur_price >= ma20:
                score += 1
                signals.append({"type": "positive", "name": "20MA 지지", "detail": f"20MA 근접 ({dist_from_ma20:.1%})"})

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

    def _bollinger(self, series: pd.Series, period=20, std=2):
        if len(series) < period:
            return None
        mid = series.rolling(period).mean().iloc[-1]
        std_val = series.rolling(period).std().iloc[-1]
        return mid + std * std_val, mid, mid - std * std_val
