from typing import Optional, Tuple, List, Dict, Any
"""
Discord 웹훅 알림 모듈
- 리치 임베드 형식의 종목 알림
- 장전/장중/장후 알림 포맷
- 에러 알림
"""
import os
import json
import requests
from datetime import datetime
import pytz
from loguru import logger
from dotenv import load_dotenv

load_dotenv()

KST = pytz.timezone("Asia/Seoul")


def _kst_now() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")


def _build_description(pick: dict) -> str:
    """종목 임베드 설명 생성 (섹터 + 뉴스 + 수급패턴 + 점수신뢰도)"""
    sector     = pick.get("sector", "")
    news_s     = pick.get("news_sentiment", {})
    market     = pick.get("market", "")
    score      = pick.get("total_score", 0)
    confidence = pick.get("score_confidence", "")
    s_pattern  = pick.get("supply_pattern", {})

    parts = [f"종합점수: **{score:.1f}점**"]
    if confidence:
        parts.append(f"신뢰도: {confidence}")
    if sector:
        parts.append(f"📂 {sector}")
    if market:
        parts.append(market)

    desc = " | ".join(parts)

    # 수급 패턴 표시
    pattern = s_pattern.get("pattern", "")
    if pattern and pattern not in ["일반", "unknown"]:
        p_emoji = "🟢" if s_pattern.get("adj", 0) > 0 else "🔴"
        desc += f"\n{p_emoji} 수급패턴: {pattern}"

    # 뉴스 감성
    if news_s and news_s.get("sentiment") != "neutral":
        emoji = "✅" if news_s["sentiment"] == "positive" else "⚠️"
        key_news = news_s.get("key_news", "")[:35]
        if key_news:
            desc += f"\n📰 {emoji} {key_news}"

    return desc


def _build_entry_field(pick: dict, entry: int, target: int, stoploss: int,
                       upside: float, downside: float) -> str:
    """매수 전략 필드 텍스트 생성"""
    es = pick.get("entry_strategy", {})
    pos_pct = pick.get("position_pct", 0)
    pos_label = pick.get("position_label", "")

    if not es:
        return (
            f"현재가: **{entry:,}원** ({pick.get('change_rate', 0):+.2f}%)\n"
            f"목표가: `{target:,}원` (+{upside:.1f}%)\n"
            f"손절가: `{stoploss:,}원` ({downside:.1f}%)\n"
            f"권장비중: **{pos_pct:.1f}%** {pos_label}"
        )

    gap_emoji = es.get("gap_emoji", "✅")
    gap_msg = es.get("gap_message", "")
    split_1st = es.get("split_1st", entry)
    split_2nd = es.get("split_2nd", entry)
    split_3rd = es.get("split_3rd", entry)
    ratio = es.get("split_ratio", (0.5, 0.3, 0.2))
    buyable = es.get("buyable", True)

    if not buyable:
        return (
            f"{gap_emoji} **{gap_msg}**\n"
            f"현재가: {entry:,}원 ({pick.get('change_rate', 0):+.2f}%)\n"
            f"⏸ 갭과열 - 눌림 후 재진입 검토"
        )

    atr = es.get("atr", 0)
    rr = es.get("rr_ratio", 0)
    is_atr = es.get("is_atr_based", False)
    stop_label = f"ATR({atr:.0f}원)" if is_atr else "고정"

    return (
        f"{gap_emoji} {gap_msg}\n"
        f"현재가: {entry:,}원 ({pick.get('change_rate', 0):+.2f}%)\n"
        f"1차(`{int(ratio[0]*100)}%`): `{split_1st:,}원`\n"
        f"2차(`{int(ratio[1]*100)}%`): `{split_2nd:,}원`\n"
        f"3차(`{int(ratio[2]*100)}%`): `{split_3rd:,}원`\n"
        f"목표: `{target:,}원` (+{upside:.1f}%) | "
        f"손절: `{stoploss:,}원` ({downside:.1f}%)\n"
        f"R:R={rr:.1f} [{stop_label}] | 비중: **{pos_pct:.1f}%** {pos_label}"
    )


