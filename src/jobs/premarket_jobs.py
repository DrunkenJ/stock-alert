"""개장 전 잡 (06:30 미국마감 / 07:00 야간뉴스 / 08:40 프리장)

main.py 에서 옮겨왔다. 등록과 실행 순서는 main.setup_schedule 이 계속 관장한다.
"""
from loguru import logger
from src.notifier.discord import DiscordNotifier
from src.analyzers.premarket import PremarketAnalyzer




# ─────────────────────────────────────────
# 스케줄 작업
# ─────────────────────────────────────────
def run_us_market_collection():
    """06:30 미국 시장 마감 데이터 수집"""
    logger.info("=" * 60)
    logger.info("미국 시장 마감 데이터 수집 (06:30)")
    logger.info("=" * 60)
    try:
        analyzer = PremarketAnalyzer()
        us_data = analyzer.collect_us_market()

        # 간단한 알림 (브리핑용)
        notifier = DiscordNotifier()
        if us_data:
            lines = []
            priority = ["S&P500", "NASDAQ", "SOX", "USDKRW", "OIL", "VIX"]
            for name in priority:
                if name in us_data:
                    d = us_data[name]
                    arrow = "🔺" if d["trend"] == "up" else ("🔻" if d["trend"] == "down" else "➡️")
                    lines.append(f"{arrow} {name}: {d['value']:,.1f} ({d['change_pct']:+.2f}%)")

            # 한국 시장 영향 추정
            stance_emoji = "📈"
            if "SOX" in us_data and us_data["SOX"]["change_pct"] > 1.0:
                stance_hint = "반도체 갭상승 예상"
            elif "NASDAQ" in us_data and us_data["NASDAQ"]["change_pct"] < -1.0:
                stance_hint = "기술주 약세 우려"
                stance_emoji = "📉"
            else:
                stance_hint = "혼조세 예상"
                stance_emoji = "➡️"

            notifier._send({"embeds": [{
                "title": f"🇺🇸 미국 시장 마감 (06:30) {stance_emoji}",
                "description": (
                    "**" + "\n".join(lines) + "**\n\n"
                    f"💡 한국 시장 영향: {stance_hint}\n"
                    "08:40 프리장 종합 분석에서 자세히 안내드릴게요."
                ),
                "color": 0x1976D2,
            }]})

        logger.info("미국 시장 데이터 수집 완료")
    except Exception as e:
        logger.exception(f"미국 시장 수집 오류: {e}")




def run_overnight_news_collection():
    """07:00 야간 뉴스/공시 스캔"""
    logger.info("=" * 60)
    logger.info("야간 뉴스/공시 스캔 (07:00)")
    logger.info("=" * 60)
    try:
        analyzer = PremarketAnalyzer()
        news = analyzer.collect_overnight_news()

        if not news:
            logger.info("야간 뉴스 없음")
            return

        # 호재성 키워드 종목 추출
        keywords_positive = ["수주", "계약", "실적", "흑자전환", "최대", "신고가", "신제품", "특허"]
        keywords_negative = ["횡령", "소송", "검찰", "리콜", "적자", "쇼크", "퇴출"]

        positive_news = []
        negative_news = []
        for n in news:
            if any(k in n for k in keywords_positive):
                positive_news.append(n)
            elif any(k in n for k in keywords_negative):
                negative_news.append(n)

        if positive_news or negative_news:
            notifier = DiscordNotifier()
            desc_parts = []
            if positive_news:
                pos_text = "\n".join(f"  ✅ {n[:50]}" for n in positive_news[:5])
                desc_parts.append(f"**🟢 호재성 뉴스 ({len(positive_news)}건)**\n{pos_text}")
            if negative_news:
                neg_text = "\n".join(f"  ⚠️ {n[:50]}" for n in negative_news[:5])
                desc_parts.append(f"**🔴 악재성 뉴스 ({len(negative_news)}건)**\n{neg_text}")

            notifier._send({"embeds": [{
                "title": f"📰 야간 뉴스 스캔 (07:00)",
                "description": "\n\n".join(desc_parts),
                "color": 0x4CAF50 if len(positive_news) > len(negative_news) else 0xFF9800,
                "footer": {"text": f"총 {len(news)}건 수집 | 08:40 종합 분석에서 한국 영향 분석"},
            }]})

        logger.info(f"야간 뉴스 수집 완료: 호재 {len(positive_news)}건 / 악재 {len(negative_news)}건")
    except Exception as e:
        logger.exception(f"야간 뉴스 수집 오류: {e}")




def run_premarket_analysis():
    """08:40 프리장 분석: 미국 시장 + 야간 뉴스 + 본장 진입 전략"""
    logger.info("=" * 60)
    logger.info("프리장 분석 시작 (08:40)")
    logger.info("=" * 60)

    notifier = DiscordNotifier()
    try:
        analyzer = PremarketAnalyzer()
        result = analyzer.analyze()

        us_data    = result.get("us_market", {})
        impact     = result.get("impact", {})
        candidates = result.get("candidates", [])

        stance = impact.get("stance", "neutral")
        color  = {"bullish": 0x00C851, "bearish": 0xFF4444, "neutral": 0xFFD700}.get(stance, 0xFFD700)
        emoji  = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(stance, "➡️")

        # 미국 시장 요약
        us_lines = []
        priority = ["S&P500", "NASDAQ", "SOX", "USDKRW", "OIL", "VIX"]
        for name in priority:
            if name in us_data:
                d = us_data[name]
                arrow = "🔺" if d["trend"] == "up" else ("🔻" if d["trend"] == "down" else "➡️")
                us_lines.append(f"{arrow} {name}: {d['value']:,.1f} ({d['change_pct']:+.2f}%)")
        us_text = "\n".join(us_lines) if us_lines else "데이터 없음"

        # 후보 종목
        cand_text = ""
        if candidates:
            cand_lines = []
            for c in candidates[:6]:
                emoji_c = "⭐" if c["type"] == "yesterday_strong" else "🇺🇸"
                cand_lines.append(f"{emoji_c} **{c['name']}** - {c['reason']}")
            cand_text = "\n".join(cand_lines)
        else:
            cand_text = "본장 시작 후 실시간 분석 권장"

        key_sectors = ", ".join(impact.get("key_sectors", [])) or "특이 사항 없음"
        avoid       = ", ".join(impact.get("avoid_sectors", [])) or "없음"

        notifier._send({"embeds": [{
            "title": f"🌅 프리장 분석 (08:40) {emoji}",
            "description": (
                f"**전망:** {impact.get('summary', '분석 중...')}\n"
                f"**확신도:** {impact.get('confidence', 0)}%\n"
                f"**전략:** {impact.get('entry_strategy', '관망')}"
            ),
            "color": color,
            "fields": [
                {
                    "name": "🇺🇸 미국 시장 마감",
                    "value": us_text,
                    "inline": False,
                },
                {
                    "name": "🎯 주목 섹터",
                    "value": f"**유망:** {key_sectors}\n**회피:** {avoid}",
                    "inline": False,
                },
                {
                    "name": "📋 본장 진입 후보",
                    "value": cand_text,
                    "inline": False,
                },
            ],
            "footer": {"text": "30분 후 본장 추천 알림 예정"},
        }]})

        logger.info(f"프리장 분석 완료: {stance} (신뢰도 {impact.get('confidence',0)}%)")

    except Exception as e:
        logger.exception(f"프리장 분석 오류: {e}")
