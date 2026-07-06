"""
주간 리뷰 Discord 알림
- 성과 리포트 임베드
- ✅ 적용 / ❌ 유지 버튼 (Discord 컴포넌트)
- 버튼 응답을 봇이 수신해서 파라미터 자동 적용
"""
import os
import json
import requests
from datetime import datetime
import pytz
from loguru import logger

KST = pytz.timezone("Asia/Seoul")


def send_weekly_report(review_data: dict, notifier):
    """주간 리포트 Discord 전송"""
    weekly = review_data["weekly_results"]
    current = review_data["current_params"]
    gpt = review_data["gpt_result"]

    # 성과 계산
    if weekly:
        returns = [r["return_pct"] for r in weekly]
        avg_return = sum(returns) / len(returns)
        win_rate = sum(1 for r in returns if r > 0) / len(returns) * 100
        best = max(weekly, key=lambda x: x["return_pct"])
        worst = min(weekly, key=lambda x: x["return_pct"])
    else:
        avg_return = win_rate = 0
        best = worst = None

    # 성과 색상
    color = 0x00C851 if avg_return > 0 else 0xFF4444

    # 종목별 수익률 필드
    stock_lines = []
    for r in weekly:
        emoji = "🟢" if r["return_pct"] > 0 else "🔴"
        stock_lines.append(
            f"{emoji} **{r['name']}** {r['return_pct']:+.2f}% "
            f"(수급:{r['supply_score']:.0f} 기술:{r['tech_score']:.0f})"
        )

    # 파라미터 조정 제안
    suggested = gpt.get("suggested_params", {})
    should_update = gpt.get("should_update", False)

    param_diff = ""
    if suggested and should_update:
        diffs = []
        if abs(suggested.get("tech_weight", current["tech_weight"]) - current["tech_weight"]) > 0.01:
            diffs.append(f"기술가중치: {current['tech_weight']} → {suggested['tech_weight']}")
        if abs(suggested.get("supply_weight", current["supply_weight"]) - current["supply_weight"]) > 0.01:
            diffs.append(f"수급가중치: {current['supply_weight']} → {suggested['supply_weight']}")
        if abs(suggested.get("min_final_score", current["min_final_score"]) - current["min_final_score"]) > 0.1:
            diffs.append(f"최소점수: {current['min_final_score']} → {suggested['min_final_score']}")
        sl_curr = (1 - current["stop_loss_pct"]) * 100
        sl_new = (1 - suggested.get("stop_loss_pct", current["stop_loss_pct"])) * 100
        if abs(sl_new - sl_curr) > 0.1:
            diffs.append(f"손절: {sl_curr:.1f}% → {sl_new:.1f}%")
        param_diff = "\n".join(diffs) if diffs else "변경 없음"

    fields = [
        {
            "name": "📊 이번 주 성과",
            "value": (
                f"추천 종목: {len(weekly)}개\n"
                f"평균 수익률: **{avg_return:+.2f}%**\n"
                f"승률: **{win_rate:.1f}%**\n"
                + (f"최고: {best['name']} ({best['return_pct']:+.2f}%)\n"
                   f"최저: {worst['name']} ({worst['return_pct']:+.2f}%)" if best else "데이터 없음")
            ),
            "inline": True,
        },
        {
            "name": "⚙️ 현재 파라미터",
            "value": (
                f"기술가중치: {current['tech_weight']}\n"
                f"수급가중치: {current['supply_weight']}\n"
                f"최소점수: {current['min_final_score']}\n"
                f"손절: {(1-current['stop_loss_pct'])*100:.1f}%\n"
                f"익절: {(current['take_profit_pct']-1)*100:.1f}%"
            ),
            "inline": True,
        },
    ]

    if stock_lines:
        fields.append({
            "name": "📈 종목별 결과",
            "value": "\n".join(stock_lines[:8]),
            "inline": False,
        })

    fields.append({
        "name": "🤖 AI 분석",
        "value": gpt.get("analysis", "분석 없음"),
        "inline": False,
    })

    if should_update and param_diff:
        fields.append({
            "name": "💡 파라미터 조정 제안",
            "value": (
                f"{param_diff}\n\n"
                f"**근거:** {gpt.get('reason', '')}\n\n"
                f"✅ 적용하려면: `!전략적용`\n"
                f"❌ 유지하려면: 무시"
            ),
            "inline": False,
        })
    else:
        fields.append({
            "name": "💡 AI 판단",
            "value": f"현재 파라미터 유지 권장\n{gpt.get('reason', '')}",
            "inline": False,
        })

    embed = {
        "title": f"📋 주간 성과 리포트 ({datetime.now(KST).strftime('%Y-%m-%d')})",
        "color": color,
        "fields": fields,
        "footer": {"text": "✅ 파라미터 적용: !전략적용 | ❌ 유지: 무시"},
    }

    # 제안된 파라미터를 JSON으로 임시 저장 (봇이 나중에 읽어서 적용)
    if suggested and should_update:
        _save_pending_params(suggested)

    payload = {
        "content": "📋 **이번 주 주간 리뷰**",
        "embeds": [embed],
    }
    notifier._send(payload)
    logger.info("주간 리포트 Discord 전송 완료")


def _save_pending_params(params: dict):
    """승인 대기 중인 파라미터 임시 저장"""
    from pathlib import Path
    pending_file = Path("data/pending_params.json")
    pending_file.parent.mkdir(exist_ok=True)
    with open(pending_file, "w") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    logger.info("파라미터 승인 대기 저장")
