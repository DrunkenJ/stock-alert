from typing import Optional, Tuple, List, Dict, Any
"""
Discord 봇 - 수시 종목 조회 기능
사용자가 Discord 채널에서 종목을 질문하면 즉시 분석 결과 반환

사용법:
  !분석 삼성전자
  !분석 005930
  !도움말
"""
import os
import sys
import asyncio
from datetime import datetime
import threading
from loguru import logger
import discord
from discord.ext import commands
from dotenv import load_dotenv

# 스레드에서도 /app 경로 인식하도록 설정
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

from src.api.kis_client import KISClient
from src.analyzers.technical import TechnicalAnalyzer
from src.analyzers.supply_demand import SupplyDemandAnalyzer
from src.analyzers.ai_evaluator import AIEvaluator
from src.notifier.discord import DiscordNotifier

load_dotenv()

# ─────────────────────────────────────────
# 종목명 → 티커 검색 (KIS API 활용)
# ─────────────────────────────────────────
def resolve_ticker(query: str) -> Optional[Tuple[str, str]]:
    """종목명 or 티커코드로 ticker, name 반환
    1. 6자리 숫자 → 직접 시세 조회
    2. 종목명 → 로컬 전종목 DB 검색 (빠름)
    3. 로컬 DB 실패 → KIS API 검색
    """
    from src.utils.stock_db import get_db  # noqa
    kis = KISClient()

    # 6자리 숫자코드 직접 조회
    if query.isdigit() and len(query) == 6:
        try:
            data = kis.get_stock_price(query)
            return query, data["name"]
        except Exception:
            return None

    # 전종목 로컬 DB 검색
    db = get_db()
    result = db.search(query)
    if result:
        return result["ticker"], result["name"]

    # KIS API 검색 (폴백)
    try:
        result = kis.search_stock_name(query)
        if result:
            return result["ticker"], result["name"]
    except Exception:
        pass

    return None


def analyze_stock_full(ticker: str, name: str) -> dict:
    """종목 전체 분석 실행"""
    kis = KISClient()
    tech = TechnicalAnalyzer()
    sd = SupplyDemandAnalyzer()
    ai = AIEvaluator()

    price_data = kis.get_stock_price(ticker)
    candles = kis.get_daily_ohlcv(ticker, days=120)
    investor = kis.get_investor_trend(ticker, days=20)

    tech_result = tech.analyze(candles)
    supply_result = sd.analyze(investor, price_data)

    stock_info = {
        "ticker": ticker,
        "name": name,
        "price": price_data["price"],
        "change_rate": price_data["change_rate"],
        "volume": price_data["volume"],
        "tech_score": tech_result["score"],
        "tech_signals": tech_result["signals"],
        "supply_score": supply_result["score"],
        "supply_signals": supply_result["signals"],
        "supply_summary": supply_result["summary"],
        "indicators": tech_result.get("indicators", {}),
        "total_score": tech_result["score"] * 0.4 + supply_result["score"] * 0.6,
    }

    ai_result = ai.evaluate_stock(stock_info)
    stock_info["ai_eval"] = ai_result
    stock_info["final_score"] = stock_info["total_score"] * 0.6 + ai_result["ai_score"] * 0.4

    # 포지션 사이징 + 매수가 계산
    try:
        from src.utils.position_sizer import calculate_position_sizes
        from src.utils.entry_calculator import calculate_entry
        sized = calculate_position_sizes([stock_info])
        stock_info = calculate_entry(sized[0])
    except Exception as e:
        logger.debug(f"포지션/매수가 계산 오류: {e}")

    return stock_info


