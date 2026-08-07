"""
매수가 계산 모듈
- 갭상승률 체크 (고점 매수 방지)
- 지정가 매수가 계산 (슬리피지 방지)
- 분할매수 3단계 진입가 제시 (평단 최적화)
"""
from loguru import logger


# 파라미터
GAP_WARNING_PCT = 3.0    # 갭상승 경고 기준 (%)
GAP_REJECT_PCT = 7.0     # 갭상승 매수 보류 기준 (%)
LIMIT_ORDER_DISC = 0.003  # 지정가 할인율 (시가 대비 -0.3%)
SPLIT_2ND_DISC = 0.015   # 2차 매수 할인율 (-1.5%)
SPLIT_3RD_DISC = 0.030   # 3차 매수 할인율 (-3.0%)
SPLIT_RATIO = (0.5, 0.3, 0.2)  # 1차/2차/3차 배분 비율


def calculate_entry(pick: dict, candles: list[dict] = None) -> dict:
    """
    단일 종목의 매수 전략 계산

    Args:
        pick: 스크리너 결과 (price, open_price, indicators 포함)
        candles: 일봉 데이터 (ATR 계산용, 없으면 고정값 사용)

    Returns:
        entry_strategy 딕셔너리 추가된 pick
    """
    pick = dict(pick)
    price = pick.get("price", 0)
    open_price = pick.get("open_price", price)
    prev_close = _estimate_prev_close(pick)

    if price <= 0:
        pick["entry_strategy"] = _default_strategy(price)
        return pick

    # ── 1. 갭 분석 ──────────────────────────────────────
    gap_pct = 0.0
    if prev_close > 0:
        gap_pct = (open_price - prev_close) / prev_close * 100
    gap_status = _classify_gap(gap_pct)

    # ── 2. 호가 단위 ─────────────────────────────────────
    tick = _get_tick_size(price)

    # ── 3. 지정가 매수가 계산 ────────────────────────────
    if gap_status == "reject":
        base = prev_close if prev_close > 0 else price
        disc = LIMIT_ORDER_DISC * 2
    elif gap_status == "warning":
        base = open_price
        disc = LIMIT_ORDER_DISC * 1.5
    else:
        base = price
        disc = LIMIT_ORDER_DISC

    limit_price = _round_to_tick(base * (1 - disc), tick)

    # ── 4. 분할매수 진입가 ───────────────────────────────
    split_1st = limit_price
    split_2nd = _round_to_tick(split_1st * (1 - SPLIT_2ND_DISC), tick)
    split_3rd = _round_to_tick(split_1st * (1 - SPLIT_3RD_DISC), tick)

    # ── 5. ATR 기반 동적 손절/익절 ───────────────────────
    if candles is None:
        candles = pick.get("_candles", [])

    atr = calculate_atr(candles) if candles else 0.0

    # [약점 6번] 점수별 R:R 동적 조정
    final_score = pick.get("final_score", 5.0)
    stop_mult, target_mult = _get_rr_by_score(final_score)

    # [약점 8번] 시간대별 변동성 조정 (오전 손절폭 확대)
    stop_mult = _adjust_stop_by_time(stop_mult)

    atr_stops = calculate_dynamic_stops(
        split_1st, atr, tick=tick,
        atr_stop_mult=stop_mult,
        atr_target_mult=target_mult,
    )

    target = atr_stops["target_price"]
    stop_loss = atr_stops["stop_loss"]
    upside = atr_stops["target_pct"]
    downside = atr_stops["stop_pct"]

    logger.debug(
        f"{pick.get('name','')} ATR={atr:.0f}원 "
        f"손절={stop_loss:,}원({downside:.1f}%) "
        f"익절={target:,}원({upside:.1f}%) "
        f"R:R={atr_stops['rr_ratio']:.1f}"
    )

    entry_strategy = {
        # 갭 분석
        "gap_pct": round(gap_pct, 2),
        "gap_status": gap_status,
        "gap_emoji": _gap_emoji(gap_status),
        "gap_message": _gap_message(gap_status, gap_pct),

        # 지정가 매수가
        "limit_price": limit_price,
        "tick_size": tick,

        # 분할매수
        "split_1st": split_1st,
        "split_2nd": split_2nd,
        "split_3rd": split_3rd,
        "split_ratio": SPLIT_RATIO,

        # ATR 기반 동적 손절/익절
        "target_price": target,
        "stop_loss": stop_loss,
        "upside_pct": round(upside, 2),
        "downside_pct": round(downside, 2),
        "atr": atr_stops["atr"],
        "rr_ratio": atr_stops["rr_ratio"],
        "is_atr_based": atr > 0,
        "stop_mult": stop_mult,
        "target_mult": target_mult,

        # 매수 가능 여부
        "buyable": gap_status != "reject",
    }

    pick["entry_strategy"] = entry_strategy

    # ai_eval 목표가도 업데이트
    if "ai_eval" in pick:
        pick["ai_eval"]["target_price"] = target
        pick["ai_eval"]["stop_loss"] = stop_loss

    logger.debug(
        f"{pick.get('name', '')} 매수전략: "
        f"갭{gap_pct:+.1f}%({gap_status}) "
        f"지정가={limit_price:,} "
        f"1차={split_1st:,}/2차={split_2nd:,}/3차={split_3rd:,}"
    )

    return pick


