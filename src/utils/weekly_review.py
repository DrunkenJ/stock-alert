"""
주간 성과 분석 + 반자동 파라미터 조정
- 매주 금요일 16:30 자동 실행
- 추천 종목 실제 수익률 계산
- GPT로 패턴 분석 및 파라미터 조정 제안
- Discord 승인 버튼으로 반자동 적용
"""
import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

PARAMS_FILE = Path("data/strategy_params.json")
HISTORY_FILE = Path("data/performance.json")

# 기본 전략 파라미터
DEFAULT_PARAMS = {
    "tech_weight": 0.4,       # 기술적 분석 가중치
    "supply_weight": 0.6,     # 수급 분석 가중치
    "min_final_score": 4.0,   # 최소 추천 점수
    "min_supply_score": 3,    # 최소 수급 점수
    "stop_loss_pct": 0.97,    # 손절 기준
    "take_profit_pct": 1.06,  # 익절 기준
    "updated_at": "",
    "version": 1,
}


def load_params() -> dict:
    """현재 전략 파라미터 로드"""
    if PARAMS_FILE.exists():
        with open(PARAMS_FILE) as f:
            params = json.load(f)
        # 누락된 키 기본값으로 보완
        for k, v in DEFAULT_PARAMS.items():
            params.setdefault(k, v)
        return params
    return dict(DEFAULT_PARAMS)


def save_params(params: dict):
    """전략 파라미터 저장"""
    PARAMS_FILE.parent.mkdir(exist_ok=True)
    params["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    params["version"] = params.get("version", 1) + 1
    with open(PARAMS_FILE, "w") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    logger.info(f"전략 파라미터 저장 완료 (v{params['version']})")


def load_history() -> dict:
    """성과 이력 로드"""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return {}


def get_weekly_results() -> list[dict]:
    """이번 주 추천 종목 성과 수집"""
    history = load_history()
    today = datetime.now()
    week_ago = today - timedelta(days=7)

    weekly = []
    for date_str, data in history.items():
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            if week_ago <= date <= today:
                picks = data.get("picks", [])
                results = data.get("results", [])
                for pick, result in zip(picks, results):
                    entry = pick.get("entry_price", 0)
                    close = result.get("close_price", entry)
                    ret = (close - entry) / entry * 100 if entry else 0
                    weekly.append({
                        "date": date_str,
                        "name": pick.get("name", ""),
                        "ticker": pick.get("ticker", ""),
                        "entry": entry,
                        "close": close,
                        "return_pct": round(ret, 2),
                        "tech_score": pick.get("scores", {}).get("tech", 0),
                        "supply_score": pick.get("scores", {}).get("supply", 0),
                        "ai_score": pick.get("scores", {}).get("ai", 0),
                        "final_score": pick.get("scores", {}).get("final", 0),
                    })
        except Exception:
            continue
    return weekly


def analyze_with_gpt(weekly_results: list[dict], current_params: dict,
                       sim_stats: dict = None) -> dict:
    """GPT로 주간 성과 분석 + IC 기반 가중치 제안"""
    from openai import OpenAI
    from src.utils.factor_ic import get_suggested_weights
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    if not weekly_results:
        return {"suggestion": None, "reason": "이번 주 데이터 없음"}

    # IC 기반 가중치 자동 계산
    ic_weights = get_suggested_weights()

    # 성과 요약
    returns = [r["return_pct"] for r in weekly_results]
    avg_return = sum(returns) / len(returns)
    win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
    best = max(weekly_results, key=lambda x: x["return_pct"])
    worst = min(weekly_results, key=lambda x: x["return_pct"])

    # 수익/손실 종목의 점수 패턴 분석
    winners = [r for r in weekly_results if r["return_pct"] > 0]
    losers = [r for r in weekly_results if r["return_pct"] <= 0]

    avg_winner_supply = sum(r["supply_score"] for r in winners) / len(winners) if winners else 0
    avg_loser_supply = sum(r["supply_score"] for r in losers) / len(losers) if losers else 0
    avg_winner_tech = sum(r["tech_score"] for r in winners) / len(winners) if winners else 0
    avg_loser_tech = sum(r["tech_score"] for r in losers) / len(losers) if losers else 0

    # 시뮬레이션 통계 텍스트
    sim_text = ""
    if sim_stats and sim_stats.get("trade_count", 0) > 0:
        sim_text = f"""

[시뮬레이션 통계 (최근 7일, {sim_stats['trade_count']}건)]
- 승률: {sim_stats.get('win_rate', 0):.1f}%
- 평균 수익률: {sim_stats.get('avg_pct', 0):+.2f}%
- 평균 익절: {sim_stats.get('avg_win', 0):+.2f}% | 평균 손절: {sim_stats.get('avg_loss', 0):+.2f}%
- 손익비(PF): {sim_stats.get('profit_factor', 0):.2f}
- 1차 익절 도달: {sim_stats.get('partial_1_rate', 0):.1f}% | 2차 익절 도달: {sim_stats.get('partial_2_rate', 0):.1f}%

[국면별 성과]
"""
        for r, s in sim_stats.get("regime_stats", {}).items():
            sim_text += f"- {r}: {s['count']}건, 평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%\n"

        sim_text += "\n[섹터별 성과]\n"
        for sec, s in sorted(
            sim_stats.get("sector_stats", {}).items(),
            key=lambda x: x[1]["avg_pct"], reverse=True
        )[:5]:
            sim_text += f"- {sec}: {s['count']}건, 평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%\n"

        sim_text += "\n[점수대별 성과]\n"
        for b, s in sim_stats.get("score_stats", {}).items():
            sim_text += f"- {b}: {s['count']}건, 평균 {s['avg_pct']:+.2f}%, 승률 {s['win_rate']:.0f}%\n"

        sim_text += "\n[청산 사유 분포]\n"
        for r, c in sim_stats.get("close_reasons", {}).items():
            sim_text += f"- {r}: {c}건\n"

    prompt = f"""
당신은 한국 주식 퀀트 전략 최적화 전문가입니다.
이번 주 추천 종목 성과를 분석하고 전략 파라미터 조정을 제안해주세요.

[이번 주 성과 요약]
- 추천 종목 수: {len(weekly_results)}개
- 평균 수익률: {avg_return:.2f}%
- 승률: {win_rate:.1f}%
- 최고 성과: {best['name']} ({best['return_pct']:+.2f}%)
- 최저 성과: {worst['name']} ({worst['return_pct']:+.2f}%)

[수익 종목 vs 손실 종목 패턴]
- 수익 종목 평균 수급점수: {avg_winner_supply:.1f} / 손실 종목: {avg_loser_supply:.1f}
- 수익 종목 평균 기술점수: {avg_winner_tech:.1f} / 손실 종목: {avg_loser_tech:.1f}

[현재 전략 파라미터]
- 기술적 가중치: {current_params['tech_weight']}
- 수급 가중치: {current_params['supply_weight']}
- 최소 추천 점수: {current_params['min_final_score']}
- 손절 기준: {(1-current_params['stop_loss_pct'])*100:.1f}%
- 익절 기준: {(current_params['take_profit_pct']-1)*100:.1f}%

[IC 기반 자동 가중치 계산]
- 분석 근거: {ic_weights.get('reason', '')}
- 샘플 수: {ic_weights.get('samples', 0)}
- IC 기반 제안: 기술={ic_weights['tech_weight']}, 수급={ic_weights['supply_weight']}
※ 샘플 20건 이상에서 IC 계산 신뢰 가능

다음 JSON 형식으로만 응답하세요:
{{
  "analysis": "이번 주 성과 분석 2~3줄",
  "should_update": true/false,
  "suggested_params": {{
    "tech_weight": 0.0~1.0,
    "supply_weight": 0.0~1.0,
    "min_final_score": 3.0~7.0,
    "stop_loss_pct": 0.93~0.98,
    "take_profit_pct": 1.04~1.15
  }},
  "reason": "파라미터 조정 이유 2~3줄"
}}
"""
    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)

        # tech + supply 합계 = 1.0 검증
        if result.get("suggested_params"):
            sp = result["suggested_params"]
            # IC 유효성 있으면 IC 기반 가중치 우선 사용
            if ic_weights.get("source") == "ic_based":
                sp["tech_weight"] = ic_weights["tech_weight"]
                sp["supply_weight"] = ic_weights["supply_weight"]
            else:
                total = sp.get("tech_weight", 0.4) + sp.get("supply_weight", 0.6)
                if abs(total - 1.0) > 0.01:
                    sp["supply_weight"] = round(1.0 - sp["tech_weight"], 2)

        # IC 결과 포함
        result["ic_analysis"] = ic_weights
        return result
    except Exception as e:
        logger.error(f"GPT 분석 실패: {e}")
        return {"suggestion": None, "reason": f"GPT 오류: {e}"}