def build_analysis_embed(stock: dict) -> discord.Embed:
    """분석 결과 Discord Embed 생성"""
    ai = stock.get("ai_eval", {})
    supply = stock.get("supply_summary", {})
    indicators = stock.get("indicators", {})
    tech_signals = stock.get("tech_signals", [])
    supply_signals = stock.get("supply_signals", [])

    rec = ai.get("recommendation", "관망")
    color_map = {
        "강력매수": 0x00C851,
        "매수":    0x2196F3,
        "관망":    0x9E9E9E,
        "매도회피": 0xFF4444,
    }
    color = color_map.get(rec, 0x9E9E9E)

    entry = stock["price"]
    target = ai.get("target_price", 0)
    stoploss = ai.get("stop_loss", 0)
    upside = (target - entry) / entry * 100 if entry and target else 0
    downside = (stoploss - entry) / entry * 100 if entry and stoploss else 0

    positive = [s["name"] for s in tech_signals + supply_signals if s.get("type") == "positive"][:5]
    negative = [s["name"] for s in tech_signals + supply_signals if s.get("type") == "negative"][:3]

    embed = discord.Embed(
        title=f"🔍 {stock['name']} ({stock['ticker']}) 분석",
        description=(
            f"종합점수: **{stock['final_score']:.1f}점** | AI 추천: **{rec}**"
        ),
        color=color,
    )

    embed.add_field(
        name="💰 가격 정보",
        value=(
            f"현재가: **{entry:,}원** ({stock['change_rate']:+.2f}%)\n"
            f"목표가: `{target:,}원` ({upside:+.1f}%)\n"
            f"손절가: `{stoploss:,}원` ({downside:+.1f}%)\n"
            f"보유예상: {ai.get('holding_days', '-')}일"
        ),
        inline=True,
    )

    embed.add_field(
        name="📊 분석 점수",
        value=(
            f"기술적: **{stock['tech_score']}/15**\n"
            f"수급:   **{stock['supply_score']}/15**\n"
            f"AI:     **{ai.get('ai_score', 0)}/10**\n"
            f"종합:   **{stock['final_score']:.1f}점**"
        ),
        inline=True,
    )

    embed.add_field(
        name="🌊 수급 현황",
        value=(
            f"외국인: {supply.get('foreign_net', 0):+,}주 "
            f"({supply.get('foreign_consecutive', 0)}일 연속)\n"
            f"기관:   {supply.get('inst_net', 0):+,}주 "
            f"({supply.get('inst_consecutive', 0)}일 연속)\n"
            f"{'✅ 외국인·기관 동반매수' if supply.get('is_double_buy') else '⚠️ 동반매수 아님'}"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 기술 지표",
        value=(
            f"RSI: {indicators.get('rsi', 'N/A')}\n"
            f"거래량: 평균 대비 {indicators.get('vol_ratio', 0):.1f}배\n"
            f"20MA: {indicators.get('ma20', 'N/A'):,}원" if indicators.get('ma20') else
            f"RSI: {indicators.get('rsi', 'N/A')}\n거래량: 평균 대비 {indicators.get('vol_ratio', 0):.1f}배"
        ),
        inline=True,
    )

    if positive:
        embed.add_field(
            name="✅ 긍정 시그널",
            value="\n".join(f"• {s}" for s in positive),
            inline=True,
        )

    if negative:
        embed.add_field(
            name="⛔ 부정 시그널",
            value="\n".join(f"• {s}" for s in negative),
            inline=True,
        )

    embed.add_field(
        name="🤖 AI 판단",
        value=(
            f"**근거:** {ai.get('reason', '-')}\n"
            f"**리스크:** {ai.get('risk', '-')}"
        ),
        inline=False,
    )

    embed.set_footer(text="KIS API + GPT-4o-mini 분석 | 투자 판단은 본인 책임")
    return embed


# ─────────────────────────────────────────
# Discord 봇 정의
# ─────────────────────────────────────────
class StockBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self._remove_default_help()

    def _remove_default_help(self):
        self.remove_command("help")

    async def on_ready(self):
        logger.info(f"Discord 봇 로그인: {self.user}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="!분석 [종목명/코드]"
            )
        )

    async def on_command_error(self, ctx, error):
        if isinstance(error, commands.CommandNotFound):
            return  # 모르는 명령어는 무시
        logger.warning(f"봇 커맨드 오류: {error}")