def calculate_entries(picks: list[dict]) -> list[dict]:
    """전체 추천 종목 매수 전략 계산"""
    result = []
    for pick in picks:
        try:
            result.append(calculate_entry(pick))
        except Exception as e:
            logger.warning(f"매수가 계산 오류 ({pick.get('name', '')}): {e}")
            result.append(pick)
    return result


# ── 내부 헬퍼 함수 ────────────────────────────────────────

def _estimate_prev_close(pick: dict) -> float:
    """전일 종가 추정 (현재가 + change_rate 역산)"""
    price = pick.get("price", 0)
    change_rate = pick.get("change_rate", 0)
    if price <= 0 or change_rate == 0:
        return price
    return price / (1 + change_rate / 100)


def _classify_gap(gap_pct: float) -> str:
    """갭 상승률 분류"""
    if gap_pct >= GAP_REJECT_PCT:
        return "reject"    # 매수 보류
    elif gap_pct >= GAP_WARNING_PCT:
        return "warning"   # 주의
    elif gap_pct <= -GAP_WARNING_PCT:
        return "gap_down"  # 갭하락 (매수 기회)
    else:
        return "normal"    # 정상


def _gap_emoji(status: str) -> str:
    return {
        "reject":   "🚫",
        "warning":  "⚠️",
        "gap_down": "💙",
        "normal":   "✅",
    }.get(status, "✅")


def _gap_message(status: str, gap_pct: float) -> str:
    if status == "reject":
        return f"갭상승 {gap_pct:+.1f}% → 매수 보류 권장 (과열)"
    elif status == "warning":
        return f"갭상승 {gap_pct:+.1f}% → 지정가 신중 매수"
    elif status == "gap_down":
        return f"갭하락 {gap_pct:+.1f}% → 분할매수 기회"
    else:
        return f"갭 {gap_pct:+.1f}% → 정상 매수 가능"


def _get_tick_size(price: int) -> int:
    """KRX 호가 단위"""
    if price < 1000:
        return 1
    elif price < 5000:
        return 5
    elif price < 10000:
        return 10
    elif price < 50000:
        return 50
    elif price < 100000:
        return 100
    elif price < 500000:
        return 500
    else:
        return 1000


def _round_to_tick(price: float, tick: int) -> int:
    """호가 단위로 반올림"""
    return int(round(price / tick) * tick)


def _get_rr_by_score(score: float) -> tuple[float, float]:
    """
    [약점 6번] 점수별 동적 R:R 조정
    - 고점수(확신 높음): 손익비 낮아도 됨 → 손절 타이트, 익절 가까이
    - 저점수(확신 낮음): 손익비 높아야 함 → 손절 넓게, 익절 멀리

    Returns: (stop_multiplier, target_multiplier)
    """
    if score >= 8.0:
        # 고확신 → R:R 1.3 (손절 타이트, 빠른 익절)
        return 1.5, 2.0
    elif score >= 7.0:
        # 중상 → R:R 1.5 (기본)
        return 2.0, 3.0
    elif score >= 6.0:
        # 중간 → R:R 2.0 (익절 더 멀리)
        return 2.0, 4.0
    else:
        # 저확신 → R:R 2.5 (큰 손익비로 보상)
        return 1.5, 3.5


