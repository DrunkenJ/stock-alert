"""청산 정책 단일 정의 (분할 익절 / 트레일링)

왜 이 모듈이 있는가
-------------------
같은 청산 규칙이 두 곳에 각각 하드코딩돼 있었고, 서로 값이 달랐다.

  trailing_stop.py  (실거래 알림)  partial_1 = max(4%, 목표가상승률 × 0.5)
  trade_simulator.py (학습용 시뮬)  partial_1 = 4% 고정

시뮬레이션 이력(trade_history.json)은 failure_analyzer → learned_rules.json 을
거쳐 다음 스크리닝에 반영된다. 즉 봇이 **실제로 실행하지 않는 전략**의 성과를
학습하고 있었다. 목표가 +14%짜리 종목이면 실거래는 7%/14%에서 분할 매도하는데
시뮬 장부에는 4%/8%로 기록되는 식이다.

→ 청산 임계값과 분할 익절가 계산을 이 모듈에서만 정의하고 양쪽이 import 한다.
"""

import os

# ── 분할 익절 하한 (저변동성 종목의 빠른 익절 습관 유지용) ──
PARTIAL_1_PCT   = 0.04   # 1차 익절 최소: +4%
PARTIAL_2_PCT   = 0.08   # 2차 익절 최소: +8%

# ── 트레일링 (고정 모드) ──
TRAILING_PCT = 0.03      # 고점 대비 -3% 하락 시 잔여 매도

# ── 고정 모드 손절 캡 ──
FIXED_STOP_CAP_PCT = 0.07
FIXED_STOP_MIN_PCT = 0.02


# ═══════════════════════════════════════════════════════════
# ATR 연동 모드 (ATR_EXIT_MODE=on 일 때만 활성 / 기본 off)
# ═══════════════════════════════════════════════════════════
# 왜 필요한가 - 실거래 80건 측정 결과:
#   · 손절 7% 캡이 80/80건(100%)에 걸려, ATR 기반 동적 손절과
#     점수별 R:R 테이블, 시간대별 손절 조정이 전부 죽은 코드가 됐다.
#   · 후보 ATR 중앙값 9.6% 기준으로 모든 임계값이 1 ATR 안쪽이다.
#       트레일링 -3% = 0.31 ATR / 1차 익절 +4% = 0.42 ATR
#       손절 -7%    = 0.73 ATR / 2차 익절 +8% = 0.83 ATR
#     → 진입도 청산도 전부 일상적 등락(노이즈) 안에서 일어난다.
#   · 승자의 80%가 1 ATR 안에서 종료되는 반면 패자는 100% 포지션이
#     손절을 그대로 받는다. 손익분기 승률 51.9% vs 실제 49% → 기대값 -0.373%/건.
#
# 주의 - 이 모드는 아직 검증되지 않았다. 구 진입 기준으로 돌린 exit 스윕에서는
# 손절/익절을 넓히는 모든 변형이 단조적으로 악화됐다(진입 알파가 음수였기 때문).
# 진입부를 교체한 뒤 run_backtest.py 로 재검증하기 전에는 켜지 말 것.
ATR_EXIT_MODE = os.getenv("ATR_EXIT_MODE", "off").lower() in ("on", "true", "1")

ATR_STOP_MULT    = float(os.getenv("ATR_STOP_MULT", "1.2"))    # 손절 = 1.2 ATR
ATR_STOP_MIN_PCT = float(os.getenv("ATR_STOP_MIN_PCT", "5.0")) / 100
ATR_STOP_MAX_PCT = float(os.getenv("ATR_STOP_MAX_PCT", "10.0")) / 100
ATR_P1_MULT      = float(os.getenv("ATR_P1_MULT", "1.0"))      # 1차 익절 = 1.0 ATR
ATR_P2_MULT      = float(os.getenv("ATR_P2_MULT", "2.0"))      # 2차 익절 = 2.0 ATR
ATR_TRAIL_MULT   = float(os.getenv("ATR_TRAIL_MULT", "2.5"))   # 트레일링 = 고점 -2.5 ATR
ATR_TRAIL_MAX_PCT = float(os.getenv("ATR_TRAIL_MAX_PCT", "15.0")) / 100  # 트레일링 폭 상한

