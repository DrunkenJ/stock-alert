from typing import Optional, Tuple, List, Dict, Any
"""
트레일링 스탑 관리자
- 익절가 도달 시 트레일링 모드로 전환
- 고점 대비 -N% 하락 시 매도 신호
- data/trailing_stops.json 에 상태 저장 (재시작 복구)
"""
import json
from datetime import datetime
from pathlib import Path
from loguru import logger

TRAILING_FILE = Path("data/trailing_stops.json")
TRAILING_PCT  = 0.03  # 고점 대비 -3% 하락 시 매도
ACTIVATE_PCT  = 0.03  # 익절가 도달 시점 트레일링 활성화

# 분할 익절 설정 (최소값 - 실제로는 종목별 ATR 목표가에 비례해 add()에서 확대됨)
PARTIAL_1_PCT  = 0.04  # 1차 익절 최소: +4% (50% 매도)
PARTIAL_2_PCT  = 0.08  # 2차 익절 최소: +8% (30% 매도)
PARTIAL_1_RATIO = 0.5  # 1차 매도 비율
PARTIAL_2_RATIO = 0.3  # 2차 매도 비율
# 나머지 20%는 트레일링 스탑이 관리


class TrailingStopManager:
    """트레일링 스탑 상태 관리"""

    def __init__(self):
        self.stops = self._load()

    # ── 공개 인터페이스 ───────────────────────────────────

    def add(self, pick: dict):
        """추천 종목 등록 (트레일링 대기 상태)"""
        ticker = pick.get("ticker", "")
        es = pick.get("entry_strategy", {})

        entry_price  = es.get("split_1st", pick.get("price", 0))
        target_price = es.get("target_price", int(entry_price * 1.06))
        stop_loss    = es.get("stop_loss", int(entry_price * 0.97))
        atr          = es.get("atr", 0)

        # 분할 익절가 계산
        # entry_calculator가 종목별 ATR/점수 기반으로 계산한 target_price(손익비 반영)에
        # 비례해서 1차/2차 익절폭을 정한다. 예전에는 모든 종목에 고정 4%/8%를 적용했는데,
        # 손절폭은 ATR에 따라 최대 -7%까지 벌어지는 반면 익절은 항상 4%/8%로 묶여 있어서
        # 변동성(거래량 급증)이 큰 종목일수록 손익비가 무너지는 문제가 있었다.
        # (실거래 데이터: vol_ratio가 높은 종목일수록 최종 수익률이 나빴던 원인 중 하나)
        # 고정값은 하한으로만 사용 - 저변동성 종목의 빠른 익절 습관은 그대로 유지.
        target_upside_pct = (target_price - entry_price) / entry_price if entry_price else 0.0
        partial_1_pct = max(PARTIAL_1_PCT, target_upside_pct * 0.5)
        partial_2_pct = max(PARTIAL_2_PCT, target_upside_pct)
        partial_1_price = int(entry_price * (1 + partial_1_pct))
        partial_2_price = int(entry_price * (1 + partial_2_pct))

        self.stops[ticker] = {
            "ticker":       ticker,
            "name":         pick.get("name", ""),
            "entry_price":  entry_price,
            "target_price": target_price,
            "stop_loss":    stop_loss,
            "atr":          atr,
            "high_price":   entry_price,
            "trailing_active":  False,
            "trailing_stop":    0,
            # 분할 익절 상태
            "partial_1_price":  partial_1_price,   # 1차 익절가 (목표가의 절반 지점, 최소 +4%)
            "partial_2_price":  partial_2_price,   # 2차 익절가 (ATR 기반 목표가, 최소 +8%)
            "partial_1_done":   False,             # 1차 익절 완료 여부
            "partial_2_done":   False,             # 2차 익절 완료 여부
            "registered_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "date": datetime.now().strftime("%Y-%m-%d"),
        }
        self._save()
        logger.info(f"트레일링 등록: {pick.get('name','')} 진입={entry_price:,} 익절={target_price:,}")

    def update_and_check(self, ticker: str, current_price: int) -> Optional[dict]:
        """
        현재가로 트레일링 상태 업데이트 + 매도 신호 체크

        Returns:
            None: 정상 보유
            dict: 매도 신호 (reason, price 포함)
        """
        if ticker not in self.stops:
            return None

        s = self.stops[ticker]
        high = s["high_price"]
        entry = s["entry_price"]
        target = s["target_price"]
        stop = s["stop_loss"]

        # ── 고점 갱신 ─────────────────────────────────────
        if current_price > high:
            s["high_price"] = current_price
            high = current_price
            if s["trailing_active"]:
                s["trailing_stop"] = int(high * (1 - TRAILING_PCT))
                logger.debug(
                    f"트레일링 갱신: {s['name']} "
                    f"고점={high:,} 트레일링손절={s['trailing_stop']:,}"
                )

        # ── 1차 분할 익절 체크 (+4%, 50% 매도) ──────────
        if not s.get("partial_1_done") and current_price >= s.get("partial_1_price", 0):
            s["partial_1_done"] = True
            # 손절가를 진입가로 상향 (본전 손절 → 리스크 제거)
            s["stop_loss"] = entry
            gain_pct = (current_price - entry) / entry * 100
            logger.info(f"1차 분할 익절: {s['name']} +{gain_pct:.1f}% → 50% 매도, 손절→본전")
            self._save()
            return {
                "signal":        "partial_exit_1",
                "ticker":        ticker,
                "name":          s["name"],
                "current_price": current_price,
                "entry_price":   entry,
                "gain_pct":      round(gain_pct, 2),
                "sell_ratio":    PARTIAL_1_RATIO,
                "sell_pct":      int(PARTIAL_1_RATIO * 100),
                "new_stop":      entry,
                "next_target":   s.get("partial_2_price", 0),
            }

        # ── 2차 분할 익절 체크 (+8%, 30% 매도) ──────────
        if s.get("partial_1_done") and not s.get("partial_2_done")                 and current_price >= s.get("partial_2_price", 0):
            s["partial_2_done"] = True
            gain_pct = (current_price - entry) / entry * 100
            logger.info(f"2차 분할 익절: {s['name']} +{gain_pct:.1f}% → 30% 매도, 트레일링 활성화")
            # 트레일링 스탑 활성화 (나머지 20% 관리)
            s["trailing_active"] = True
            s["trailing_stop"]   = int(current_price * (1 - TRAILING_PCT))
            s["trailing_activated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._save()
            return {
                "signal":        "partial_exit_2",
                "ticker":        ticker,
                "name":          s["name"],
                "current_price": current_price,
                "entry_price":   entry,
                "gain_pct":      round(gain_pct, 2),
                "sell_ratio":    PARTIAL_2_RATIO,
                "sell_pct":      int(PARTIAL_2_RATIO * 100),
                "trailing_stop": s["trailing_stop"],
            }

        # ── 트레일링 활성화 체크 (분할 미사용 시 기존 방식) ─
        if not s["trailing_active"] and not s.get("partial_1_done") and current_price >= target:
            s["trailing_active"] = True
            s["trailing_stop"] = int(high * (1 - TRAILING_PCT))
            s["trailing_activated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
            gain_pct = (current_price - entry) / entry * 100
            logger.info(
                f"트레일링 활성화: {s['name']} "
                f"현재가={current_price:,} (+{gain_pct:.1f}%) "
                f"트레일링손절={s['trailing_stop']:,}"
            )
            self._save()
            return {
                "signal": "trailing_activated",
                "ticker": ticker,
                "name": s["name"],
                "current_price": current_price,
                "trailing_stop": s["trailing_stop"],
                "gain_pct": round(gain_pct, 2),
                "high_price": high,
            }

        # ── 매도 신호 체크 ────────────────────────────────
        if s["trailing_active"]:
            # 트레일링 손절 발동
            if current_price <= s["trailing_stop"]:
                gain_pct = (current_price - entry) / entry * 100
                max_pct = (high - entry) / entry * 100
                logger.info(
                    f"트레일링 스탑 발동: {s['name']} "
                    f"현재가={current_price:,} ({gain_pct:+.1f}%) "
                    f"고점={high:,} (+{max_pct:.1f}%)"
                )
                self.remove(ticker)
                return {
                    "signal": "trailing_stop",
                    "ticker": ticker,
                    "name": s["name"],
                    "current_price": current_price,
                    "entry_price": entry,
                    "high_price": high,
                    "gain_pct": round(gain_pct, 2),
                    "max_pct": round(max_pct, 2),
                    "trailing_stop": s["trailing_stop"],
                }
        else:
            # 트레일링 미활성 상태 → 종가 기준 손절
            if current_price <= stop:
                now = datetime.now()
                hour = now.hour
                minute = now.minute
                loss_pct = (current_price - entry) / entry * 100

                # 14:30 이후: 장마감 임박 → 매도 확정
                if hour > 14 or (hour == 14 and minute >= 30):
                    logger.info(f"손절 발동(장마감임박): {s['name']} {loss_pct:.1f}%")
                    self.remove(ticker)
                    return {
                        "signal": "stop_loss",
                        "ticker": ticker,
                        "name": s["name"],
                        "current_price": current_price,
                        "entry_price": entry,
                        "gain_pct": round(loss_pct, 2),
                        "reason": "장마감 임박 손절",
                    }

                # 14:30 이전: 경고만 (아직 매도 X)
                # 이미 경고 보낸 경우 중복 방지
                last_warn = s.get("stop_warned_at", "")
                warn_key = now.strftime("%Y-%m-%d %H")  # 시간 단위로 중복 방지
                if last_warn != warn_key:
                    s["stop_warned_at"] = warn_key
                    self._save()
                    logger.info(f"손절 경고(장중): {s['name']} {loss_pct:.1f}%")
                    return {
                        "signal": "stop_warning",
                        "ticker": ticker,
                        "name": s["name"],
                        "current_price": current_price,
                        "entry_price": entry,
                        "stop_loss": stop,
                        "gain_pct": round(loss_pct, 2),
                    }

        self._save()
        return None

    def check_all(self, get_price_fn) -> List[dict]:
        """
        전체 보유 종목 트레일링 체크

        Args:
            get_price_fn: ticker → current_price 함수

        Returns:
            매도 신호 리스트
        """
        signals = []
        today = datetime.now().strftime("%Y-%m-%d")

        # 오늘 등록된 종목만 체크
        today_stops = {
            t: s for t, s in self.stops.items()
            if s.get("date") == today
        }

        for ticker, s in today_stops.items():
            try:
                price = get_price_fn(ticker)
                if price and price > 0:
                    signal = self.update_and_check(ticker, price)
                    if signal:
                        signals.append(signal)
            except Exception as e:
                logger.debug(f"트레일링 체크 실패 ({ticker}): {e}")

        return signals

    def remove(self, ticker: str):
        """종목 제거"""
        if ticker in self.stops:
            del self.stops[ticker]
            self._save()

    def clear_old(self):
        """전일 종목 정리"""
        today = datetime.now().strftime("%Y-%m-%d")
        old_tickers = [
            t for t, s in self.stops.items()
            if s.get("date") != today
        ]
        for t in old_tickers:
            del self.stops[t]
        if old_tickers:
            self._save()
            logger.info(f"전일 트레일링 {len(old_tickers)}개 정리")

    def check_closing_stops(self, get_price_fn) -> List[dict]:
        """
        장마감(15:30) 후 종가 기준 손절 최종 체크
        종가 < 손절가인 종목 → 내일 시초가 매도 권고
        """
        signals = []
        today = datetime.now().strftime("%Y-%m-%d")
        today_stops = {t: s for t, s in self.stops.items()
                       if s.get("date") == today}

        for ticker, s in today_stops.items():
            try:
                close_price = get_price_fn(ticker)
                stop = s["stop_loss"]
                entry = s["entry_price"]

                if close_price <= stop:
                    loss_pct = (close_price - entry) / entry * 100
                    logger.info(
                        f"종가 손절 확정: {s['name']} "
                        f"종가={close_price:,} 손절가={stop:,} ({loss_pct:.1f}%)"
                    )
                    signals.append({
                        "signal": "stop_loss_close",
                        "ticker": ticker,
                        "name": s["name"],
                        "current_price": close_price,
                        "entry_price": entry,
                        "stop_loss": stop,
                        "gain_pct": round(loss_pct, 2),
                    })
                    self.remove(ticker)
            except Exception as e:
                logger.debug(f"종가 손절 체크 실패 ({ticker}): {e}")

        return signals

    def get_status(self) -> List[dict]:
        """현재 트레일링 현황"""
        today = datetime.now().strftime("%Y-%m-%d")
        return [
            s for s in self.stops.values()
            if s.get("date") == today
        ]

    # ── 내부 ─────────────────────────────────────────────

    def _load(self) -> dict:
        if TRAILING_FILE.exists():
            try:
                with open(TRAILING_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        TRAILING_FILE.parent.mkdir(exist_ok=True)
        with open(TRAILING_FILE, "w") as f:
            json.dump(self.stops, f, ensure_ascii=False, indent=2)


# 싱글톤
_manager = None

def get_trailing_manager() -> TrailingStopManager:
    global _manager
    if _manager is None:
        _manager = TrailingStopManager()
    return _manager