def create_bot() -> StockBot:
    bot = StockBot()

    # ── !분석 커맨드 ──────────────────────────
    @bot.command(name="분석", aliases=["analyze", "a"])
    async def analyze(ctx, *, query: str = None):
        """
        사용법: !분석 삼성전자  또는  !분석 005930
        """
        if not query:
            await ctx.send("📌 사용법: `!분석 [종목명 또는 종목코드]`\n예) `!분석 삼성전자` / `!분석 005930`")
            return

        query = query.strip()
        thinking = await ctx.send(f"🔍 **{query}** 분석 중... (약 10~20초 소요)")

        try:
            # 티커 조회
            result = await asyncio.get_event_loop().run_in_executor(
                None, resolve_ticker, query
            )
            if not result:
                await thinking.edit(content=f"❌ **{query}** 종목을 찾을 수 없어요.\n종목명 또는 6자리 종목코드를 정확히 입력해주세요.")
                return

            ticker, name = result

            # 전체 분석 실행 (blocking → executor)
            stock = await asyncio.get_event_loop().run_in_executor(
                None, analyze_stock_full, ticker, name
            )

            embed = build_analysis_embed(stock)
            # thinking 메시지 삭제 실패해도 결과는 반드시 전송
            try:
                await thinking.delete()
            except Exception:
                pass
            await ctx.send(embed=embed)
            logger.info(f"수동 분석 완료: {name}({ticker}) by {ctx.author}")

        except Exception as e:
            logger.exception(f"분석 오류 ({query}): {e}")
            try:
                await thinking.edit(content=f"⚠️ 분석 중 오류가 발생했어요: `{str(e)[:100]}`")
            except Exception:
                await ctx.send(f"⚠️ 분석 중 오류가 발생했어요: `{str(e)[:100]}`")

    # ── !비교 커맨드 ──────────────────────────
    @bot.command(name="비교", aliases=["compare", "vs"])
    async def compare(ctx, *, query: str = None):
        """
        사용법: !비교 삼성전자 SK하이닉스
        두 종목을 동시에 분석해서 비교
        """
        if not query or len(query.split()) < 2:
            await ctx.send("📌 사용법: `!비교 [종목1] [종목2]`\n예) `!비교 삼성전자 SK하이닉스`")
            return

        parts = query.split()
        q1, q2 = parts[0], parts[1]
        thinking = await ctx.send(f"🔍 **{q1}** vs **{q2}** 비교 분석 중...")

        try:
            loop = asyncio.get_event_loop()
            r1, r2 = await asyncio.gather(
                loop.run_in_executor(None, resolve_ticker, q1),
                loop.run_in_executor(None, resolve_ticker, q2),
            )
            if not r1:
                await thinking.edit(content=f"❌ {q1} 종목을 찾을 수 없어요.")
                return
            if not r2:
                await thinking.edit(content=f"❌ {q2} 종목을 찾을 수 없어요.")
                return

            s1, s2 = await asyncio.gather(
                loop.run_in_executor(None, analyze_stock_full, r1[0], r1[1]),
                loop.run_in_executor(None, analyze_stock_full, r2[0], r2[1]),
            )

            try:
                await thinking.delete()
            except Exception:
                pass
            await ctx.send(f"**📊 {r1[1]} vs {r2[1]} 비교**", embed=build_analysis_embed(s1))
            await ctx.send(embed=build_analysis_embed(s2))

            # 승자 판정
            winner = r1[1] if s1["final_score"] >= s2["final_score"] else r2[1]
            diff = abs(s1["final_score"] - s2["final_score"])
            await ctx.send(
                f"🏆 **종합 우위: {winner}** "
                f"(점수 차이: {diff:.1f}점)"
            )
            logger.info(f"비교 분석 완료: {r1[1]} vs {r2[1]}")

        except Exception as e:
            logger.exception(f"비교 오류: {e}")
            await thinking.edit(content=f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !주간리포트 커맨드 ───────────────────
    @bot.command(name="주간리포트", aliases=["weekly", "w"])
    async def weekly_report(ctx):
        """주간 성과 분석 및 파라미터 제안"""
        thinking = await ctx.send("📊 주간 성과 분석 중... (약 20초 소요)")
        try:
            from src.utils.weekly_review import run_weekly_review
            from src.notifier.discord_review import send_weekly_report
            from src.notifier.discord import DiscordNotifier

            loop = asyncio.get_event_loop()
            review_data = await loop.run_in_executor(None, run_weekly_review)
            notifier = DiscordNotifier()
            await loop.run_in_executor(None, send_weekly_report, review_data, notifier)
            try:
                await thinking.delete()
            except Exception:
                pass
            logger.info(f"주간 리포트 전송: {ctx.author}")
        except Exception as e:
            logger.exception(f"주간 리포트 오류: {e}")
            await thinking.edit(content=f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !전략적용 커맨드 ─────────────────────
    @bot.command(name="전략적용", aliases=["apply"])
    async def apply_strategy(ctx):
        """AI가 제안한 파라미터 적용"""
        from pathlib import Path
        import json

        pending_file = Path("data/pending_params.json")
        if not pending_file.exists():
            await ctx.send("⚠️ 적용할 파라미터 제안이 없어요. `!주간리포트`를 먼저 실행하세요.")
            return

        try:
            with open(pending_file) as f:
                suggested = json.load(f)

            from src.utils.weekly_review import apply_params
            loop = asyncio.get_event_loop()
            new_params = await loop.run_in_executor(None, apply_params, suggested)

            pending_file.unlink()  # 적용 후 삭제

            msg = (
                "✅ **전략 파라미터 적용 완료!**\n"
                "```\n"
                f"기술가중치: {new_params['tech_weight']}\n"
                f"수급가중치: {new_params['supply_weight']}\n"
                f"최소점수:   {new_params['min_final_score']}\n"
                f"손절기준:   -{(1-new_params['stop_loss_pct'])*100:.1f}%\n"
                f"익절기준:   +{(new_params['take_profit_pct']-1)*100:.1f}%\n"
                f"버전:       v{new_params['version']}\n"
                "```"
            )
            await ctx.send(msg)
            logger.info(f"전략 파라미터 적용: {ctx.author}")
        except Exception as e:
            await ctx.send(f"⚠️ 적용 오류: `{str(e)[:100]}`")

    # ── !전략현황 커맨드 ─────────────────────
    @bot.command(name="전략현황", aliases=["params", "p"])
    async def show_params(ctx):
        """현재 전략 파라미터 확인"""
        from src.utils.weekly_review import load_params
        params = load_params()
        msg = (
            f"⚙️ **현재 전략 파라미터** (v{params.get('version', 1)})\n"
            "```\n"
            f"기술가중치: {params['tech_weight']} ({params['tech_weight']*100:.0f}%)\n"
            f"수급가중치: {params['supply_weight']} ({params['supply_weight']*100:.0f}%)\n"
            f"최소점수:   {params['min_final_score']}\n"
            f"손절기준:   -{(1-params['stop_loss_pct'])*100:.1f}%\n"
            f"익절기준:   +{(params['take_profit_pct']-1)*100:.1f}%\n"
            f"최종수정:   {params.get('updated_at', '초기값')}\n"
            "```"
        )
        await ctx.send(msg)

    # ── !스크리닝 커맨드 ────────────────────
    @bot.command(name="스크리닝", aliases=["scan", "s", "screening"])
    async def manual_screening(ctx, *, args: str = None):
        """
        수시 스크리닝 실행 + 추천 종목 Discord 전송
        사용법:
          !스크리닝          → 기본 스크리닝 (최대 5종목)
          !스크리닝 3        → 상위 3종목만
          !스크리닝 strict   → 최소 점수 6.0 엄격 모드
          !스크리닝 fast     → AI 평가 제외 (빠른 결과)
        """
        # 파라미터 파싱
        max_picks  = 5
        min_score  = None
        fast_mode  = False

        if args:
            parts = args.lower().split()
            for p in parts:
                if p.isdigit():
                    max_picks = int(p)
                elif p == "strict":
                    min_score = 6.0
                elif p == "fast":
                    fast_mode = True

        mode_str  = "빠름(AI제외)" if fast_mode else "정밀(3단계AI)"
        score_str = f" | 최소점수: {min_score}" if min_score else ""
        eta       = 3 if fast_mode else 8
        thinking  = await ctx.send(
            f"🔍 스크리닝 시작... (약 {eta}분 소요)\n"
            f"모드: {mode_str} | 최대종목: {max_picks}개{score_str}"
        )

        try:
            from src.analyzers.screener import StockScreener
            from src.notifier.discord import DiscordNotifier
            from src.analyzers.ai_evaluator import AIEvaluator

            loop = asyncio.get_event_loop()

            def _run_screening():
                screener = StockScreener()
                screener.final_picks = max_picks
                if min_score:
                    screener.min_score = min_score

                if fast_mode:
                    # fast 모드: AI 평가 단계 스킵
                    picks = screener._get_candidates_with_scores()
                    return picks[:max_picks] if picks else []
                else:
                    return screener.run()

            picks = await loop.run_in_executor(None, _run_screening)

            try:
                await thinking.delete()
            except Exception:
                pass

            if not picks:
                await ctx.send(
                    f"📭 조건 충족 종목 없음\n"
                    f"최소 점수({min_score or 4.0}) 미달 또는 뉴스 악재 제외"
                )
                return

            # 결과 전송
            notifier = DiscordNotifier()
            time_str = datetime.now().strftime("%H:%M")
            await ctx.send(
                f"✅ **수동 스크리닝 완료** ({len(picks)}종목)\n"
                f"요청: {ctx.author.name} | {time_str}"
            )

            # 시황 요약
            if not fast_mode and picks:
                ai = AIEvaluator()
                summary = await loop.run_in_executor(
                    None,
                    lambda: ai.generate_market_summary(picks, "수동 스크리닝")
                )
                notifier.send_morning_picks(picks, summary)
            else:
                notifier.send_morning_picks(picks, "수동 스크리닝 결과")

            logger.info(f"수동 스크리닝: {[p['name'] for p in picks]} by {ctx.author}")

        except Exception as e:
            logger.exception(f"수동 스크리닝 오류: {e}")
            try:
                await thinking.edit(content=f"⚠️ 스크리닝 오류: `{str(e)[:100]}`")
            except Exception:
                await ctx.send(f"⚠️ 스크리닝 오류: `{str(e)[:100]}`")

    # ── !데이터수집 커맨드 ────────────────────
    @bot.command(name="데이터수집", aliases=["data", "datacount"])
    async def show_data_count(ctx):
        """3단계 분석용 데이터 누적 상태"""
        from src.utils.detailed_collector import get_data_count

        try:
            stats = get_data_count()
            total = stats["total"]
            closed = stats["closed"]
            active = stats["active"]

            target = 30
            progress = min(100, int(closed / target * 100))
            bar = "█" * (progress // 10) + "░" * (10 - progress // 10)

            status = ""
            if closed >= 30:
                status = "🎯 **3단계 분석 가능!** Claude에게 요청하세요."
            elif closed >= 15:
                status = f"📊 중간 단계 - {30 - closed}건 더 필요"
            else:
                status = f"📈 초기 단계 - 데이터 누적 중"

            await ctx.send(
                f"**📊 3단계 분석 데이터 누적 현황**\n\n"
                f"전체 기록: **{total}건**\n"
                f"청산 완료: **{closed}건** / 30건\n"
                f"진행 중: {active}건\n\n"
                f"`{bar}` {progress}%\n\n"
                f"{status}"
            )

        except Exception as e:
            await ctx.send(f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !학습규칙 커맨드 ────────────────────
    @bot.command(name="학습규칙", aliases=["rules", "learned"])
    async def show_learned_rules(ctx):
        """자동 학습된 규칙 조회 (주간 리뷰에서 자동 생성)"""
        from src.utils.failure_analyzer import get_failure_analyzer

        try:
            analyzer = get_failure_analyzer()
            rules = analyzer.get_learned_rules()

            if not rules:
                await ctx.send(
                    "📭 학습된 규칙 없음\n"
                    "금요일 주간 리뷰 후 자동으로 생성됩니다\n"
                    "(시뮬레이션 데이터 최소 10건 필요)"
                )
                return

            excluded  = rules.get("excluded_sectors", [])
            boost     = rules.get("boost_sectors", {})
            updated   = rules.get("excluded_at", "")

            lines = [f"**🧠 자동 학습된 전략 규칙** ({updated})\n"]

            if excluded:
                lines.append("🚫 **자동 제외 섹터**")
                for sec in excluded:
                    lines.append(f"  • {sec}")
            else:
                lines.append("🚫 제외 섹터: 없음")

            lines.append("")

            if boost:
                lines.append("⭐ **가중치 상향 섹터**")
                for sec, b in boost.items():
                    lines.append(f"  • {sec}: +{b}점")
            else:
                lines.append("⭐ 강화 섹터: 없음")

            lines.append("\n💡 다음 주간 리뷰까지 자동 적용됩니다")

            await ctx.send("\n".join(lines))

        except Exception as e:
            await ctx.send(f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !시뮬레이션 커맨드 ────────────────────
    @bot.command(name="시뮬레이션", aliases=["sim", "simulation"])
    async def show_simulation(ctx, days: int = 7):
        """시뮬레이션 매매 통계 조회"""
        from src.utils.trade_simulator import get_simulator
        thinking = await ctx.send(f"📊 최근 {days}일 시뮬레이션 통계 분석 중...")

        try:
            loop = asyncio.get_event_loop()
            sim = get_simulator()
            stats = await loop.run_in_executor(None, lambda: sim.get_statistics(days=days))

            try:
                await thinking.delete()
            except Exception:
                pass

            if stats.get("trade_count", 0) == 0:
                await ctx.send("📭 시뮬레이션 데이터 없음 (최소 7일 필요)")
                return

            cnt      = stats["trade_count"]
            wr       = stats["win_rate"]
            avg      = stats["avg_pct"]
            pf       = stats["profit_factor"]
            avg_win  = stats["avg_win"]
            avg_loss = stats["avg_loss"]
            p1       = stats["partial_1_rate"]
            p2       = stats["partial_2_rate"]

            # 청산 사유
            reasons = stats.get("close_reasons", {})
            reason_text = "\n".join(
                f"  • {k}: {v}건" for k, v in reasons.items()
            ) or "데이터 없음"

            # 섹터별
            sector_lines = []
            for sec, s in sorted(
                stats.get("sector_stats", {}).items(),
                key=lambda x: x[1]["avg_pct"], reverse=True
            )[:5]:
                sector_lines.append(
                    f"  {sec}: {s['count']}건, {s['avg_pct']:+.2f}% (승률 {s['win_rate']:.0f}%)"
                )
            sector_text = "\n".join(sector_lines) or "없음"

            # 점수대별
            score_lines = []
            for b, s in stats.get("score_stats", {}).items():
                score_lines.append(
                    f"  {b}: {s['count']}건, {s['avg_pct']:+.2f}% (승률 {s['win_rate']:.0f}%)"
                )
            score_text = "\n".join(score_lines) or "없음"

            embed = discord.Embed(
                title=f"📊 시뮬레이션 통계 (최근 {days}일)",
                color=0x00C851 if avg > 0 else 0xFF4444,
            )
            embed.add_field(
                name="📈 전체 성과",
                value=(
                    f"거래수: **{cnt}건** | 활성: {stats.get('active_count',0)}건\n"
                    f"승률: **{wr:.1f}%** | 평균 수익: **{avg:+.2f}%**\n"
                    f"평균 익절: {avg_win:+.2f}% | 평균 손절: {avg_loss:+.2f}%\n"
                    f"손익비(PF): **{pf:.2f}**"
                ),
                inline=False,
            )
            embed.add_field(
                name="✂️ 분할 익절 도달률",
                value=f"1차(+4%): {p1:.1f}% | 2차(+8%): {p2:.1f}%",
                inline=False,
            )
            embed.add_field(
                name="🏁 청산 사유",
                value=reason_text,
                inline=False,
            )
            embed.add_field(
                name="📂 섹터별 (상위 5개)",
                value=sector_text,
                inline=False,
            )
            embed.add_field(
                name="⭐ 점수대별",
                value=score_text,
                inline=False,
            )
            await ctx.send(embed=embed)

        except Exception as e:
            await thinking.edit(content=f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !추적 커맨드 ─────────────────────────
    @bot.command(name="추적", aliases=["watch", "watchlist"])
    async def watchlist_cmd(ctx, *, query: str = None):
        """
        추적 종목 등록/조회/삭제
        사용법:
          !추적             → 추적 종목 목록
          !추적 삼성전자    → 현재가로 추적 등록
          !추적 삭제 005930 → 추적 삭제
        """
        from src.utils.watchlist import get_watchlist_manager, load_watchlist
        from src.api.kis_client import KISClient

        wm = get_watchlist_manager()

        # 목록 조회
        if not query:
            items = wm.get_all()
            if not items:
                await ctx.send(
                    "📭 추적 종목 없음\n"
                    "사용법: `!추적 [종목명]`\n"
                    "예) `!추적 삼성전자`"
                )
                return

            kis = KISClient()
            lines = ["**👀 추적 종목 현황**\n"]
            for item in items:
                try:
                    cur      = kis.get_stock_price(item["ticker"])["price"]
                    reg      = item["reg_price"]
                    chg      = (cur - reg) / reg * 100
                    alerted  = "✅" if item.get("alerted") else "⏳"
                    days     = (datetime.now() - datetime.strptime(
                        item["added_date"], "%Y-%m-%d")).days
                    lines.append(
                        f"{alerted} **{item['name']}** ({item['ticker']})\n"
                        f"　등록가: {reg:,} → 현재: {cur:,} ({chg:+.1f}%) | {days}일째 추적\n"
                        f"　점수: {item['score']:.1f} | 이유: {item.get('reason','')[:20]}"
                    )
                except Exception:
                    lines.append(f"• {item['name']}: 조회 실패")

            await ctx.send("\n".join(lines))
            return

        parts = query.split()

        # 삭제
        if parts[0] == "삭제" and len(parts) >= 2:
            ticker = parts[1]
            if wm.remove(ticker):
                await ctx.send(f"✅ {ticker} 추적 삭제 완료")
            else:
                await ctx.send(f"⚠️ {ticker} 를 찾을 수 없어요")
            return

        # 등록
        name_query = query.strip()
        try:
            from src.utils.stock_db import get_db
            db = get_db()
            db.load()
            ticker = db._db.get(name_query, "")
            if not ticker:
                for n, t in db._db.items():
                    if name_query in n:
                        ticker = t
                        name_query = n
                        break

            if not ticker:
                await ctx.send(f"⚠️ '{name_query}' 종목을 찾을 수 없어요")
                return

            kis = KISClient()
            price = kis.get_stock_price(ticker)["price"]
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: wm.add(ticker, name_query, 0.0, price, "수동 등록")
            )
            if result:
                target = int(price * 0.97)
                await ctx.send(
                    f"👀 **{name_query}** ({ticker}) 추적 등록 완료\n"
                    f"등록가: {price:,}원 | 목표 진입가: {target:,}원 (-3%)\n"
                    f"조정 + 반등 시점에 자동으로 알림을 드릴게요!"
                )
            else:
                await ctx.send(f"이미 추적 중인 종목이에요")

        except Exception as e:
            await ctx.send(f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !보유 커맨드 ─────────────────────────
    @bot.command(name="보유", aliases=["hold", "holding"])
    async def add_holding(ctx, *, query: str = None):
        """
        보유 종목 등록/조회/삭제
        사용법:
          !보유                    → 보유 종목 목록
          !보유 삼성전자 78500      → 종목 등록 (종목명 진입가)
          !보유 삼성전자 78500 83000 76100 → (종목명 진입가 목표가 손절가)
          !보유 삭제 005930        → 종목 삭제
        """
        from src.utils.sell_signal import (
            load_holdings, add_holding as _add,
            remove_holding, save_holdings
        )

        # 목록 조회
        if not query:
            holdings = load_holdings()
            if not holdings:
                await ctx.send(
                    "📭 등록된 보유 종목 없음\n"
                    "사용법: `!보유 [종목명] [진입가]`\n"
                    "예) `!보유 삼성전자 78500`"
                )
                return

            from src.api.kis_client import KISClient
            kis = KISClient()
            lines = ["**📋 보유 종목 현황**\n"]
            for ticker, h in holdings.items():
                try:
                    cur = kis.get_stock_price(ticker)["price"]
                    entry = h.get("entry_price", 0)
                    gain = (cur - entry) / entry * 100 if entry else 0
                    emoji = "🟢" if gain >= 0 else "🔴"
                    lines.append(
                        f"{emoji} **{h['name']}** ({ticker})\n"
                        f"　진입: {entry:,} → 현재: {cur:,} ({gain:+.1f}%)"
                    )
                except Exception:
                    lines.append(f"• {h.get('name', ticker)}: 조회 실패")
            await ctx.send("\n".join(lines))
            return

        parts = query.split()

        # 삭제
        if parts[0] == "삭제" and len(parts) >= 2:
            ticker = parts[1]
            if remove_holding(ticker):
                await ctx.send(f"✅ {ticker} 보유 종목 삭제 완료")
            else:
                await ctx.send(f"⚠️ {ticker} 를 찾을 수 없어요")
            return

        # 등록: !보유 종목명 진입가 [목표가] [손절가]
        if len(parts) < 2:
            await ctx.send("사용법: `!보유 [종목명] [진입가]`\n예) `!보유 삼성전자 78500`")
            return

        try:
            # 마지막 숫자들 파싱
            nums = []
            name_parts = []
            for p in parts:
                try:
                    nums.append(int(p.replace(",", "")))
                except ValueError:
                    name_parts.append(p)

            if not nums:
                await ctx.send("진입가를 숫자로 입력해주세요")
                return

            name_query = " ".join(name_parts)
            entry = nums[0]
            target = nums[1] if len(nums) > 1 else int(entry * 1.06)
            stop = nums[2] if len(nums) > 2 else int(entry * 0.97)

            # 종목 검색
            from src.utils.stock_db import get_db
            db = get_db()
            db.load()
            ticker = db._db.get(name_query, "")
            if not ticker:
                # 부분 검색
                for n, t in db._db.items():
                    if name_query in n:
                        ticker = t
                        name_query = n
                        break

            if not ticker:
                await ctx.send(f"⚠️ '{name_query}' 종목을 찾을 수 없어요. 정확한 종목명을 입력해주세요.")
                return

            _add(ticker, name_query, entry, target, stop)
            await ctx.send(
                f"✅ **{name_query}** ({ticker}) 보유 등록 완료\n"
                f"진입가: {entry:,}원 | 목표가: {target:,}원 | 손절가: {stop:,}원\n"
                f"30분마다 매도 신호를 자동으로 체크합니다."
            )
        except Exception as e:
            await ctx.send(f"⚠️ 등록 오류: `{str(e)[:100]}`")

    # ── !트레일링 커맨드 ────────────────────
    @bot.command(name="트레일링", aliases=["trailing", "ts"])
    async def trailing_status(ctx):
        """현재 트레일링 스탑 현황"""
        from src.utils.trailing_stop import get_trailing_manager
        from src.api.kis_client import KISClient

        tm = get_trailing_manager()
        stops = tm.get_status()

        if not stops:
            await ctx.send("📭 현재 트레일링 스탑 등록된 종목 없음\n`09:10` 추천 알림 이후 자동 등록됩니다.")
            return

        kis = KISClient()
        lines = ["**🎯 트레일링 스탑 현황**\n"]
        for s in stops:
            try:
                current  = kis.get_stock_price(s["ticker"])["price"]
                entry    = s["entry_price"]
                high     = s["high_price"]
                gain     = (current - entry) / entry * 100
                active   = s.get("trailing_active", False)
                p1_done  = s.get("partial_1_done", False)
                p2_done  = s.get("partial_2_done", False)
                p1_price = s.get("partial_1_price", 0)
                p2_price = s.get("partial_2_price", 0)
                trail_s  = s.get("trailing_stop", 0)

                # 상태 표시
                if active:
                    status = "🚀 트레일링 중 (20% 보유)"
                elif p2_done:
                    status = "✅ 2차 익절 완료 (20% 잔여)"
                elif p1_done:
                    status = "✂️ 1차 익절 완료 (50% 잔여)"
                else:
                    status = "⏳ 대기 중 (100% 보유)"

                # 진행 단계 표시
                stage = ""
                if not p1_done:
                    stage = f"　1차 익절 목표: {p1_price:,}원 (+4%)"
                elif not p2_done:
                    stage = f"　2차 익절 목표: {p2_price:,}원 (+8%)"
                elif trail_s:
                    stage = f"　트레일링 손절: {trail_s:,}원"

                line = (
                    f"{status} **{s['name']}**\n"
                    f"　진입: {entry:,} → 현재: {current:,} ({gain:+.1f}%)\n"
                    f"{stage}"
                )
                lines.append(line)
            except Exception:
                lines.append(f"• {s['name']}: 조회 실패")

        await ctx.send("\n".join(lines))

    # ── !국면 커맨드 ─────────────────────────
    @bot.command(name="국면", aliases=["regime", "r"])
    async def market_regime(ctx):
        """현재 시장 국면 확인"""
        thinking = await ctx.send("🗺️ 시장 국면 분석 중...")
        try:
            from src.analyzers.regime_classifier import MarketRegimeClassifier
            loop = asyncio.get_event_loop()
            classifier = MarketRegimeClassifier()
            result = await loop.run_in_executor(
                None, lambda: classifier.classify(use_cache=False)
            )
            label    = result.get("regime_label", "")
            score    = result.get("regime_score", 0)
            strategy = result.get("strategy", "")
            signals  = result.get("signals", [])
            ma_slope = result.get("ma_slope_pct", 0)
            drawdown = result.get("drawdown_pct", 0)
            vix      = result.get("vix", 0)
            picks_m  = result.get("picks_multiplier", 1.0)

            signal_text = "\n".join(f"• {s}" for s in signals[:6])

            embed = discord.Embed(
                title=f"🗺️ 시장 국면: {label}",
                color=0x00C851 if score >= 2 else (0xFF4444 if score <= -4 else 0xFFD700),
            )
            embed.add_field(
                name="📊 국면 지표",
                value=(
                    f"국면점수: **{score}점**\n"
                    f"MA기울기: {ma_slope:+.2f}%\n"
                    f"고점낙폭: {drawdown:.1f}%\n"
                    f"VIX: {vix:.1f}"
                ),
                inline=True,
            )
            embed.add_field(
                name="⚙️ 전략 조정",
                value=(
                    f"추천종목: 기본 × {picks_m:.1f}배\n"
                    f"전략: {strategy}"
                ),
                inline=True,
            )
            embed.add_field(
                name="📌 판단 신호",
                value=signal_text or "없음",
                inline=False,
            )
            try:
                await thinking.delete()
            except Exception:
                pass
            await ctx.send(embed=embed)
            logger.info(f"국면 조회: {label} by {ctx.author}")
        except Exception as e:
            await thinking.edit(content=f"⚠️ 오류: `{str(e)[:100]}`")

    # ── !IC분석 커맨드 ────────────────────────
    @bot.command(name="IC분석", aliases=["ic", "factor"])
    async def ic_analysis(ctx):
        """팩터별 IC(수익 예측력) 분석"""
        from src.utils.factor_ic import format_report
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(None, format_report)
        msg = "```\n" + report + "\n```"
        await ctx.send(msg)

    # ── !포지션 커맨드 ────────────────────────
    @bot.command(name="포지션", aliases=["position", "pos"])
    async def position_size(ctx, *, query: str = None):
        """
        총 투자금액 입력 시 추천 종목 배분 금액 계산
        사용법: !포지션 1000000 (100만원 기준)
        """
        if not query:
            await ctx.send(
                "📌 사용법: `!포지션 [총투자금액]`\n"
                "예) `!포지션 1000000` (100만원 기준 배분)"
            )
            return

        try:
            capital = int(query.replace(",", "").replace("원", "").strip())
        except ValueError:
            await ctx.send("⚠️ 숫자로 입력해주세요. 예) `!포지션 1000000`")
            return

        # today_picks에서 포지션 정보 가져오기
        import json
        from pathlib import Path
        today_file = Path("/app/data/today_picks.json")

        if not today_file.exists():
            await ctx.send("⚠️ 오늘 추천 종목이 없어요. 09:10 추천 알림 이후 사용하세요.")
            return

        with open(today_file) as f:
            picks = json.load(f)

        if not picks:
            await ctx.send("⚠️ 오늘 추천 종목이 없어요.")
            return

        # 포지션 사이징 재계산
        from src.utils.position_sizer import calculate_position_sizes
        picks = calculate_position_sizes(picks)

        lines = [f"💰 **총 투자금액: {capital:,}원** 기준 배분\n"]
        total_amount = 0
        for p in picks:
            pct = p.get("position_pct", 0)
            amount = int(capital * pct / 100)
            shares = amount // p.get("price", 1) if p.get("price", 0) > 0 else 0
            label = p.get("position_label", "")
            total_amount += amount
            lines.append(
                f"{label} **{p.get('name', '')}** ({p.get('ticker', '')})\n"
                f"　비중: {pct:.1f}% → **{amount:,}원** (약 {shares}주)"
            )

        lines.append(f"\n합계: {total_amount:,}원 / {capital:,}원")
        await ctx.send("\n".join(lines))

    # ── !도움말 커맨드 ────────────────────────
    @bot.command(name="도움말", aliases=["help", "h"])
    async def help_cmd(ctx):
        embed = discord.Embed(
            title="📖 Stock Alert Bot 명령어",
            color=0x2196F3,
        )
        embed.add_field(
            name="🔍 종목 분석",
            value=(
                "`!분석 [종목명]` — 종목명으로 분석\n"
                "`!분석 [종목코드]` — 6자리 코드로 분석\n"
                "예) `!분석 삼성전자` / `!분석 005930`"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚖️ 종목 비교",
            value=(
                "`!비교 [종목1] [종목2]` — 두 종목 비교\n"
                "예) `!비교 삼성전자 SK하이닉스`"
            ),
            inline=False,
        )

        embed.add_field(
            name="📋 주간 리포트 / 전략",
            value=(
                "`!주간리포트` — 이번 주 성과 분석 + AI 파라미터 제안\n"
                "`!전략적용` — AI 제안 파라미터 적용\n"
                "`!전략현황` — 현재 전략 파라미터 확인\n"
                "`!IC분석` — 팩터 수익 예측력 분석\n"
                "`!포지션 [금액]` — 투자금 기준 배분 금액 계산\n"
                "`!국면` — 현재 시장 국면 분석\n"
                "`!트레일링` — 트레일링 스탑 현황\n"
                "`!보유 [종목명] [진입가]` — 보유 종목 등록/조회\n"
                "`!추적 [종목명]` — 매수 타이밍 추적 등록/조회\n"
                "`!시뮬레이션 [일수]` — 시뮬레이션 매매 통계 (기본 7일)\n"
                "`!학습규칙` — 자동 학습된 전략 규칙 조회\n"
                "`!데이터수집` — 3단계 분석 데이터 누적 현황"
            ),
            inline=False,
        )
        embed.set_footer(text="분석에 10~20초 소요 | KIS API + GPT-4o-mini")
        await ctx.send(embed=embed)

    return bot


# ─────────────────────────────────────────
# 백그라운드 스레드로 봇 실행
# ─────────────────────────────────────────
def run_bot_in_thread():
    """별도 스레드에서 Discord 봇 이벤트 루프 실행"""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        logger.warning("DISCORD_BOT_TOKEN 미설정 - Discord 봇 비활성화")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = create_bot()
    try:
        logger.info("Discord 봇 시작")
        loop.run_until_complete(bot.start(token))
    except Exception as e:
        logger.error(f"Discord 봇 오류: {e}")
    finally:
        loop.close()


def start_discord_bot():
    """메인에서 호출 - 데몬 스레드로 봇 기동"""
    thread = threading.Thread(target=run_bot_in_thread, daemon=True, name="discord-bot")
    thread.start()
    logger.info("Discord 봇 스레드 시작됨")
    return thread
