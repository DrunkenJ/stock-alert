"""
실패 패턴 분석 + 자동 전략 조정
- 시뮬레이션 데이터에서 손실 종목의 공통 패턴 추출
- 잘 되는 섹터/시간대 가중치 자동 상향
- 다음 주 자동 제외 규칙 생성
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


LEARNED_RULES_FILE = Path("data/learned_rules.json")


class FailurePatternAnalyzer:
    """실패 패턴 분석기"""

    def __init__(self):
        self.rules = self._load_rules()

    def analyze(self, sim_stats: dict) -> dict:
        """
        시뮬레이션 통계에서 실패 패턴 + 성공 패턴 추출
        """
        result = {
            "exclude_sectors":   [],      # 자동 제외 섹터
            "boost_sectors":     [],      # 가중치 상향 섹터
            "min_score_adj":     0,       # 최소 점수 조정
            "tech_weight_adj":   0,       # 기술적 분석 가중치 조정
            "supply_weight_adj": 0,       # 수급 분석 가중치 조정
            "regime_warnings":   [],      # 특정 국면 경고
            "score_threshold":   None,    # 새로운 점수 임계값
            "analysis_summary":  "",
        }

        if not sim_stats or sim_stats.get("trade_count", 0) < 10:
            result["analysis_summary"] = "데이터 부족 (10건 이상 필요)"
            return result

        # 1. 섹터별 패턴 분석
        sector_stats = sim_stats.get("sector_stats", {})
        for sector, s in sector_stats.items():
            # 보수적 조정: 최소 5건 이상 누적된 패턴만
            if s["count"] < 5:
                continue
            # 제외 기준 강화: 평균 -3% 이하 AND 승률 25% 이하 (명백한 손실만)
            if s["avg_pct"] < -3.0 and s["win_rate"] < 25:
                result["exclude_sectors"].append({
                    "sector": sector,
                    "reason": f"평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%",
                    "count":  s["count"],
                })
            # 강화 기준 강화: 평균 +4% 이상 AND 승률 65% 이상 (확실한 강세만)
            elif s["avg_pct"] > 4.0 and s["win_rate"] >= 65:
                result["boost_sectors"].append({
                    "sector": sector,
                    "reason": f"평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%",
                    "boost":  +0.3,  # 가점도 0.5 → 0.3으로 축소
                })

        # 2. 점수대별 패턴 분석
        score_stats = sim_stats.get("score_stats", {})
        # 4.0-5.0 구간이 명백한 손실일 때만 (count 5+, 평균 -2.5%+ )
        bucket_45 = score_stats.get("4.0-5.0", {})
        if bucket_45 and bucket_45.get("count", 0) >= 5:
            if bucket_45["avg_pct"] < -2.5 and bucket_45["win_rate"] < 30:
                result["min_score_adj"] = +1.0
                result["score_threshold"] = 5.0

        # 5.0-7.0 구간 조정은 더 엄격하게 (count 8+, 평균 -2%+)
        bucket_57 = score_stats.get("5.0-7.0", {})
        if bucket_57 and bucket_57.get("count", 0) >= 8:
            if bucket_57["avg_pct"] < -2.0 and bucket_57["win_rate"] < 35:
                result["min_score_adj"] = +2.0
                result["score_threshold"] = 7.0

        # 3. 국면별 패턴 분석
        regime_stats = sim_stats.get("regime_stats", {})
        for regime, s in regime_stats.items():
            if s["count"] < 3:
                continue
            if s["avg_pct"] < -2.0:
                result["regime_warnings"].append({
                    "regime": regime,
                    "reason": f"평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%",
                    "action": "추천 종목 50% 축소 권장",
                })

        # 4. 청산 사유 패턴 분석
        close_reasons = sim_stats.get("close_reasons", {})
        total = sim_stats["trade_count"]
        stop_loss_rate = close_reasons.get("stop_loss", 0) / total * 100

        # 가중치 조정은 매우 보수적으로 (충분한 데이터 + 명확한 패턴만)
        if total >= 20:  # 최소 20건 이상
            if stop_loss_rate > 50:  # 50% 이상 손절일 때만
                result["tech_weight_adj"]   = +0.05  # 0.1 → 0.05
                result["supply_weight_adj"] = -0.05
                result["analysis_summary"] = (
                    f"손절율 {stop_loss_rate:.0f}% 과다 → 기술적 분석 비중 소폭 상향"
                )
            elif stop_loss_rate < 15:  # 15% 이하일 때만
                result["supply_weight_adj"] = +0.05
                result["tech_weight_adj"]   = -0.05

        # 5. 분할 익절 도달률 분석
        p1_rate = sim_stats.get("partial_1_rate", 0)
        p2_rate = sim_stats.get("partial_2_rate", 0)

        # 1차 익절 도달률 낮음 (<30%) → 1차 익절 기준 완화 권장
        if p1_rate < 30 and total >= 10:
            result["analysis_summary"] += " | 1차 익절 도달률 낮음, 기준 완화 검토"

        return result

    def apply_rules(self, current_params: dict, analysis: dict) -> dict:
        """
        분석 결과를 파라미터에 자동 반영
        """
        updated = dict(current_params)
        changes = []

        # 최소 점수 조정
        if analysis.get("min_score_adj", 0) > 0:
            old_score = updated.get("min_final_score", 4.0)
            new_score = min(old_score + analysis["min_score_adj"], 7.0)
            if new_score != old_score:
                updated["min_final_score"] = new_score
                changes.append(f"최소 점수: {old_score} → {new_score}")

        # 가중치 조정
        if analysis.get("tech_weight_adj", 0) != 0:
            old_w = updated.get("tech_weight", 0.4)
            new_w = max(0.2, min(0.6, old_w + analysis["tech_weight_adj"]))
            updated["tech_weight"]   = round(new_w, 2)
            updated["supply_weight"] = round(1.0 - new_w, 2)
            changes.append(f"기술/수급 가중치: {old_w:.1f}/{1-old_w:.1f} → {new_w:.1f}/{1-new_w:.1f}")

        # 제외 섹터 저장
        exclude = analysis.get("exclude_sectors", [])
        if exclude:
            self.rules["excluded_sectors"] = [e["sector"] for e in exclude]
            self.rules["excluded_at"]      = datetime.now().strftime("%Y-%m-%d")
            changes.append(f"자동 제외 섹터: {', '.join(e['sector'] for e in exclude)}")

        # 강화 섹터 저장
        boost = analysis.get("boost_sectors", [])
        if boost:
            self.rules["boost_sectors"] = {b["sector"]: b["boost"] for b in boost}
            changes.append(f"가중치 상향 섹터: {', '.join(b['sector'] for b in boost)}")

        self._save_rules()
        return {"params": updated, "changes": changes}

    def get_learned_rules(self) -> dict:
        """현재 학습된 규칙 조회"""
        # 7일 이상 된 규칙은 무효화
        applied_at = self.rules.get("excluded_at", "")
        if applied_at:
            try:
                applied = datetime.strptime(applied_at, "%Y-%m-%d")
                if (datetime.now() - applied).days > 14:
                    return {}
            except Exception:
                pass
        return self.rules

    def _load_rules(self) -> dict:
        if not LEARNED_RULES_FILE.exists():
            return {}
        try:
            with open(LEARNED_RULES_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_rules(self):
        LEARNED_RULES_FILE.parent.mkdir(exist_ok=True)
        with open(LEARNED_RULES_FILE, "w") as f:
            json.dump(self.rules, f, ensure_ascii=False, indent=2)


def get_failure_analyzer() -> FailurePatternAnalyzer:
    return FailurePatternAnalyzer()