def apply_params(suggested: dict) -> dict:
    """제안된 파라미터 적용"""
    current = load_params()
    current.update(suggested)
    save_params(current)

    # .env 파일도 업데이트 (MIN_FINAL_SCORE)
    _update_env("MIN_FINAL_SCORE", str(suggested.get("min_final_score", 4.0)))
    return current


def _update_env(key: str, value: str):
    """환경변수 파일 업데이트"""
    env_path = Path(".env")
    if not env_path.exists():
        return
    with open(env_path) as f:
        lines = f.readlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)


def run_weekly_review() -> dict:
    """주간 리뷰 전체 실행 (시뮬레이션 통계 포함)"""
    logger.info("주간 성과 분석 시작")

    weekly_results = get_weekly_results()
    current_params = load_params()

    # 시뮬레이션 통계 추가 (정밀 분석)
    sim_stats = {}
    try:
        from src.utils.trade_simulator import get_simulator
        sim = get_simulator()
        sim_stats = sim.get_statistics(days=7)
        logger.info(f"시뮬레이션 통계: {sim_stats.get('trade_count')}건")
    except Exception as e:
        logger.warning(f"시뮬레이션 통계 오류: {e}")

    gpt_result = analyze_with_gpt(weekly_results, current_params, sim_stats)

    # [2단계] 실패 패턴 분석 + 자동 규칙 학습
    learned = {}
    try:
        from src.utils.failure_analyzer import get_failure_analyzer
        analyzer = get_failure_analyzer()
        analysis = analyzer.analyze(sim_stats)
        rule_result = analyzer.apply_rules(current_params, analysis)
        learned = {
            "analysis": analysis,
            "changes":  rule_result["changes"],
        }
        if rule_result["changes"]:
            logger.info(f"자동 학습된 규칙: {len(rule_result['changes'])}개 변경")
    except Exception as e:
        logger.warning(f"실패 패턴 분석 오류: {e}")

    # 3단계 분석 준비 알람 체크
    stage3_alert = None
    try:
        from src.utils.detailed_collector import check_stage3_readiness
        stage3_alert = check_stage3_readiness()
        if stage3_alert:
            logger.info(f"3단계 알람: 데이터 {stage3_alert.get('closed_count')}건")
    except Exception as e:
        logger.debug(f"3단계 알람 체크 오류: {e}")

    return {
        "weekly_results": weekly_results,
        "current_params": current_params,
        "sim_stats":      sim_stats,
        "gpt_result":     gpt_result,
        "learned":        learned,
        "stage3_alert":   stage3_alert,
    }
