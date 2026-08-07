"""
시뮬레이션 매매 기록 시스템
- 추천 종목을 가상 포트폴리오에 자동 등록
- 일별 가격 기반으로 분할 익절/트레일링/손절 시뮬레이션
- 실제 청산 시점과 수익률 자동 계산
- 주간 리뷰에서 정밀 분석에 활용

기록 파일:
- data/trade_history.json    : 청산 완료 거래 (장기 보관)
- data/active_trades.json    : 진행 중 거래
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


ACTIVE_FILE  = Path("data/active_trades.json")
HISTORY_FILE = Path("data/trade_history.json")

# 시뮬레이션 파라미터 (실제 시스템과 동일)
from src.utils.exit_policy import (  # noqa: F401
    PARTIAL_1_PCT, PARTIAL_2_PCT, PARTIAL_1_RATIO, PARTIAL_2_RATIO,
    TRAILING_PCT, calc_partial_prices, calc_trailing_stop, realized_pct_at,
)

MAX_HOLD_DAYS  = 7
COST_PCT       = 0.3   # 왕복 거래비용 %p (수수료+거래세+슬리피지) - 청산 시 1회 차감


class TradeSimulator:
    """가상 매매 시뮬레이터"""

    def __init__(self):
        self.active  = self._load(ACTIVE_FILE)
        self.history = self._load(HISTORY_FILE)

    def register_picks(self, picks: list[dict], regime: str = "unknown"):
        """09:10 추천 시 자동 등록"""
        today = datetime.now().strftime("%Y-%m-%d")
        registered = 0

        for pick in picks:
            ticker = pick.get("ticker", "")
            if not ticker:
                continue

            # 이미 활성 거래 있으면 스킵
            if ticker in self.active:
                continue

            es = pick.get("entry_strategy", {})
            entry_price  = es.get("split_1st", pick.get("price", 0))
            target_price = es.get("target_price", int(entry_price * 1.06))
            stop_loss    = es.get("stop_loss",    int(entry_price * 0.97))
            atr          = es.get("atr", 0)
            _p1, _p2     = calc_partial_prices(entry_price, target_price, atr)

            self.active[ticker] = {
                "ticker":           ticker,
                "name":             pick.get("name", ""),
                "entry_date":       today,
                "entry_price":      entry_price,
                "target_price":     target_price,
                "stop_loss":        stop_loss,
                "atr":              atr,
                "high_price":       entry_price,
                "regime":           regime,
                "sector":           pick.get("sector", ""),
                "score":            pick.get("final_score", 0),
                "score_confidence": pick.get("score_confidence", ""),
                "ai_score":         pick.get("ai_eval", {}).get("ai_score", 0),
                # 분할 익절 상태 (실거래 trailing_stop.add() 와 동일 계산)
                "partial_1_price":  _p1,
                "partial_2_price":  _p2,
                "partial_1_done":   False,
                "partial_2_done":   False,
                "partial_1_date":   None,
                "partial_2_date":   None,
                # 트레일링 상태
                "trailing_active":  False,
                "trailing_stop":    0,
                # 누적 수익
                "realized_pct":     0.0,
                "events":           [],
            }
            registered += 1

        self._save(ACTIVE_FILE, self.active)
        logger.info(f"시뮬레이션 등록: {registered}종목")
        return registered

    def update_daily(self, kis_client) -> list[dict]:
        """
        매일 16:00 마감 후 호출
        모든 활성 거래를 오늘 캔들로 업데이트
        Returns: 청산 완료된 거래 리스트
        """
        if not self.active:
            return []

        today = datetime.now().strftime("%Y-%m-%d")
        closed_today = []

        for ticker in list(self.active.keys()):
            trade = self.active[ticker]

            try:
                # 오늘 캔들
                candles = kis_client.get_daily_ohlcv(ticker, days=2)
                if not candles:
                    continue
                today_candle = candles[-1]

                high  = today_candle["high"]
                low   = today_candle["low"]
                close = today_candle["close"]

                # 시뮬레이션 진행
                result = self._simulate_day(trade, high, low, close, today)

                # 청산 완료 시 history로 이동
                if result.get("closed"):
                    closed_today.append(self.active[ticker])
                    self.history.setdefault(today, []).append(self.active[ticker])
                    del self.active[ticker]

            except Exception as e:
                logger.debug(f"시뮬레이션 업데이트 오류 ({ticker}): {e}")

        # 최대 보유일 초과 강제 청산
        for ticker in list(self.active.keys()):
            trade = self.active[ticker]
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d")
            days_held = (datetime.now() - entry_date).days

            if days_held >= MAX_HOLD_DAYS:
                # 마감일 강제 청산
                try:
                    price = kis_client.get_stock_price(ticker)["price"]
                    remaining = 1.0 - (PARTIAL_1_RATIO if trade["partial_1_done"] else 0) \
                                    - (PARTIAL_2_RATIO if trade["partial_2_done"] else 0)
                    pct = (price - trade["entry_price"]) / trade["entry_price"] * 100
                    trade["realized_pct"] += pct * remaining
                    trade["realized_pct"] -= COST_PCT
                    trade["close_date"] = today
                    trade["close_reason"] = "max_hold"
                    trade["events"].append({
                        "date": today,
                        "event": "강제 청산 (보유일 초과)",
                        "price": price,
                        "pct": round(pct, 2),
                    })
                    closed_today.append(trade)
                    self.history.setdefault(today, []).append(trade)
                    del self.active[ticker]
                except Exception:
                    pass

        self._save(ACTIVE_FILE, self.active)
        self._save(HISTORY_FILE, self.history)
        return closed_today

    def _simulate_day(self, trade: dict, high: int, low: int,
                      close: int, today: str) -> dict:
        """하루 시뮬레이션 (장중 가격 변동 반영)"""
        entry = trade["entry_price"]

        # 고점 갱신
        if high > trade["high_price"]:
            trade["high_price"] = high
            if trade["trailing_active"]:
                trade["trailing_stop"] = calc_trailing_stop(high, trade.get("atr", 0), entry)

        # 손절 체크 (트레일링 미활성 + 저점이 손절가 이하)
        if not trade["trailing_active"] and low <= trade["stop_loss"]:
            # 손절 발동
            remaining = 1.0 - (PARTIAL_1_RATIO if trade["partial_1_done"] else 0)
            pct = (trade["stop_loss"] - entry) / entry * 100
            trade["realized_pct"] += pct * remaining
            trade["realized_pct"] -= COST_PCT
            trade["close_date"]   = today
            trade["close_reason"] = "stop_loss"
            trade["events"].append({
                "date": today,
                "event": "손절",
                "price": trade["stop_loss"],
                "pct": round(pct, 2),
            })
            return {"closed": True}

        # 1차 익절 (+4%)
        if not trade["partial_1_done"] and high >= trade["partial_1_price"]:
            trade["partial_1_done"] = True
            trade["partial_1_date"] = today
            # 익절가가 목표가에 연동돼 움직이므로 상수(4%)가 아닌 체결가 기준으로 기록
            pct = realized_pct_at(entry, trade["partial_1_price"])
            trade["realized_pct"] += pct * PARTIAL_1_RATIO
            trade["stop_loss"]     = entry  # 본전 손절로 상향
            trade["events"].append({
                "date":  today,
                "event": "1차 익절 (50% 매도)",
                "price": trade["partial_1_price"],
                "pct":   round(pct, 2),
            })

        # 2차 익절 (+8%)
        if trade["partial_1_done"] and not trade["partial_2_done"] \
                and high >= trade["partial_2_price"]:
            trade["partial_2_done"] = True
            trade["partial_2_date"] = today
            pct = realized_pct_at(entry, trade["partial_2_price"])
            trade["realized_pct"]  += pct * PARTIAL_2_RATIO
            trade["trailing_active"] = True
            trade["trailing_stop"]   = calc_trailing_stop(trade["partial_2_price"], trade.get("atr", 0), entry)
            trade["events"].append({
                "date":  today,
                "event": "2차 익절 (30% 매도)",
                "price": trade["partial_2_price"],
                "pct":   round(pct, 2),
            })

        # 트레일링 스탑 발동
        if trade["trailing_active"] and low <= trade["trailing_stop"]:
            remaining = max(0.0, 1.0 - PARTIAL_1_RATIO - PARTIAL_2_RATIO)
            pct = (trade["trailing_stop"] - entry) / entry * 100
            trade["realized_pct"]  += pct * remaining
            trade["realized_pct"]  -= COST_PCT
            trade["close_date"]    = today
            trade["close_reason"]  = "trailing_stop"
            trade["high_at_close"] = trade["high_price"]
            trade["events"].append({
                "date":  today,
                "event": "트레일링 스탑 (잔여 20% 매도)",
                "price": trade["trailing_stop"],
                "pct":   round(pct, 2),
            })
            return {"closed": True}

        return {"closed": False}

    def get_statistics(self, days: int = 30) -> dict:
        """최근 N일 통계 (주간 리뷰용)"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        recent = []
        for date, trades in self.history.items():
            if date >= cutoff:
                recent.extend(trades)

        if not recent:
            return {"trade_count": 0, "message": "데이터 부족"}

        # 기본 통계
        total = len(recent)
        wins  = [t for t in recent if t["realized_pct"] > 0]
        losses = [t for t in recent if t["realized_pct"] <= 0]
        win_rate = len(wins) / total * 100

        avg_win  = sum(t["realized_pct"] for t in wins)  / len(wins)   if wins   else 0
        avg_loss = sum(t["realized_pct"] for t in losses)/ len(losses) if losses else 0
        avg_all  = sum(t["realized_pct"] for t in recent) / total

        # 청산 사유별
        close_reasons = {}
        for t in recent:
            r = t.get("close_reason", "unknown")
            close_reasons[r] = close_reasons.get(r, 0) + 1

        # 분할 익절 도달률
        partial_1_count = sum(1 for t in recent if t.get("partial_1_done"))
        partial_2_count = sum(1 for t in recent if t.get("partial_2_done"))

        # 국면별 통계
        regime_stats = {}
        for t in recent:
            r = t.get("regime", "unknown")
            regime_stats.setdefault(r, []).append(t["realized_pct"])

        regime_summary = {}
        for r, pcts in regime_stats.items():
            regime_summary[r] = {
                "count":    len(pcts),
                "avg_pct":  round(sum(pcts) / len(pcts), 2),
                "win_rate": round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1),
            }

        # 섹터별 통계
        sector_stats = {}
        for t in recent:
            s = t.get("sector", "기타")
            sector_stats.setdefault(s, []).append(t["realized_pct"])

        sector_summary = {}
        for s, pcts in sector_stats.items():
            sector_summary[s] = {
                "count":    len(pcts),
                "avg_pct":  round(sum(pcts) / len(pcts), 2),
                "win_rate": round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1),
            }

        # 점수대별 통계
        score_buckets = {"7.0+": [], "5.0-7.0": [], "4.0-5.0": [], "4.0 미만": []}
        for t in recent:
            score = t.get("score", 0)
            if score >= 7.0:
                bucket = "7.0+"
            elif score >= 5.0:
                bucket = "5.0-7.0"
            elif score >= 4.0:
                bucket = "4.0-5.0"
            else:
                bucket = "4.0 미만"
            score_buckets[bucket].append(t["realized_pct"])

        score_summary = {}
        for b, pcts in score_buckets.items():
            if pcts:
                score_summary[b] = {
                    "count":    len(pcts),
                    "avg_pct":  round(sum(pcts) / len(pcts), 2),
                    "win_rate": round(sum(1 for p in pcts if p > 0) / len(pcts) * 100, 1),
                }

        # 손익비 (Profit Factor)
        total_win  = sum(t["realized_pct"] for t in wins)
        total_loss = abs(sum(t["realized_pct"] for t in losses))
        profit_factor = total_win / total_loss if total_loss > 0 else 0

        return {
            "trade_count":     total,
            "win_rate":        round(win_rate, 1),
            "avg_pct":         round(avg_all, 2),
            "avg_win":         round(avg_win, 2),
            "avg_loss":        round(avg_loss, 2),
            "profit_factor":   round(profit_factor, 2),
            "close_reasons":   close_reasons,
            "partial_1_rate":  round(partial_1_count / total * 100, 1),
            "partial_2_rate":  round(partial_2_count / total * 100, 1),
            "regime_stats":    regime_summary,
            "sector_stats":    sector_summary,
            "score_stats":     score_summary,
            "active_count":    len(self.active),
        }

    def _load(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self, path: Path, data: dict):
        path.parent.mkdir(exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# 싱글톤
_simulator = None

def get_simulator() -> TradeSimulator:
    global _simulator
    if _simulator is None:
        _simulator = TradeSimulator()
    return _simulator
