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

# ── 분할 익절 하한 (저변동성 종목의 빠른 익절 습관 유지용) ──
PARTIAL_1_PCT   = 0.04   # 1차 익절 최소: +4%
PARTIAL_2_PCT   = 0.08   # 2차 익절 최소: +8%
PARTIAL_1_RATIO = 0.5    # 1차 매도 비율
PARTIAL_2_RATIO = 0.3    # 2차 매도 비율

# ── 트레일링 ──
TRAILING_PCT = 0.03      # 고점 대비 -3% 하락 시 잔여 매도


def calc_partial_prices(entry_price: int, target_price: int) -> tuple[int, int]:
    """분할 익절가 계산 (실거래·시뮬 공통)

    entry_calculator 가 종목별 ATR/점수로 산출한 target_price 에 비례해 익절폭을
    정한다. 고정 4%/8%만 쓰면, 손절폭은 ATR을 따라 최대 -7%까지 벌어지는데
    익절은 묶여 있어 변동성이 큰 종목일수록 손익비가 무너진다.
    고정값은 하한으로만 쓴다.

    Returns: (1차 익절가, 2차 익절가)
    """
    if not entry_price:
        return 0, 0

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
