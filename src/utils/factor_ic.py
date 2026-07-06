"""
팩터 IC(Information Coefficient) 분석기
- 각 팩터(기술/수급) 점수와 실제 수익률의 상관계수 계산
- IC가 높은 팩터에 높은 가중치 자동 부여
- 최소 샘플 수 도달 시에만 동작 (샘플 < 20이면 기본값 유지)

사용 이론:
  IC = Corr(팩터점수, 미래수익률)
  - IC > 0.05: 약한 유효성
  - IC > 0.10: 유효
  - IC > 0.15: 강한 유효성 (실전 퀀트 기준)

  가중치 = 팩터별 IC / 전체 IC 합
  (음수 IC는 0으로 처리)
"""
import json
import statistics
from pathlib import Path
from loguru import logger


PERFORMANCE_FILE = Path("data/performance.json")
MIN_SAMPLES = 20  # 최소 샘플 수 (이보다 적으면 기본값 유지)


def _load_performance() -> list[dict]:
    """성과 기록에서 팩터 점수와 수익률 추출"""
    if not PERFORMANCE_FILE.exists():
        return []

    with open(PERFORMANCE_FILE, encoding="utf-8") as f:
        history = json.load(f)

    samples = []
    for date, data in history.items():
        picks = data.get("picks", [])
        results = data.get("results", [])

        for pick, result in zip(picks, results):
            entry = pick.get("entry_price", 0)
            close = result.get("close_price", entry)
            if entry <= 0:
                continue

            ret = (close - entry) / entry * 100
            scores = pick.get("scores", {})

            samples.append({
                "date": date,
                "ticker": pick.get("ticker", ""),
                "name": pick.get("name", ""),
                "tech_score": scores.get("tech", 0),
                "supply_score": scores.get("supply", 0),
                "ai_score": scores.get("ai", 0),
                "return_pct": ret,
            })

    return samples


def _pearson_correlation(x: list, y: list) -> float:
    """피어슨 상관계수 계산"""
    if len(x) != len(y) or len(x) < 2:
        return 0.0

    mean_x = statistics.mean(x)
    mean_y = statistics.mean(y)

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
    den_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5

    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def calculate_ic() -> dict:
    """팩터별 IC 계산"""
    samples = _load_performance()

    result = {
        "sample_count": len(samples),
        "is_valid": len(samples) >= MIN_SAMPLES,
        "ic": {},
        "suggested_weights": None,
        "interpretation": "",
    }

    if not samples:
        result["interpretation"] = "성과 데이터 없음 - 기본 가중치 유지"
        return result

    if len(samples) < MIN_SAMPLES:
        result["interpretation"] = (
            f"샘플 부족 ({len(samples)}/{MIN_SAMPLES}) - "
            f"기본 가중치 유지"
        )
        return result

    returns = [s["return_pct"] for s in samples]

    # 팩터별 IC 계산
    factors = {
        "tech": [s["tech_score"] for s in samples],
        "supply": [s["supply_score"] for s in samples],
        "ai": [s["ai_score"] for s in samples],
    }

    for factor_name, scores in factors.items():
        ic = _pearson_correlation(scores, returns)
        result["ic"][factor_name] = round(ic, 4)

    # IC 기반 가중치 제안 (음수 IC는 0으로)
    tech_ic = max(0, result["ic"]["tech"])
    supply_ic = max(0, result["ic"]["supply"])

    total_ic = tech_ic + supply_ic
    if total_ic > 0:
        result["suggested_weights"] = {
            "tech_weight": round(tech_ic / total_ic, 2),
            "supply_weight": round(supply_ic / total_ic, 2),
        }
    else:
        # 모든 IC가 0 이하면 수급 우선 유지 (한국 시장 특성)
        result["suggested_weights"] = {"tech_weight": 0.4, "supply_weight": 0.6}

    # 해석 생성
    tech_ic_raw = result["ic"]["tech"]
    supply_ic_raw = result["ic"]["supply"]

    interpretations = []
    for name, ic in [("기술", tech_ic_raw), ("수급", supply_ic_raw)]:
        if ic >= 0.15:
            interpretations.append(f"{name}팩터 강한 유효성 (IC={ic:.3f})")
        elif ic >= 0.10:
            interpretations.append(f"{name}팩터 유효 (IC={ic:.3f})")
        elif ic >= 0.05:
            interpretations.append(f"{name}팩터 약한 유효성 (IC={ic:.3f})")
        elif ic > 0:
            interpretations.append(f"{name}팩터 미약 (IC={ic:.3f})")
        else:
            interpretations.append(f"{name}팩터 역상관 (IC={ic:.3f}) - 재검토 필요")

    result["interpretation"] = " / ".join(interpretations)
    return result


def get_suggested_weights() -> dict:
    """주간 리뷰용: 현재 최적 가중치 반환"""
    ic_result = calculate_ic()

    if not ic_result["is_valid"]:
        return {
            "tech_weight": 0.4,
            "supply_weight": 0.6,
            "source": "default",
            "reason": ic_result["interpretation"],
        }

    weights = ic_result["suggested_weights"]
    return {
        **weights,
        "source": "ic_based",
        "reason": ic_result["interpretation"],
        "ic": ic_result["ic"],
        "samples": ic_result["sample_count"],
    }


def format_report() -> str:
    """IC 분석 리포트 텍스트 생성 (Discord용)"""
    result = calculate_ic()

    lines = [
        f"📊 팩터 IC 분석 (샘플 {result['sample_count']}건)",
        f"상태: {'✅ 유효' if result['is_valid'] else '🟡 샘플 부족'}",
        "",
    ]

    if result["ic"]:
        lines.append("[팩터별 IC]")
        for factor, ic in result["ic"].items():
            emoji = "🟢" if ic >= 0.10 else ("🟡" if ic >= 0.05 else "🔴")
            lines.append(f"  {emoji} {factor}: {ic:+.4f}")

    if result.get("suggested_weights"):
        w = result["suggested_weights"]
        lines.append("")
        lines.append("[제안 가중치]")
        lines.append(f"  기술: {w['tech_weight']*100:.0f}%")
        lines.append(f"  수급: {w['supply_weight']*100:.0f}%")

    lines.append("")
    lines.append(f"📝 {result['interpretation']}")

    return "\n".join(lines)