# 익절폭 최소 하한 (왕복 거래비용 0.3%p 대비 의미 있는 폭 보장)
MIN_PARTIAL_PCT = 0.02

# 분할 매도 비율 - ATR 모드에서는 잔량을 20%→34%로 키운다.
# 트레일링이 2.5 ATR로 넓어져야 큰 추세를 끝까지 들고 갈 수 있고,
# 그러려면 트레일링이 관리하는 잔량 자체가 의미 있는 크기여야 한다.
if ATR_EXIT_MODE:
    PARTIAL_1_RATIO = float(os.getenv("ATR_P1_RATIO", "0.33"))
    PARTIAL_2_RATIO = float(os.getenv("ATR_P2_RATIO", "0.33"))
else:
    PARTIAL_1_RATIO = 0.5
    PARTIAL_2_RATIO = 0.3


def stop_cap_pct() -> float:
    """현재 모드의 손절 상한(%) - 변동성 밴드 실링 계산에 쓰인다"""
    return (ATR_STOP_MAX_PCT if ATR_EXIT_MODE else FIXED_STOP_CAP_PCT) * 100


def calc_stop_distance(entry_price: int, atr: float, stop_mult: float) -> float:
    """손절폭(원) 계산

    고정 모드: clip(stop_mult × ATR, 진입가 2%, 진입가 7%)  ← 현행 유지
    ATR 모드 : clip(1.2 × ATR,      진입가 5%, 진입가 10%)
    """
    if not entry_price or atr <= 0:
        return 0.0

    if ATR_EXIT_MODE:
        raw = ATR_STOP_MULT * atr
        lo, hi = entry_price * ATR_STOP_MIN_PCT, entry_price * ATR_STOP_MAX_PCT
    else:
        raw = stop_mult * atr
        lo, hi = entry_price * FIXED_STOP_MIN_PCT, entry_price * FIXED_STOP_CAP_PCT

    return max(lo, min(raw, hi))


def calc_trailing_stop(high_price: int, atr: float = 0,
                       entry_price: int = 0) -> int:
    """트레일링 손절가

    고정 모드: 고점 × (1 - 3%)
    ATR 모드 : 고점 - 2.5 ATR (샹들리에), 단 두 가지 안전장치를 건다.

      ① 트레일링 폭 상한 - 초고변동성 종목에서 2.5 ATR 이 무한정 벌어지는 것을 막는다.
         (ATR 15% 종목이면 2.5 ATR = 고점 대비 37.5%. 사실상 트레일링이 없는 것과 같다)
      ② 본전 하한 - 이게 없으면 익절 후 잔량이 손실로 뒤집힌다.
         ATR 9.6% 종목이 +20%까지 올라도 트레일링가는 -4%가 되어버린다.
         1차 익절 시점에 손절가는 이미 진입가로 상향되므로 그 규칙과도 일치한다.
    """
    if ATR_EXIT_MODE and atr > 0:
        dist = min(ATR_TRAIL_MULT * atr, high_price * ATR_TRAIL_MAX_PCT)
        stop = int(high_price - dist)
    else:
        stop = int(high_price * (1 - TRAILING_PCT))

    if entry_price:
        stop = max(stop, entry_price)
    return stop


def calc_partial_prices(entry_price: int, target_price: int,
                        atr: float = 0) -> tuple[int, int]:
    """분할 익절가 계산 (실거래·시뮬 공통)

    고정 모드: entry_calculator 가 산출한 target_price 에 비례.
              (고정 4%/8%만 쓰면 손절은 ATR 따라 -7%까지 벌어지는데 익절은
               묶여 있어 변동성 큰 종목일수록 손익비가 무너진다. 하한으로만 사용)
    ATR 모드 : 1.0 ATR / 2.0 ATR. target_price 를 거치지 않으므로
              손절 캡에 눌려 익절폭까지 고정되던 연쇄가 끊긴다.

    Returns: (1차 익절가, 2차 익절가)
    """
    if not entry_price:
        return 0, 0

    if ATR_EXIT_MODE and atr > 0:
        p1 = max(ATR_P1_MULT * atr / entry_price, MIN_PARTIAL_PCT)
        p2 = max(ATR_P2_MULT * atr / entry_price, MIN_PARTIAL_PCT * 2)
    else:
        target_upside = (target_price - entry_price) / entry_price if target_price else 0.0
        p1 = max(PARTIAL_1_PCT, target_upside * 0.5)
        p2 = max(PARTIAL_2_PCT, target_upside)

    return int(entry_price * (1 + p1)), int(entry_price * (1 + p2))