def _adjust_stop_by_time(stop_mult: float) -> float:
    """
    [약점 8번] 시간대별 변동성 조정
    - 09:10~10:00: 오전 변동성 큼 → 손절폭 30% 확대
    - 10:00~14:00: 정상
    - 14:00~15:20: 마감 임박 → 손절폭 10% 축소 (빠른 청산)
    """
    from datetime import datetime
    hour = datetime.now().hour
    minute = datetime.now().minute

    if hour == 9 or (hour == 10 and minute < 30):
        # 오전 변동성 구간: 손절 멀게 (노이즈 방지)
        return stop_mult * 1.3
    elif hour >= 14 and minute >= 30:
        # 마감 임박: 손절 타이트하게
        return stop_mult * 0.9
    else:
        return stop_mult


def _default_strategy(price: int) -> dict:
    return {
        "gap_pct": 0,
        "gap_status": "normal",
        "gap_emoji": "✅",
        "gap_message": "정상",
        "limit_price": price,
        "tick_size": _get_tick_size(price),
        "split_1st": price,
        "split_2nd": int(price * 0.985),
        "split_3rd": int(price * 0.970),
        "split_ratio": SPLIT_RATIO,
        "target_price": int(price * 1.06),
        "stop_loss": int(price * 0.97),
        "upside_pct": 6.0,
        "downside_pct": -3.0,
        "buyable": True,
    }


def calculate_atr(candles: list[dict], period: int = 14) -> float:
    """
    ATR(Average True Range) 계산
    TR = max(고가-저가, |고가-전일종가|, |저가-전일종가|)
    ATR = TR의 period일 평균
    """
    if len(candles) < period + 1:
        # 데이터 부족 시 최근 5일 고저 평균으로 대체
        if len(candles) >= 2:
            ranges = [c["high"] - c["low"] for c in candles[-5:]]
            return sum(ranges) / len(ranges)
        return 0.0

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    # 최근 period일 ATR
    return sum(trs[-period:]) / period


def calculate_dynamic_stops(entry_price: int, atr: float,
                            atr_stop_mult: float = 2.0,
                            atr_target_mult: float = 3.0,
                            tick: int = None) -> dict:
    """
    ATR 기반 동적 손절/익절 계산

    Args:
        entry_price: 진입가
        atr: ATR 값
        atr_stop_mult: 손절 ATR 배수 (기본 2.0)
        atr_target_mult: 익절 ATR 배수 (기본 3.0)
        tick: 호가 단위

    Returns:
        stop_loss, target_price, stop_pct, target_pct
    """
    if tick is None:
        tick = _get_tick_size(entry_price)

    # 손절폭 상한 (캡): 진입가의 -7% 까지만 허용
    # 변동성 큰 종목이 ATR×2로 -15%까지 벌어지던 문제 해결
    # (주의: 리플레이 백테스트 결과 캡을 3.5%로 줄이면 승률 64%→28% 붕괴 - 축소 금지)
    MAX_STOP_PCT = 0.07   # 최대 손절폭 7%
    MIN_STOP_PCT = 0.02   # 최소 손절폭 2%

    if atr <= 0:
        # ATR 없으면 고정값 폴백
        stop_loss = _round_to_tick(entry_price * 0.97, tick)
        target = _round_to_tick(entry_price * 1.06, tick)
    else:
        # 손절폭 계산 (모드별 캡은 exit_policy 단일 정의)
        from src.utils.exit_policy import calc_stop_distance
        stop_dist = calc_stop_distance(entry_price, atr, atr_stop_mult)
        stop_loss = _round_to_tick(entry_price - stop_dist, tick)

        # 익절폭 = 손절폭 × R:R 비율 (손익비 보장)
        # atr_target_mult / atr_stop_mult 가 R:R
        rr = atr_target_mult / atr_stop_mult if atr_stop_mult > 0 else 1.5
        target_dist = stop_dist * rr
        target = _round_to_tick(entry_price + target_dist, tick)

    # 안전장치: 익절 최소 +2%
    target = max(target, int(entry_price * 1.02))

    # 손절이 진입가보다 높으면 고정값 사용
    if stop_loss >= entry_price:
        stop_loss = _round_to_tick(entry_price * 0.97, tick)

    stop_pct = (stop_loss - entry_price) / entry_price * 100
    target_pct = (target - entry_price) / entry_price * 100
    rr_ratio = abs(target_pct / stop_pct) if stop_pct != 0 else 0

    return {
        "stop_loss": stop_loss,
        "target_price": target,
        "stop_pct": round(stop_pct, 2),
        "target_pct": round(target_pct, 2),
        "atr": round(atr, 0),
        "atr_stop_mult": atr_stop_mult,
        "atr_target_mult": atr_target_mult,
        "rr_ratio": round(rr_ratio, 2),
    }
