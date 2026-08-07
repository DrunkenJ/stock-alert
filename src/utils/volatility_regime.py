"""시장 변동성 국면 판정 + 국면별 ATR 진입 필터

왜 필요한가
-----------
ATR 상한을 절대값 하나로 고정하면 시장 변동성 국면이 바뀔 때 무너진다.
- 2026-05~07 백데이터로 잡은 ATR 5.0% 상한을 2026-08 시장에 적용했더니
  후보 50개 중 2개(4%)만 통과해 5거래일 연속 추천 0개가 나왔다.
  당시 후보 풀 ATR 중앙값은 10.4%였다 (삼성전자조차 일중 21% 폭을 그린 국면).
- 반대로 저변동성 국면에서는 5.0% 상한이 아무것도 거르지 못한다.

→ 후보 풀의 ATR 중앙값으로 시장 변동성 국면을 판정하고, 국면별 상한을 쓴다.

상한을 정하는 기준
------------------
ATR 상한은 손절 로직과 물려 있다. entry_calculator 는 손절폭을
    stop_dist = clip(ATR × 2.6, 진입가의 2%, 진입가의 7%)
로 계산한다. 상한 7%가 고정이므로 ATR이 커질수록 손절폭은 ATR 대비 좁아지고,
손절폭이 ATR보다 작아지면 정상 등락(노이즈)에 손절당한다.

  ATR  4% → 손절 7% = 1.75 ATR   (여유 있음)
  ATR  7% → 손절 7% = 1.00 ATR   (아슬아슬)
  ATR 10% → 손절 7% = 0.70 ATR   (노이즈에 털림)

그래서 어떤 국면에서도 ATR 상한이 STOP_CAP / MIN_STOP_ATR_MULT 를 넘지 않게 묶는다.
손절폭 캡(7%)은 리플레이 백테스트에서 축소 시 승률 64%→28%로 붕괴한 값이라
건드리지 않는다 (entry_calculator 주석 참조).
"""
import os
import statistics
from loguru import logger


# entry_calculator.MAX_STOP_PCT 와 동기화 (%)
STOP_CAP_PCT = 7.0

# 손절폭이 최소 이 배수만큼은 ATR을 덮어야 한다
MIN_STOP_ATR_MULT = float(os.getenv("MIN_STOP_ATR_MULT", "0.8"))

# 어떤 국면에서도 넘을 수 없는 ATR 절대 실링
ABSOLUTE_ATR_CEILING = STOP_CAP_PCT / MIN_STOP_ATR_MULT   # 기본 8.75%

# (레벨, 중앙값 하한, 중앙값 상한, ATR 상한, 한글 라벨)
BANDS = [
    ("calm",     0.0,   2.5,   3.5, "저변동성"),
    ("normal",   2.5,   4.0,   5.0, "보통"),
    ("elevated", 4.0,   7.0,   7.0, "변동성 확대"),
    ("high",     7.0,  10.0,   8.5, "고변동성"),
    ("extreme", 10.0, 999.0, ABSOLUTE_ATR_CEILING, "초고변동성"),
]

# 국면 판정과 무관하게, 풀에서 이 비율보다 많이 통과시키지 않는다
MAX_KEEP_RATIO = float(os.getenv("ATR_MAX_KEEP_RATIO", "0.6"))


def classify_market_volatility(atr_pcts: list[float]) -> dict:
    """후보 풀의 ATR 분포로 시장 변동성 국면 판정

    Args:
        atr_pcts: 후보 종목들의 ATR/주가 비율(%) 리스트
    Returns:
        level / label / median / atr_cap / stop_atr_mult
    """
    vals = [a for a in atr_pcts if a is not None and a > 0]
    if not vals:
        return {
            "level": "unknown", "label": "판단 불가", "median": None,
            "atr_cap": float(os.getenv("MAX_ENTRY_ATR_PCT", "20.0")),
            "stop_atr_mult": None, "sample": 0,
        }

    median = statistics.median(vals)

    level, label, cap = "extreme", "초고변동성", ABSOLUTE_ATR_CEILING
    for lv, lo, hi, c, lb in BANDS:
        if lo <= median < hi:
            level, label, cap = lv, lb, c
            break

    cap = min(cap, ABSOLUTE_ATR_CEILING)

    return {
        "level":         level,
        "label":         label,
        "median":        round(median, 2),
        "atr_cap":       round(cap, 2),
        # 이 상한의 종목을 샀을 때 손절폭이 ATR의 몇 배가 되는지
        "stop_atr_mult": round(STOP_CAP_PCT / cap, 2) if cap else None,
        "sample":        len(vals),
    }


def apply_volatility_filter(items: list, get_atr, final_picks: int = 5) -> tuple[list, dict]:
    """국면별 ATR 상한으로 후보를 거른다

    Args:
        items:       후보 리스트 (임의 dict)
        get_atr:     item → ATR% (없으면 None)
        final_picks: 로깅용
    Returns:
        (통과 리스트, 국면 정보 dict)
    """
    if not items:
        return items, {}

    atrs = [get_atr(it) for it in items]
    vol = classify_market_volatility(atrs)

    if vol["median"] is None:
        return items, vol

    cap = vol["atr_cap"]
    scored = [(it, a) for it, a in zip(items, atrs) if a is not None]
    unscored = [it for it, a in zip(items, atrs) if a is None]

    passed = [it for it, a in scored if a <= cap]

    # 상한이 느슨해 풀의 대부분이 통과하는 국면에서는 상대 순위로 한 번 더 조인다
    max_keep = int(len(scored) * MAX_KEEP_RATIO)
    if max_keep and len(passed) > max_keep:
        passed.sort(key=lambda it: get_atr(it))
        passed = passed[:max_keep]

    logger.info(
        f"  변동성 국면: {vol['label']} (풀 ATR 중앙값 {vol['median']:.1f}%) "
        f"→ ATR 상한 {cap:.1f}% / 손절폭 {vol['stop_atr_mult']:.2f}ATR"
    )
    logger.info(f"  변동성 필터: {len(items)}개 → {len(passed)}개 통과")

    if vol["level"] == "extreme":
        logger.warning(
            "  초고변동성 국면 - 손절폭(7% 고정)이 ATR을 충분히 덮지 못합니다. "
            "추천 수가 적거나 0개일 수 있습니다."
        )

    # ATR 산출 불가 종목은 판단 보류 → 통과시키지 않음 (안전측)
    if unscored:
        logger.debug(f"  ATR 산출 불가 {len(unscored)}개 제외")

    return passed, vol