def realized_pct_at(entry_price: int, exit_price: int) -> float:
    """진입가 대비 실현 수익률(%)

    분할 익절 손익을 상수(4%/8%)로 적으면 익절가가 목표가에 연동돼 움직일 때
    장부가 실제와 어긋난다. 반드시 체결가 기준으로 계산한다.
    """
    if not entry_price:
        return 0.0
    return (exit_price - entry_price) / entry_price * 100


def simulate_exit(entry_price: int, atr: float, stop_loss: int,
                  target_price: int, path: list[dict],
                  cost_pct: float = 0.3, max_hold_days: int = 7) -> dict:
    """진입 이후 일봉 경로로 청산을 시뮬레이션한다 (분할 익절 + 트레일링 + 손절)

    백테스트가 실제 청산 정책을 그대로 재현하기 위한 함수.
    엔진에는 원래 단순 -3%/+6% 로직만 있어서, 분할 익절·트레일링·본전 손절 상향이
    전부 빠져 있었다. 그 상태로는 ATR 연동 모드를 검증할 수 없다.

    trade_simulator._simulate_day 와 동일한 규칙을 따른다:
      · 저점이 손절가 이하 → 잔여 전량 청산
      · 고점이 1차 익절가 이상 → 비율만큼 실현 + 손절가를 진입가로 상향
      · 고점이 2차 익절가 이상 → 비율만큼 실현 + 트레일링 활성화
      · 트레일링 활성 후 저점이 트레일링가 이하 → 잔여 청산
      · max_hold_days 초과 → 종가 청산
    거래비용은 청산 시 1회 차감한다.

    Args:
        path: 진입 다음날부터의 일봉 [{high, low, close}, ...]
    Returns:
        realized_pct / close_reason / holding_days / max_favorable_pct
    """
    if not entry_price or not path:
        return {"realized_pct": 0.0, "close_reason": "no_data",
                "holding_days": 0, "max_favorable_pct": 0.0}

    p1, p2 = calc_partial_prices(entry_price, target_price, atr)
    stop = stop_loss
    high_water = entry_price
    realized = 0.0
    p1_done = p2_done = False
    trailing_active = False
    trailing_stop = 0

    def _close(exit_price: int, reason: str, day: int) -> dict:
        remaining = 1.0 - (PARTIAL_1_RATIO if p1_done else 0.0) \
                        - (PARTIAL_2_RATIO if p2_done else 0.0)
        total = realized + realized_pct_at(entry_price, exit_price) * remaining - cost_pct
        return {
            "realized_pct":      round(total, 3),
            "close_reason":      reason,
            "holding_days":      day,
            "max_favorable_pct": round(realized_pct_at(entry_price, high_water), 2),
        }

    for day, c in enumerate(path[:max_hold_days], start=1):
        high, low, close = c["high"], c["low"], c["close"]

        if high > high_water:
            high_water = high
            if trailing_active:
                trailing_stop = calc_trailing_stop(high_water, atr, entry_price)

        # 손절이 익절보다 먼저 (같은 날 둘 다 닿으면 보수적으로 손절 처리)
        if not trailing_active and low <= stop:
            return _close(stop, "stop_loss", day)

        if not p1_done and high >= p1:
            p1_done = True
            realized += realized_pct_at(entry_price, p1) * PARTIAL_1_RATIO
            stop = entry_price          # 본전 손절로 상향

        if p1_done and not p2_done and high >= p2:
            p2_done = True
            realized += realized_pct_at(entry_price, p2) * PARTIAL_2_RATIO
            trailing_active = True
            trailing_stop = calc_trailing_stop(max(high_water, p2), atr, entry_price)

        if trailing_active and low <= trailing_stop:
            return _close(trailing_stop, "trailing_stop", day)

    last = path[min(len(path), max_hold_days) - 1]
    return _close(last["close"], "max_hold", min(len(path), max_hold_days))
