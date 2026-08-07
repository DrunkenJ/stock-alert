from typing import Optional, Tuple, List, Dict, Any
"""
추적 종목 시스템 (Watchlist)
- 좋은 신호지만 진입 타이밍이 안 맞는 종목 등록
- 매일 조정 여부 모니터링
- 눌림목 + 반등 신호 시 매수 타이밍 알림

등록 경로:
  ① 갭상승 과열 종목 자동 등록
  ② 추천 종목 초과 (6위 이하) 자동 등록
  ③ !추적 명령어 수동 등록

매수 타이밍 조건:
  - 등록가 대비 -1~-5% 조정 (눌림목)
  - RSI 40~60 (중립 회복)
  - 당일 수급 유지 (외국인/기관 매도 없음)
  - MA20 위에서 지지
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


WATCHLIST_FILE = Path("data/watchlist.json")
MAX_WATCHLIST  = 20    # 최대 추적 종목 수
MAX_DAYS       = 10    # 최대 추적 기간 (일)

# 매수 타이밍 조건
PULLBACK_MIN = -0.05   # 최소 조정폭 (-5%)
PULLBACK_MAX = -0.01   # 최대 조정폭 (-1%, 이보다 적으면 아직 조정 안 됨)
RSI_MIN      = 38.0
RSI_MAX      = 62.0


def load_watchlist() -> dict:
    if not WATCHLIST_FILE.exists():
        return {}
    try:
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_watchlist(wl: dict):
    WATCHLIST_FILE.parent.mkdir(exist_ok=True)
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)


class WatchlistManager:
    """추적 종목 관리자"""

    def add(self, ticker: str, name: str, score: float,
            entry_price: int, reason: str = "") -> bool:
        """추적 종목 등록"""
        if not ticker or not str(ticker).strip():
            # 티커 없는 항목은 시세 조회가 영구 실패해 목록에 죽은 채로 남는다
            logger.warning(f"추적 등록 무시 - 티커 없음: {name}")
            return False

        wl = load_watchlist()

        if ticker in wl:
            logger.debug(f"이미 추적 중: {name}")
            return False

        if len(wl) >= MAX_WATCHLIST:
            # 오래된 종목 제거
            oldest = min(wl.items(), key=lambda x: x[1].get("added_at", ""))
            del wl[oldest[0]]
            logger.debug(f"추적 목록 초과 → {oldest[1]['name']} 제거")

        # 목표 진입가: 등록가 대비 -2~3% 눌림목
        target_entry = int(entry_price * 0.97)

        wl[ticker] = {
            "ticker":       ticker,
            "name":         name,
            "score":        score,
            "reg_price":    entry_price,   # 등록 시점 가격
            "target_entry": target_entry,  # 목표 진입가 (눌림목)
            "reason":       reason,
            "added_at":     datetime.now().strftime("%Y-%m-%d %H:%M"),
            "added_date":   datetime.now().strftime("%Y-%m-%d"),
            "alerted":      False,         # 알림 발송 여부
            "alert_count":  0,             # 알림 횟수
        }
        save_watchlist(wl)
        logger.info(f"추적 등록: {name}({ticker}) 점수={score:.1f} 등록가={entry_price:,} 목표진입={target_entry:,}")
        return True

    def remove(self, ticker: str) -> bool:
        wl = load_watchlist()
        if ticker in wl:
            del wl[ticker]
            save_watchlist(wl)
            return True
        return False

    def clean_expired(self):
        """만료된 추적 종목 정리 (MAX_DAYS 초과)"""
        wl = load_watchlist()
        today = datetime.now().date()
        expired = []
        for ticker, item in wl.items():
            # 티커가 비어있으면 조회 불가 → 즉시 제거
            if not ticker or not str(ticker).strip():
                expired.append(ticker)
                continue
            try:
                added = datetime.strptime(item["added_date"], "%Y-%m-%d").date()
                if (today - added).days > MAX_DAYS:
                    expired.append(ticker)
            except Exception:
                expired.append(ticker)

        for t in expired:
            logger.info(f"추적 만료 제거: {wl[t]['name']} ({MAX_DAYS}일 초과)")
            del wl[t]

        if expired:
            save_watchlist(wl)
        return expired

    def check_entry_timing(self, ticker: str, item: dict,
                           candles: list, investor: dict = None) -> Optional[dict]:
        """
        매수 타이밍 체크
        Returns: 타이밍 신호 dict or None
        """
        if not candles or len(candles) < 20:
            return None

        reg_price = item["reg_price"]
        closes    = [c["close"] for c in candles]
        current   = closes[-1]

        # ── 1. 조정폭 체크 ──────────────────────────────
        pullback = (current - reg_price) / reg_price
        if not (PULLBACK_MIN <= pullback <= PULLBACK_MAX):
            return None  # 아직 조정 안 됨 or 너무 많이 하락

        # ── 2. RSI 체크 ──────────────────────────────────
        rsi = self._calc_rsi(closes)
        if rsi is None or not (RSI_MIN <= rsi <= RSI_MAX):
            return None  # 과매수/과매도 아닌 중립 구간만

        # ── 3. MA20 위 지지 체크 ─────────────────────────
        ma20 = sum(closes[-20:]) / 20
        if current < ma20 * 0.99:  # MA20 1% 아래면 패스
            return None

        # ── 4. 수급 체크 (매도 전환 없는지) ─────────────
        if investor:
            detail = investor.get("detail", [])[:3]
            foreign_net = sum(d.get("foreign", 0) for d in detail)
            inst_net    = sum(d.get("inst", 0) for d in detail)
            # 외국인+기관 모두 매도 전환이면 패스
            if foreign_net < 0 and inst_net < 0:
                return None

        # ── 5. 반등 신호 체크 (당일 양봉) ───────────────
        today_candle = candles[-1]
        is_bullish = today_candle["close"] > today_candle["open"]

        # 조건 모두 충족 → 타이밍 신호
        pullback_pct = pullback * 100

        return {
            "ticker":       ticker,
            "name":         item["name"],
            "score":        item["score"],
            "reg_price":    reg_price,
            "current":      current,
            "pullback_pct": round(pullback_pct, 2),
            "rsi":          round(rsi, 1),
            "ma20":         round(ma20, 0),
            "is_bullish":   is_bullish,
            "target_entry": item.get("target_entry", current),
            "reason":       item.get("reason", ""),
        }

    def check_all(self, kis_client) -> List[dict]:
        """전체 추적 종목 타이밍 체크"""
        wl = load_watchlist()
        if not wl:
            return []

        signals = []
        for ticker, item in wl.items():
            try:
                candles  = kis_client.get_daily_ohlcv(ticker, days=30)
                investor = kis_client.get_investor_trend(ticker, days=5)
                signal   = self.check_entry_timing(ticker, item, candles, investor)

                if signal:
                    # 이미 오늘 알림 보낸 종목은 스킵
                    last_alert = item.get("last_alert_date", "")
                    today = datetime.now().strftime("%Y-%m-%d")
                    if last_alert == today:
                        continue

                    # 알림 기록 업데이트
                    wl[ticker]["alerted"]         = True
                    wl[ticker]["alert_count"]      = item.get("alert_count", 0) + 1
                    wl[ticker]["last_alert_date"]  = today
                    signals.append(signal)

            except Exception as e:
                logger.debug(f"추적 체크 실패 ({ticker}): {e}")

        if signals:
            save_watchlist(wl)

        return signals

    def get_all(self) -> List[dict]:
        """전체 추적 종목 목록"""
        wl = load_watchlist()
        return list(wl.values())

    def _calc_rsi(self, closes: list, period: int = 14) -> Optional[float]:
        if len(closes) < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, len(closes[-(period+1):])):
            diff = closes[-(period+1)+i] - closes[-(period+1)+i-1]
            gains.append(max(diff, 0))
            losses.append(abs(min(diff, 0)))
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))


# 싱글톤
_manager = None

def get_watchlist_manager() -> WatchlistManager:
    global _manager
    if _manager is None:
        _manager = WatchlistManager()
    return _manager
