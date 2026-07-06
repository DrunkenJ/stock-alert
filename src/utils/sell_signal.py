from typing import Optional, Tuple, List, Dict, Any
"""
매도 신호 감지기
보유 중인 종목에서 다양한 매도 신호를 감지

감지 신호:
  ① 수급 역전: 외국인/기관 순매도 전환
  ② 기술적 붕괴: MA 데드크로스, RSI 과매수 반전
  ③ 갭하락: 전일 대비 -3% 이상
  ④ 거래량 소멸: 평균 대비 30% 이하
  ⑤ 목표가/손절가 도달 (기존 유지)
"""
import json
from datetime import datetime
from pathlib import Path
from loguru import logger


HOLDINGS_FILE = Path("data/holdings.json")
SELL_SIGNAL_THRESHOLD = 2  # 이 이상 신호 시 매도 권고


def load_holdings() -> dict:
    """보유 종목 로드"""
    if not HOLDINGS_FILE.exists():
        return {}
    try:
        with open(HOLDINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_holdings(holdings: dict):
    """보유 종목 저장"""
    HOLDINGS_FILE.parent.mkdir(exist_ok=True)
    with open(HOLDINGS_FILE, "w") as f:
        json.dump(holdings, f, ensure_ascii=False, indent=2)


def add_holding(ticker: str, name: str, entry_price: int,
                target: int = 0, stop: int = 0, quantity: int = 0):
    """보유 종목 수동 등록"""
    holdings = load_holdings()
    holdings[ticker] = {
        "ticker": ticker,
        "name": name,
        "entry_price": entry_price,
        "target_price": target or int(entry_price * 1.06),
        "stop_loss": stop or int(entry_price * 0.97),
        "quantity": quantity,
        "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "high_price": entry_price,
    }
    save_holdings(holdings)
    logger.info(f"보유 종목 등록: {name}({ticker}) 진입가={entry_price:,}")
    return holdings[ticker]


def remove_holding(ticker: str) -> bool:
    """보유 종목 제거"""
    holdings = load_holdings()
    if ticker in holdings:
        del holdings[ticker]
        save_holdings(holdings)
        return True
    return False


class SellSignalDetector:
    """매도 신호 감지기"""

    def __init__(self, kis_client=None):
        self.kis = kis_client

    def detect(self, ticker: str, holding: dict,
               candles: list = None,
               investor: dict = None) -> List[dict]:
        """
        종목별 매도 신호 전체 감지

        Returns:
            감지된 신호 리스트 [{signal, severity, message}, ...]
        """
        signals = []

        if not candles or len(candles) < 5:
            return signals

        current_price = candles[-1]["close"]
        entry = holding.get("entry_price", current_price)
        target = holding.get("target_price", int(entry * 1.06))
        stop = holding.get("stop_loss", int(entry * 0.97))

        # ── ① 목표가/손절가 도달 ────────────────────────
        if current_price <= stop:
            pct = (current_price - entry) / entry * 100
            signals.append({
                "signal": "stop_loss",
                "severity": "high",
                "message": f"손절가 도달 ({current_price:,}원 / {pct:.1f}%)",
                "action": "즉시 매도",
            })

        if current_price >= target:
            pct = (current_price - entry) / entry * 100
            signals.append({
                "signal": "target_reached",
                "severity": "medium",
                "message": f"목표가 도달 ({current_price:,}원 / +{pct:.1f}%)",
                "action": "익절 또는 트레일링 전환",
            })

        # ── ② 갭하락 감지 ───────────────────────────────
        if len(candles) >= 2:
            prev_close = candles[-2]["close"]
            today_open = candles[-1].get("open", current_price)
            gap_pct = (today_open - prev_close) / prev_close * 100
            if gap_pct <= -3.0:
                signals.append({
                    "signal": "gap_down",
                    "severity": "high",
                    "message": f"갭하락 {gap_pct:.1f}% 발생",
                    "action": "매도 검토 (추세 붕괴 가능)",
                })
            elif gap_pct <= -1.5:
                signals.append({
                    "signal": "gap_down_mild",
                    "severity": "low",
                    "message": f"소폭 갭하락 {gap_pct:.1f}%",
                    "action": "모니터링 강화",
                })

        # ── ③ 기술적 추세 붕괴 ──────────────────────────
        closes = [c["close"] for c in candles]

        # MA5 vs MA20 데드크로스
        if len(closes) >= 20:
            ma5_now  = sum(closes[-5:]) / 5
            ma20_now = sum(closes[-20:]) / 20
            ma5_prev = sum(closes[-6:-1]) / 5
            ma20_prev = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else ma20_now

            # 데드크로스: MA5가 MA20 아래로 돌파
            if ma5_prev >= ma20_prev and ma5_now < ma20_now:
                signals.append({
                    "signal": "dead_cross",
                    "severity": "medium",
                    "message": f"MA5/MA20 데드크로스 (MA5={ma5_now:,.0f} < MA20={ma20_now:,.0f})",
                    "action": "매도 검토",
                })

        # RSI 과매수 후 하락 반전 (70 이상 → 60 이하로)
        rsi = self._calculate_rsi(closes)
        if rsi is not None:
            if rsi > 70:
                signals.append({
                    "signal": "rsi_overbought",
                    "severity": "low",
                    "message": f"RSI 과매수 구간 ({rsi:.1f})",
                    "action": "익절 분할 매도 고려",
                })

        # ── ④ 수급 역전 ─────────────────────────────────
        if investor:
            detail = investor.get("detail", [])
            if len(detail) >= 3:
                # 최근 3일 외국인/기관 동향
                recent = detail[:3]
                foreign_net = sum(d.get("foreign", 0) for d in recent)
                inst_net = sum(d.get("inst", 0) for d in recent)

                # 기존에 매수하던 주체가 순매도로 전환
                foreign_5d = investor.get("foreign_net", 0)
                inst_5d = investor.get("inst_net", 0)

                if foreign_5d > 0 and foreign_net < 0:
                    signals.append({
                        "signal": "foreign_reversal",
                        "severity": "medium",
                        "message": f"외국인 순매도 전환 (최근3일: {foreign_net:+,}주)",
                        "action": "수급 이탈 모니터링",
                    })

                if inst_5d > 0 and inst_net < 0:
                    signals.append({
                        "signal": "inst_reversal",
                        "severity": "medium",
                        "message": f"기관 순매도 전환 (최근3일: {inst_net:+,}주)",
                        "action": "수급 이탈 모니터링",
                    })

                # 외국인 + 기관 동반 매도 (강한 신호)
                if foreign_net < 0 and inst_net < 0:
                    signals.append({
                        "signal": "dual_selling",
                        "severity": "high",
                        "message": f"외국인+기관 동반 순매도 (외:{foreign_net:+,} 기:{inst_net:+,})",
                        "action": "매도 강력 검토",
                    })

        # ── ⑤ 거래량 소멸 ───────────────────────────────
        if len(candles) >= 10:
            volumes = [c.get("volume", 0) for c in candles]
            avg_vol = sum(volumes[-10:-1]) / 9
            today_vol = volumes[-1]
            if avg_vol > 0 and today_vol < avg_vol * 0.3:
                signals.append({
                    "signal": "volume_dry",
                    "severity": "low",
                    "message": f"거래량 소멸 (평균 {avg_vol:,.0f} → 오늘 {today_vol:,})",
                    "action": "추세 지속성 의심, 모니터링",
                })

        return signals

    def _calculate_rsi(self, closes: list, period: int = 14) -> Optional[float]:
        """RSI 계산"""
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes[-period-1:])):
            diff = closes[-period-1+i] - closes[-period-1+i-1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def evaluate_severity(self, signals: List[dict]) -> str:
        """전체 신호 심각도 평가"""
        high = sum(1 for s in signals if s["severity"] == "high")
        medium = sum(1 for s in signals if s["severity"] == "medium")
        if high >= 1:
            return "high"
        elif medium >= 2:
            return "high"
        elif medium >= 1:
            return "medium"
        elif signals:
            return "low"
        return "none"
