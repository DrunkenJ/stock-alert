"""
포지션 사이징 모듈
- 변동성 역가중 (Inverse Volatility): 변동성 낮은 종목에 더 배분
- 점수 가중 (Score Weight): 확신 높은 종목에 더 배분
- 두 방식 혼합으로 최적 배분 비율 계산
- 최대 비중 제한으로 집중 리스크 방지
"""
import os
from loguru import logger


# 기본 파라미터
MAX_POSITION_PCT = 40.0   # 종목당 최대 비중 (%)
MIN_POSITION_PCT = 5.0    # 종목당 최소 비중 (%)
SCORE_WEIGHT = 0.5        # 점수 가중 비율 (0~1, 나머지는 변동성역가중)
VOL_WEIGHT = 0.5          # 변동성 역가중 비율


def calculate_position_sizes(picks: list[dict]) -> list[dict]:
    """
    추천 종목별 포지션 비율 계산

    Args:
        picks: 스크리너 결과 (final_score, indicators.vol_ratio 포함)

    Returns:
        picks에 position_pct, position_label 추가된 리스트
    """
    if not picks:
        return picks

    n = len(picks)

    # ── 1. 점수 기반 가중치 ───────────────────────────────
    scores = [p.get("final_score", 5.0) for p in picks]
    min_score = min(scores)
    max_score = max(scores)

    if max_score == min_score:
        score_weights = [1.0 / n] * n
    else:
        # 점수를 1~2 범위로 정규화
        normalized = [(s - min_score) / (max_score - min_score) + 1.0 for s in scores]
        total = sum(normalized)
        score_weights = [v / total for v in normalized]

    # ── 2. 변동성 역가중 ──────────────────────────────────
    # vol_ratio: 거래량 배율 (높을수록 변동성 큼)
    # ATR 없을 때 vol_ratio로 근사
    vol_ratios = []
    for p in picks:
        indicators = p.get("indicators", {})
        vol_ratio = indicators.get("vol_ratio", 1.0)
        price = p.get("price", 0)

        # 가격대별 기본 변동성 추정 (소형주일수록 변동성 큼)
        if price < 5000:
            base_vol = 3.0
        elif price < 20000:
            base_vol = 2.0
        elif price < 100000:
            base_vol = 1.5
        else:
            base_vol = 1.0

        # 거래량 급증 = 변동성 확대 신호
        vol_adjusted = base_vol * max(1.0, vol_ratio ** 0.5)
        vol_ratios.append(vol_adjusted)

    # 역수로 변환 (변동성 낮을수록 높은 가중치)
    inv_vols = [1.0 / v for v in vol_ratios]
    total_inv = sum(inv_vols)
    vol_weights = [v / total_inv for v in inv_vols]

    # ── 3. 혼합 가중치 ────────────────────────────────────
    mixed_weights = [
        SCORE_WEIGHT * sw + VOL_WEIGHT * vw
        for sw, vw in zip(score_weights, vol_weights)
    ]
    total_mixed = sum(mixed_weights)
    raw_pcts = [w / total_mixed * 100 for w in mixed_weights]

    # ── 4. 최대/최소 비중 제한 적용 ──────────────────────
    final_pcts = _apply_constraints(raw_pcts, n)

    # ── 5. 결과 적용 ──────────────────────────────────────
    result = []
    for i, pick in enumerate(picks):
        pct = final_pcts[i]
        pick = dict(pick)
        pick["position_pct"] = round(pct, 1)
        pick["position_label"] = _get_label(pct, n)
        pick["score_weight"] = round(score_weights[i] * 100, 1)
        pick["vol_weight"] = round(vol_weights[i] * 100, 1)
        result.append(pick)

    # 로그
    logger.info("포지션 사이징 완료:")
    for p in result:
        logger.info(
            f"  {p['name']}: {p['position_pct']}% "
            f"(점수가중:{p['score_weight']}% / 변동성가중:{p['vol_weight']}%)"
        )

    return result


def _apply_constraints(raw_pcts: list[float], n: int) -> list[float]:
    """최대/최소 비중 제약 반복 적용"""
    pcts = list(raw_pcts)
    max_iter = 10

    for _ in range(max_iter):
        changed = False

        # 최대 비중 제한
        excess = 0.0
        capped_indices = set()
        for i, p in enumerate(pcts):
            if p > MAX_POSITION_PCT:
                excess += p - MAX_POSITION_PCT
                pcts[i] = MAX_POSITION_PCT
                capped_indices.add(i)
                changed = True

        # 초과분을 나머지에 재배분
        free_indices = [i for i in range(n) if i not in capped_indices]
        if excess > 0 and free_indices:
            per_free = excess / len(free_indices)
            for i in free_indices:
                pcts[i] += per_free

        # 최소 비중 제한
        deficiency = 0.0
        low_indices = set()
        for i, p in enumerate(pcts):
            if p < MIN_POSITION_PCT:
                deficiency += MIN_POSITION_PCT - p
                pcts[i] = MIN_POSITION_PCT
                low_indices.add(i)
                changed = True

        # 부족분을 나머지에서 차감
        free_indices = [i for i in range(n)
                        if i not in low_indices and i not in capped_indices]
        if deficiency > 0 and free_indices:
            per_free = deficiency / len(free_indices)
            for i in free_indices:
                pcts[i] = max(MIN_POSITION_PCT, pcts[i] - per_free)

        if not changed:
            break

    # 합계를 100%로 정규화
    total = sum(pcts)
    if total > 0:
        pcts = [p / total * 100 for p in pcts]

    return pcts


def _get_label(pct: float, n: int) -> str:
    """비중에 따른 레이블"""
    equal = 100 / n
    if pct >= equal * 1.3:
        return "🔴 집중"
    elif pct >= equal * 1.1:
        return "🟡 비중확대"
    elif pct >= equal * 0.9:
        return "🟢 중립"
    elif pct >= equal * 0.7:
        return "🔵 비중축소"
    else:
        return "⚪ 소량"


def format_position_text(picks: list[dict], total_capital: int = None) -> str:
    """Discord용 포지션 텍스트 생성"""
    lines = []
    for p in picks:
        pct = p.get("position_pct", 0)
        label = p.get("position_label", "")
        name = p.get("name", p.get("ticker", ""))

        if total_capital:
            amount = int(total_capital * pct / 100)
            lines.append(f"{label} **{name}**: {pct}% (약 {amount:,}원)")
        else:
            lines.append(f"{label} **{name}**: {pct}%")

    return "\n".join(lines)