class DiscordNotifier:
    """Discord 웹훅으로 종목 알림 전송"""

    # 색상 코드
    COLOR_GREEN = 0x00C851
    COLOR_BLUE = 0x2196F3
    COLOR_ORANGE = 0xFF6D00
    COLOR_RED = 0xFF4444
    COLOR_GRAY = 0x9E9E9E
    COLOR_GOLD = 0xFFD700

    def __init__(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        if not self.webhook_url:
            raise ValueError("DISCORD_WEBHOOK_URL 미설정")
        # DISCORD_WEBHOOK_ALERT가 플레이스홀더거나 미설정이면 기본 웹훅 사용
        alert_url = os.getenv("DISCORD_WEBHOOK_ALERT", "")
        if not alert_url or "your_alert_webhook" in alert_url or len(alert_url) < 50:
            self.alert_url = self.webhook_url
        else:
            self.alert_url = alert_url

    def _send(self, payload: dict, url: str = None) -> bool:
        """웹훅 POST 전송"""
        target = url or self.webhook_url
        try:
            resp = requests.post(
                target,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 204:
                return True
            else:
                logger.warning(f"Discord 전송 오류: {resp.status_code} {resp.text}")
                return False
        except Exception as e:
            logger.error(f"Discord 웹훅 실패: {e}")
            return False

    # ─────────────────────────────────────────
    # 메인 알림: 추천 종목 리스트
    # ─────────────────────────────────────────
    def send_morning_picks(self, picks: List[dict], market_summary: str):
        """장 시작 전 추천 종목 알림"""
        embeds = []

        # 포지션 배분 요약
        position_summary = ""
        if picks and picks[0].get("position_pct"):
            pos_lines = []
            for p in picks:
                pos_lines.append(
                    f"{p.get('position_label', '')} **{p.get('name', '')}**: "
                    f"{p.get('position_pct', 0):.1f}%"
                )
            position_summary = "\n**📊 권장 배분:**\n" + "\n".join(pos_lines)

        # 헤더 임베드
        header = {
            "title": "📈 오늘의 추천 종목",
            "description": (
                f"**시장 전략:** {market_summary}\n\n"
                f"수급+기술+AI 복합 분석 기반 종목 선정"
                f"{position_summary}"
            ),
            "color": self.COLOR_GOLD,
            "footer": {"text": f"🕐 {_kst_now()} | KIS API + OpenAI"},
            "thumbnail": {"url": "https://i.imgur.com/8g7YHSK.png"},
        }
        embeds.append(header)

        # 종목별 임베드
        for i, pick in enumerate(picks, 1):
            embed = self._build_stock_embed(i, pick)
            embeds.append(embed)

        # Discord는 한 번에 최대 10개 임베드
        payload = {
            "content": "@here 🔔 **오늘의 추천 종목이 선정되었습니다!**",
            "embeds": embeds[:10],
        }
        success = self._send(payload)
        if success:
            logger.info(f"장전 알림 전송 완료 ({len(picks)}종목)")

    def send_realtime_alert(self, pick: dict, trigger: str):
        """장중 실시간 조건 충족 알림"""
        embed = self._build_stock_embed(None, pick, is_realtime=True)
        embed["title"] = f"⚡ 실시간 알림: {pick['name']}"
        embed["description"] = f"**트리거:** {trigger}\n\n" + embed.get("description", "")
        embed["color"] = self.COLOR_ORANGE

        payload = {
            "content": f"🚨 **{pick['name']}** 조건 충족!",
            "embeds": [embed],
        }
        self._send(payload)

    def send_closing_summary(self, picks: List[dict], results: List[dict]):
        """장 마감 후 결과 요약"""
        fields = []
        total_return = 0

        for pick, result in zip(picks, results):
            entry = pick.get("price", 0)
            close_price = result.get("close_price", entry)
            ret = (close_price - entry) / entry * 100 if entry else 0
            total_return += ret

            emoji = "🟢" if ret > 0 else ("🔴" if ret < 0 else "⚪")
            fields.append({
                "name": f"{emoji} {pick['name']} ({pick['ticker']})",
                "value": (
                    f"진입가: {entry:,}원 → 종가: {close_price:,}원\n"
                    f"수익률: **{ret:+.2f}%**"
                ),
                "inline": True,
            })

        avg_return = total_return / len(picks) if picks else 0
        embed = {
            "title": "📊 오늘의 종목 결과 요약",
            "description": f"평균 수익률: **{avg_return:+.2f}%**",
            "color": self.COLOR_GREEN if avg_return > 0 else self.COLOR_RED,
            "fields": fields,
            "footer": {"text": f"🕐 {_kst_now()}"},
        }
        payload = {
            "content": "📋 **오늘 추천 종목 마감 결과**",
            "embeds": [embed],
        }
        self._send(payload)

    def send_error_alert(self, error_msg: str, context: str = ""):
        """에러 알림"""
        embed = {
            "title": "⚠️ 시스템 오류",
            "description": f"**컨텍스트:** {context}\n```\n{error_msg[:500]}\n```",
            "color": self.COLOR_RED,
            "footer": {"text": _kst_now()},
        }
        payload = {"embeds": [embed]}
        self._send(payload, url=self.alert_url)

    def send_startup_message(self):
        """봇 시작 알림"""
        embed = {
            "title": "🤖 Stock Alert Bot 시작",
            "description": (
                "**📊 분석 파이프라인:**\n"
                "• 🗺️ 시장 국면 분류 (추세/횡보/약세장)\n"
                "• 🔭 거시경제 판단 (VIX + 환율 + 뉴스)\n"
                "• 🌊 외국인/기관 수급 분석 (KIS API)\n"
                "• 📈 기술적 분석 (MA/RSI/MACD/BB/ATR)\n"
                "• 📂 섹터 다변화 (동일섹터 최대 2종목)\n"
                "• 📰 뉴스 감성 분석 (악재 자동 차단)\n"
                "• 🤖 AI 종합 판단 (GPT-4o-mini)\n"
                "• 💰 포지션 사이징 (변동성 역가중)\n"
                "• 🎯 ATR 기반 동적 손절/익절\n"
                "• ✂️ 분할매수 3단계 가이드\n\n"
                "**⏰ 자동 실행 스케줄:**\n"
                "• 09:05 - 전종목 DB 갱신\n"
                "• 09:07 - 거시경제 + 시장 국면 판단\n"
                "• 09:10 - 추천 종목 알림 (섹터분산+뉴스검증)\n"
                "• 09:10~15:30 - 실시간 목표가/손절 알림\n"
                "• 16:00 - 마감 결과 요약\n"
                "• 16:05 - 수급 데이터 저장 (백테스트용)\n"
                "• 금 16:30 - 주간 성과 리포트 + AI 전략 제안\n\n"
                "**💬 Discord 명령어:**\n"
                "`!분석` `!비교` `!국면` `!포지션`\n"
                "`!주간리포트` `!전략적용` `!전략현황` `!IC분석`\n"
                "`!도움말` — 전체 명령어 목록"
            ),
            "color": self.COLOR_BLUE,
            "footer": {"text": f"✅ 시작됨: {_kst_now()} | v2.0"},
        }
        self._send({"embeds": [embed]})

    # ─────────────────────────────────────────
    # 임베드 빌더
    # ─────────────────────────────────────────
    def _build_stock_embed(self, rank: int, pick: dict, is_realtime: bool = False) -> dict:
        ai = pick.get("ai_eval", {})
        supply = pick.get("supply_summary", {})
        indicators = pick.get("indicators", {})
        tech_signals = pick.get("tech_signals", [])
        supply_signals = pick.get("supply_signals", [])

        # 추천 레벨별 색상
        rec = ai.get("recommendation", "관망")
        color_map = {
            "강력매수": self.COLOR_GREEN,
            "매수": self.COLOR_BLUE,
            "관망": self.COLOR_GRAY,
            "매도회피": self.COLOR_RED,
        }
        color = color_map.get(rec, self.COLOR_GRAY)

        # 목표가/손절가 수익률 (없으면 기본값 +6%/-3% 적용)
        entry = pick.get("price", 0)
        target = ai.get("target_price", 0) or int(entry * 1.06)
        stoploss = ai.get("stop_loss", 0) or int(entry * 0.97)
        upside = (target - entry) / entry * 100 if entry and target else 0
        downside = (stoploss - entry) / entry * 100 if entry and stoploss else 0

        # 주요 시그널 요약
        positive_signals = [s["name"] for s in tech_signals + supply_signals if s.get("type") == "positive"][:4]
        signal_text = " · ".join(positive_signals) if positive_signals else "해당 없음"

        title_prefix = f"**#{rank}**" if rank else "⚡"
        fields = [
            {
                "name": "💰 매수 전략",
                "value": _build_entry_field(pick, entry, target, stoploss, upside, downside),
                "inline": True,
            },
            {
                "name": "📊 분석 점수",
                "value": (
                    f"기술적: **{pick.get('tech_score', 0)}/15**\n"
                    f"수급: **{pick.get('supply_score', 0)}/15**\n"
                    f"AI: **{ai.get('ai_score', 0)}/10**"
                ),
                "inline": True,
            },
            {
                "name": "🌊 수급 현황",
                "value": (
                    f"외국인: {supply.get('foreign_net', 0):+,}주 "
                    f"({supply.get('foreign_consecutive', 0)}일 연속)\n"
                    f"기관: {supply.get('inst_net', 0):+,}주 "
                    f"({supply.get('inst_consecutive', 0)}일 연속)\n"
                    f"{'✅ 동반매수' if supply.get('is_double_buy') else '⚠️ 단독매수'}"
                ),
                "inline": True,
            },
            {
                "name": "📈 기술 지표",
                "value": (
                    f"RSI: {indicators.get('rsi', 'N/A')}\n"
                    f"거래량: 평균 {indicators.get('vol_ratio', 0):.1f}배\n"
                    f"시그널: {signal_text}"
                ),
                "inline": True,
            },
            {
                "name": f"🤖 CIO 최종 판단 {({'strong':'🟢','moderate':'🟡','weak':'🔴'}).get(ai.get('consensus',''),'') }",
                "value": (
                    f"추천: **{rec}** | 합의: {ai.get('consensus','-')}\n"
                    f"근거: {ai.get('reason', '-')}\n"
                    f"보유예상: {ai.get('holding_days', '-')}일"
                ),
                "inline": False,
            },
            {
                "name": f"⚠️ 리스크 검증 {({'high':'🔴','medium':'🟡','low':'🟢'}).get(ai.get('_risk',{}).get('risk_level',''),'')}",
                "value": (
                    f"위험도: **{ai.get('_risk',{}).get('risk_level','-')}**\n"
                    f"{ai.get('_risk',{}).get('bear_case','') or ai.get('risk','-')}"
                ),
                "inline": False,
            },
        ]

        return {
            "title": f"{title_prefix} {pick.get('name', '')} ({pick.get('ticker', '')})",
            "description": _build_description(pick),
            "color": color,
            "fields": fields,
        }
