"""
Stock Alert Bot - 메인 진입점
스케줄러 기반 자동 실행:
  - 시작 시: 전종목 DB 빌드
  - 08:00: 전종목 DB 갱신
  - 08:30: 장전 추천 종목 알림
  - 장중 30분 간격: 목표가/손절 체크
  - 16:00: 마감 요약
  - Discord 봇: 수시 !분석 명령어 응답
"""
import os
import sys
import time
import signal
import threading
import schedule
import pytz
from datetime import datetime
from loguru import logger
from dotenv import load_dotenv

from src.analyzers.screener import StockScreener
from src.analyzers.ai_evaluator import AIEvaluator
from src.analyzers.macro_agent import MacroAgent
from src.notifier.discord import DiscordNotifier
from src.utils.trailing_stop import get_trailing_manager
from src.utils.sell_signal import SellSignalDetector, load_holdings
from src.utils.watchlist import get_watchlist_manager
from src.analyzers.surge_detector import SurgeDetector
from src.analyzers.premarket import PremarketAnalyzer
from src.utils.trade_simulator import get_simulator
from src.utils.detailed_collector import (
    record_detailed_trade, update_trade_outcome, check_stage3_readiness
)
from src.notifier.discord_bot import start_discord_bot
from src.utils.tracker import save_picks, save_results

load_dotenv()

# 로그 설정
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}", level="INFO")
logger.add("logs/app.log", rotation="1 day", retention="30 days", level="DEBUG")

KST = pytz.timezone("Asia/Seoul")
today_picks: list[dict] = []
macro_result: dict = {"judgment": "neutral", "recommended_picks": 5}


def _restore_today_picks():
    """컨테이너 재시작 시 오늘 추천 종목 복구"""
    global today_picks
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt
    _today_file = _Path("data/today_picks.json")
    if _today_file.exists():
        try:
            mtime = _dt.fromtimestamp(_today_file.stat().st_mtime)
            if mtime.date() == _dt.now().date():
                with open(_today_file) as _f:
                    today_picks = _json.load(_f)
                logger.info(f"오늘 추천 종목 복구: {[p.get('name','') for p in today_picks]}")
        except Exception as e:
            logger.debug(f"today_picks 복구 실패: {e}")


# ─────────────────────────────────────────
# 전종목 DB 초기화
# ─────────────────────────────────────────
def init_stock_db():
    """시작 시 전종목 DB 로드 (백그라운드)"""
    def _load():
        try:
            from src.utils.stock_db import get_db
            db = get_db()
            db.load()
            logger.info(f"✅ 전종목 DB 준비 완료: {db.total_count():,}개 종목")
        except Exception as e:
            logger.error(f"전종목 DB 초기화 실패: {e}")

    thread = threading.Thread(target=_load, daemon=True, name="db-init")
    thread.start()


def rebuild_stock_db():
    """매일 08:00 전종목 DB 갱신 (신규 상장 반영)"""
    try:
        from src.utils.stock_db import rebuild_db
        logger.info("전종목 DB 갱신 시작")
        rebuild_db()
        logger.info("전종목 DB 갱신 완료")
    except Exception as e:
        logger.error(f"DB 갱신 실패: {e}")


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


def run_macro_analysis():
    """09:07 거시경제 판단"""
    global macro_result
    logger.info("거시경제 판단 시작")
    try:
        agent = MacroAgent()
        macro_result = agent.analyze()

        # 국면 분류 실행 (거시 판단과 연계)
        try:
            from src.analyzers.regime_classifier import MarketRegimeClassifier
            classifier = MarketRegimeClassifier()
            regime_result = classifier.classify(use_cache=False)
            macro_result["regime"] = regime_result
            logger.info(f"시장 국면: {regime_result['regime_label']}")
        except Exception as e:
            logger.warning(f"국면 분류 오류: {e}")

        # Discord로 거시 판단 결과 전송
        notifier = DiscordNotifier()
        judgment = macro_result.get("judgment", "neutral")
        confidence = macro_result.get("confidence", 0)
        summary = macro_result.get("market_summary", "")
        caution = macro_result.get("caution_message", "")
        key_factors = macro_result.get("key_factors", [])
        top_news = macro_result.get("top_news", [])

        emoji = {"risk_on": "🟢", "risk_off": "🔴", "neutral": "🟡"}.get(judgment, "🟡")
        label = {"risk_on": "Risk On (매수 우호)", "risk_off": "Risk Off (매수 자제)", "neutral": "중립 (선별 접근)"}.get(judgment, "중립")

        # 거시 지표 데이터
        mdata = macro_result.get("market_data", {})
        vix_val    = mdata.get("VIX",    {}).get("value", "N/A")
        vix_chg    = mdata.get("VIX",    {}).get("change_pct", 0)
        usdkrw_val = mdata.get("USDKRW", {}).get("value", "N/A")
        usdkrw_chg = mdata.get("USDKRW", {}).get("change_pct", 0)
        nasdaq_chg = mdata.get("NASDAQ", {}).get("change_pct", 0)
        sp500_chg  = mdata.get("SP500",  {}).get("change_pct", 0)
        us10y_val  = mdata.get("US10Y",  {}).get("value", "N/A")
        vix_signal = macro_result.get("vix_signal", "알 수 없음")

        vix_emoji = "🟢" if vix_signal == "안정" else ("🟡" if vix_signal == "경계" else "🔴")

        indicator_text = (
            f"{vix_emoji} VIX: {vix_val} ({vix_signal}) {vix_chg:+.1f}%\n"
            f"💵 원달러: {usdkrw_val:,.0f}원 {usdkrw_chg:+.1f}%\n"
            f"📈 나스닥: {nasdaq_chg:+.2f}% | S&P500: {sp500_chg:+.2f}%\n"
            f"🏦 미국10년물: {us10y_val}%"
        ) if mdata else "데이터 수집 실패"

        factor_text     = "\n".join(f"• {f}" for f in key_factors[:3]) or "없음"
        news_text_short = "\n".join(f"• {n[:40]}" for n in top_news[:3]) or "없음"
        value_text      = f"확신도: **{confidence}%**\n{summary}"

        fields = [
            {
                "name": f"{emoji} 시장 판단: {label}",
                "value": value_text,
                "inline": False,
            },
            {
                "name": "📊 글로벌 지표",
                "value": indicator_text,
                "inline": False,
            },
            {
                "name": "📌 핵심 요인",
                "value": factor_text,
                "inline": True,
            },
            {
                "name": "📰 주요 뉴스",
                "value": news_text_short,
                "inline": True,
            },
        ]
        if caution:
            fields.append({
                "name": "⚠️ 주의사항",
                "value": caution,
                "inline": False,
            })

        # 국면 정보 필드 추가
        regime = macro_result.get("regime", {})
        if regime:
            regime_signals = "\n".join(f"• {s}" for s in regime.get("signals", [])[:4])
            fields.append({
                "name": f"🗺️ 시장 국면: {regime.get('regime_label', '')}",
                "value": (
                    f"국면점수: {regime.get('regime_score', 0)}점\n"
                    f"전략: {regime.get('strategy', '')}\n"
                    f"{regime_signals}"
                ),
                "inline": False,
            })

        color = {"risk_on": 0x00C851, "risk_off": 0xFF4444, "neutral": 0xFFD700}.get(judgment, 0xFFD700)
        notifier._send({
            "embeds": [{
                "title": "🔭 오늘의 거시경제 + 시장 국면 판단",
                "color": color,
                "fields": fields,
                "footer": {"text": f"뉴스 {macro_result.get('news_count', 0)}건 분석 | GPT-4o-mini"},
            }]
        })
        logger.info(f"거시 판단 완료: {judgment} ({confidence}%)")

    except Exception as e:
        logger.exception(f"거시 판단 오류: {e}")
        macro_result = {"judgment": "neutral", "recommended_picks": int(os.getenv("FINAL_PICKS", "5"))}


def run_morning_analysis():
    """09:10 장전 분석 및 알림"""
    global today_picks
    notifier = DiscordNotifier()
    logger.info("장전 분석 시작")
    try:
        # 거시 판단 반영
        judgment = macro_result.get("judgment", "neutral")
        recommended = macro_result.get("recommended_picks", int(os.getenv("FINAL_PICKS", "5")))

        screener = StockScreener()
        screener.final_picks = recommended  # 거시 판단에 따라 종목 수 조정
        picks = screener.run()

        if not picks:
            logger.warning("추천 종목 없음")
            notifier.send_error_alert("오늘 조건 충족 종목 없음", context="장전 분석")
            return

        today_picks = picks
        ai = AIEvaluator()
        market_trend = _get_market_trend()
        summary = ai.generate_market_summary(picks, market_trend)
        notifier.send_morning_picks(picks, summary)
        save_picks(picks)  # 성과 추적용 저장

        # 트레일링 스탑 등록
        try:
            tm = get_trailing_manager()
            tm.clear_old()
            for pick in picks:
                tm.add(pick)
            logger.info(f"트레일링 스탑 등록: {len(picks)}종목")
        except Exception as e:
            logger.warning(f"트레일링 등록 오류: {e}")

        # 시뮬레이터에 가상 매매 등록 (주간 분석용)
        try:
            sim = get_simulator()
            regime = "unknown"
            try:
                from src.analyzers.regime_classifier import MarketRegimeClassifier
                regime_data = MarketRegimeClassifier().classify(use_cache=True)
                regime = regime_data.get("regime", "unknown")
            except Exception:
                pass
            sim.register_picks(picks, regime=regime)

            # 상세 데이터 수집 (3단계 준비)
            for pick in picks:
                try:
                    regime_data = None
                    try:
                        from src.analyzers.regime_classifier import MarketRegimeClassifier
                        regime_data = MarketRegimeClassifier().classify(use_cache=True)
                    except Exception:
                        pass
                    record_detailed_trade(
                        pick=pick,
                        candles=pick.get("_candles", []),
                        regime=regime_data,
                    )
                except Exception as e:
                    logger.debug(f"상세 데이터 기록 오류: {e}")

        except Exception as e:
            logger.warning(f"시뮬레이터 등록 오류: {e}")

        # today_picks 파일 백업 (컨테이너 재시작 대비)
        import json as _json
        from pathlib import Path as _Path
        _today_file = _Path("data/today_picks.json")
        _today_file.parent.mkdir(exist_ok=True)
        with open(_today_file, "w") as _f:
            _json.dump(picks, _f, ensure_ascii=False, default=str)

        logger.info(f"장전 알림 완료: {[p['name'] for p in picks]}")

    except Exception as e:
        logger.exception(f"장전 분석 오류: {e}")
        notifier.send_error_alert(str(e), context="run_morning_analysis")


def run_realtime_check():
    """장중 실시간 목표가/손절/트레일링 체크"""
    from src.api.kis_client import KISClient

    if not today_picks:
        return
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    tm = get_trailing_manager()

    # ── 트레일링 스탑 체크 ───────────────────────────────
    try:
        def _get_price(ticker: str) -> int:
            return kis.get_stock_price(ticker)["price"]

        signals = tm.check_all(_get_price)
        for sig in signals:
            stype = sig.get("signal")
            name  = sig.get("name", "")
            price = sig.get("current_price", 0)
            gain  = sig.get("gain_pct", 0)

            if stype == "partial_exit_1":
                sell_pct  = sig.get("sell_pct", 50)
                new_stop  = sig.get("new_stop", 0)
                next_tgt  = sig.get("next_target", 0)
                notifier._send({"embeds": [{
                    "title": f"✂️ 1차 분할 익절: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"💰 **{sell_pct}% 매도 권장** (보유분의 절반)\n"
                        f"손절가 → 진입가({new_stop:,}원)로 상향 (본전 손절)\n"
                        f"2차 익절 목표: `{next_tgt:,}원` (+8%)"
                    ),
                    "color": 0x00C851,
                }]})

            elif stype == "partial_exit_2":
                sell_pct   = sig.get("sell_pct", 30)
                trail_stop = sig.get("trailing_stop", 0)
                notifier._send({"embeds": [{
                    "title": f"✂️ 2차 분할 익절: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"💰 **{sell_pct}% 추가 매도 권장**\n"
                        f"나머지 20% → 트레일링 스탑 전환\n"
                        f"트레일링 손절가: `{trail_stop:,}원` (고점 -3%)\n"
                        f"🚀 대박 수익을 노려요!"
                    ),
                    "color": 0x4CAF50,
                }]})

            elif stype == "trailing_activated":
                trail_stop = sig.get("trailing_stop", 0)
                notifier._send({"embeds": [{
                    "title": f"🔄 트레일링 활성화: {name}",
                    "description": (
                        f"현재가: **{price:,}원** (+{gain:.1f}%)\n"
                        f"익절가 도달 → 트레일링 모드 전환\n"
                        f"트레일링 손절가: `{trail_stop:,}원` (고점 -3%)\n"
                        f"추가 수익을 노리며 보유 계속"
                    ),
                    "color": 0x00C851,
                }]})

            elif stype == "trailing_stop":
                high    = sig.get("high_price", 0)
                max_pct = sig.get("max_pct", 0)
                notifier._send({"embeds": [{
                    "title": f"🎯 트레일링 스탑 발동: {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"고점: {high:,}원 (+{max_pct:.1f}%)\n"
                        f"고점 대비 -3% 하락 → 나머지 20% 매도\n"
                        f"💡 분할 익절 전략 완료!"
                    ),
                    "color": 0x2196F3,
                }]})

                # [약점 7번] 청산 후 추적 종목으로 자동 전환
                # 강한 종목은 재조정 후 재진입 기회 포착
                try:
                    wm = get_watchlist_manager()
                    # 청산가를 기준으로 추적 등록 (재조정 기다림)
                    _pick_ref = next(
                        (p for p in today_picks if p.get("name") == name), {}
                    )
                    _score = _pick_ref.get("final_score", 0)
                    if _score >= 5.0:  # 점수 좋은 종목만 재추적
                        wm.add(
                            ticker=sig.get("ticker", ""),
                            name=name,
                            score=_score,
                            entry_price=price,
                            reason=f"청산 후 재추적 (최고 +{max_pct:.1f}%)"
                        )
                        notifier._send({"embeds": [{
                            "title": f"👀 재추적 등록: {name}",
                            "description": (
                                f"청산 완료 후 재조정 기다리는 중\n"
                                f"등록가: {price:,}원 | 목표진입: {int(price*0.97):,}원\n"
                                f"조정 + 반등 시 자동 알림"
                            ),
                            "color": 0x9C27B0,
                        }]})
                except Exception as e:
                    logger.debug(f"재추적 등록 오류: {e}")

            elif stype == "stop_warning":
                stop  = sig.get("stop_loss", 0)
                notifier._send({"embeds": [{
                    "title": f"⚠️ 손절 경고 (장중): {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"손절가: {stop:,}원 터치\n"
                        f"📌 아직 매도 X → 종가 확인 후 판단\n"
                        f"14:30 이후에도 손절가 하회 시 매도 권고"
                    ),
                    "color": 0xFF9800,
                }]})

            elif stype == "stop_loss":
                reason = sig.get("reason", "손절가 도달")
                notifier._send({"embeds": [{
                    "title": f"🛑 손절 신호: {name}",
                    "description": (
                        f"현재가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"{reason} → 매도 신호"
                    ),
                    "color": 0xFF4444,
                }]})

            elif stype == "stop_loss_close":
                stop = sig.get("stop_loss", 0)
                notifier._send({"embeds": [{
                    "title": f"🛑 종가 손절 확정: {name}",
                    "description": (
                        f"종가: **{price:,}원** ({gain:+.1f}%)\n"
                        f"손절가 {stop:,}원 하회 마감\n"
                        f"📌 내일 시초가에 매도 권고"
                    ),
                    "color": 0xFF4444,
                }]})

    except Exception as e:
        logger.debug(f"트레일링 체크 오류: {e}")

    # ── 기존 실시간 체크 (트레일링 미활성 종목) ───────────
    for pick in today_picks:
        try:
            ticker  = pick["ticker"]
            current = kis.get_stock_price(ticker)
            price   = current["price"]
            entry   = pick.get("entry_strategy", {}).get("split_1st", pick.get("price", 0))
            change  = (price - entry) / entry * 100 if entry else 0

            # 트레일링 활성 종목은 트레일링이 관리
            if tm.stops.get(ticker, {}).get("trailing_active"):
                continue

            es     = pick.get("entry_strategy", {})
            target = es.get("target_price", 0) or pick.get("ai_eval", {}).get("target_price", 0)
            stop   = es.get("stop_loss", 0) or pick.get("ai_eval", {}).get("stop_loss", 0)

            if target and price >= target:
                notifier.send_realtime_alert(
                    {**pick, "price": price, "change_rate": change},
                    f"🎯 목표가 도달! ({price:,}원) +{change:.1f}%"
                )
            elif stop and price <= stop:
                notifier.send_realtime_alert(
                    {**pick, "price": price, "change_rate": change},
                    f"🛑 손절 라인 도달! ({price:,}원) {change:.1f}%"
                )
            time.sleep(0.2)
        except Exception as e:
            logger.debug(f"실시간 체크 오류 ({pick.get('ticker','')}): {e}")

    # ── 보유 종목 매도 신호 감지 ─────────────────────────
    try:
        holdings = load_holdings()
        if holdings:
            detector = SellSignalDetector()
            for ticker, holding in holdings.items():
                try:
                    candles  = kis.get_daily_ohlcv(ticker, days=30)
                    investor = kis.get_investor_trend(ticker, days=5)
                    signals  = detector.detect(ticker, holding, candles, investor)

                    if not signals:
                        continue

                    severity = detector.evaluate_severity(signals)
                    if severity == "none":
                        continue

                    name = holding.get("name", ticker)
                    color = {
                        "high":   0xFF4444,
                        "medium": 0xFF9800,
                        "low":    0xFFD700,
                    }.get(severity, 0xFFD700)

                    signal_lines = []
                    for s in signals:
                        dot = "🔴" if s["severity"]=="high" else ("🟡" if s["severity"]=="medium" else "🔵")
                        signal_lines.append(f"{dot} **{s['message']}** → {s['action']}")
                    signal_text = "\n".join(signal_lines)

                    entry = holding.get("entry_price", 0)
                    current_price = candles[-1]["close"] if candles else 0
                    gain_pct = (current_price - entry) / entry * 100 if entry else 0

                    notifier._send({"embeds": [{
                        "title": f"⚠️ 매도 신호 감지: {name} ({ticker})",
                        "description": (
                            f"현재가: **{current_price:,}원** ({gain_pct:+.1f}%)\n"
                            f"진입가: {entry:,}원\n\n"
                            f"{signal_text}"
                        ),
                        "color": color,
                        "footer": {"text": f"신호 {len(signals)}개 감지 | 심각도: {severity}"},
                    }]})
                    logger.info(f"매도 신호 알림: {name} ({len(signals)}개)")
                    time.sleep(0.3)

                except Exception as e:
                    logger.debug(f"매도 신호 감지 오류 ({ticker}): {e}")

    except Exception as e:
        logger.debug(f"보유 종목 매도 신호 전체 오류: {e}")


def run_surge_detection():
    """장중 실시간 급등 감지 (5분마다)"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    detector = SurgeDetector()

    try:
        # 오늘 이미 추천된 종목 제외
        exclude = {p["ticker"] for p in today_picks}

        # 추적 종목도 제외 (중복 알림 방지)
        try:
            wm = get_watchlist_manager()
            exclude |= {item["ticker"] for item in wm.get_all()}
        except Exception:
            pass

        signals = detector.detect(kis, exclude_tickers=exclude)

        for sig in signals:
            name      = sig.get("name", "")
            ticker    = sig.get("ticker", "")
            price     = sig.get("price", 0)
            change    = sig.get("change_rate", 0)
            intraday  = sig.get("intraday_change", 0)
            vol_ratio = sig.get("volume_ratio", 0)
            trade_val = sig.get("trade_value_billion", 0)
            foreign   = sig.get("foreign_net", 0)
            inst      = sig.get("inst_net", 0)
            strength  = sig.get("strength", "medium")

            strength_emoji = "🔥" if strength == "strong" else "⚡"

            supply_text = ""
            if foreign > 0 and inst > 0:
                supply_text = f"외국인+기관 동반매수 ({foreign:+,}/{inst:+,})"
            elif foreign > 0:
                supply_text = f"외국인 매수 ({foreign:+,})"
            elif inst > 0:
                supply_text = f"기관 매수 ({inst:+,})"
            else:
                supply_text = "수급 미확인"

            notifier._send({"embeds": [{
                "title": f"{strength_emoji} 실시간 급등: {name} ({ticker})",
                "description": (
                    f"현재가: **{price:,}원** ({change:+.2f}%)\n"
                    f"시초가 대비: +{intraday:.2f}% (장중 모멘텀)\n"
                    f"거래량: 평균 대비 **{vol_ratio:.1f}배**\n"
                    f"거래대금: {trade_val:.0f}억원\n"
                    f"📊 {supply_text}\n\n"
                    f"💡 강도: **{strength.upper()}**"
                ),
                "color": 0xFF6F00 if strength == "strong" else 0xFFA000,
                "footer": {"text": f"실시간 감지 | {sig.get('detected_at')}"},
            }]})
            logger.info(f"급등 알림: {name} ({change:+.2f}%, 거래량 {vol_ratio:.1f}배)")

    except Exception as e:
        logger.debug(f"급등 감지 오류: {e}")


def run_watchlist_check():
    """장중 추적 종목 매수 타이밍 체크 (30분마다)"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    if not kis.is_market_open():
        return

    notifier = DiscordNotifier()
    wm = get_watchlist_manager()

    try:
        # 만료 종목 정리
        wm.clean_expired()

        # 타이밍 체크
        signals = wm.check_all(kis)

        for sig in signals:
            name        = sig.get("name", "")
            ticker      = sig.get("ticker", "")
            current     = sig.get("current", 0)
            pullback    = sig.get("pullback_pct", 0)
            rsi         = sig.get("rsi", 0)
            score       = sig.get("score", 0)
            reg_price   = sig.get("reg_price", 0)
            tgt_entry   = sig.get("target_entry", 0)
            is_bullish  = sig.get("is_bullish", False)
            reason      = sig.get("reason", "")
            bull_emoji  = "🕯️ 양봉" if is_bullish else "📉 음봉"

            notifier._send({"embeds": [{
                "title": f"🎯 매수 타이밍: {name} ({ticker})",
                "description": (
                    f"**등록 이유:** {reason}\n"
                    f"등록가: {reg_price:,}원 → 현재: **{current:,}원** "
                    f"({pullback:+.1f}% 조정)\n\n"
                    f"📊 RSI: {rsi:.1f} | {bull_emoji}\n"
                    f"✅ 눌림목 + 수급 유지 조건 충족\n\n"
                    f"💡 **권장 진입가: {tgt_entry:,}원**\n"
                    f"종합점수: {score:.1f}점"
                ),
                "color": 0x9C27B0,
                "footer": {"text": "추적 종목 매수 타이밍 알림"},
            }]})
            logger.info(f"추적 타이밍 알림: {name} ({pullback:+.1f}% 조정, RSI={rsi:.1f})")

    except Exception as e:
        logger.debug(f"추적 체크 오류: {e}")


def run_closing_stop_check():
    """15:35 장마감 후 종가 기준 손절 최종 체크"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    notifier = DiscordNotifier()
    tm = get_trailing_manager()
    try:
        def _get_price(ticker: str) -> int:
            return kis.get_stock_price(ticker)["price"]
        signals = tm.check_closing_stops(_get_price)
        for sig in signals:
            name  = sig.get("name", "")
            price = sig.get("current_price", 0)
            gain  = sig.get("gain_pct", 0)
            stop  = sig.get("stop_loss", 0)
            notifier._send({"embeds": [{
                "title": f"🛑 종가 손절 확정: {name}",
                "description": (
                    f"종가: **{price:,}원** ({gain:+.1f}%)\n"
                    f"손절가 {stop:,}원 하회 마감\n"
                    f"📌 내일 시초가에 매도 권고"
                ),
                "color": 0xFF4444,
            }]})
        cnt = len(signals)
        logger.info(f"종가 손절 확정: {cnt}종목" if cnt else "종가 손절 없음")
    except Exception as e:
        logger.exception(f"종가 손절 체크 오류: {e}")


def run_simulation_update():
    """16:10 시뮬레이션 진행 업데이트 (마감가 기준)"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    try:
        sim = get_simulator()
        closed = sim.update_daily(kis)
        if closed:
            notifier = DiscordNotifier()
            for trade in closed:
                pct = trade.get("realized_pct", 0)
                reason = trade.get("close_reason", "")
                reason_kr = {
                    "stop_loss":     "🛑 손절",
                    "trailing_stop": "🎯 트레일링",
                    "max_hold":      "⏰ 보유일 초과",
                }.get(reason, reason)
                events = trade.get("events", [])
                events_text = "\n".join(
                    f"  {e.get('date')}: {e.get('event')} ({e.get('pct'):+.1f}%)"
                    for e in events[-5:]
                )
                emoji = "🟢" if pct > 0 else "🔴"
                notifier._send({"embeds": [{
                    "title": f"{emoji} 시뮬레이션 청산: {trade['name']}",
                    "description": (
                        f"진입일: {trade['entry_date']} → 청산일: {trade.get('close_date')}\n"
                        f"진입가: {trade['entry_price']:,}원\n"
                        f"**누적 수익률: {pct:+.2f}%**\n"
                        f"청산 사유: {reason_kr}\n\n"
                        f"**상세 이벤트:**\n{events_text}"
                    ),
                    "color": 0x00C851 if pct > 0 else 0xFF4444,
                    "footer": {"text": "가상 매매 시뮬레이션 결과"},
                }]})
            # 상세 데이터에도 청산 결과 반영
            for trade in closed:
                try:
                    update_trade_outcome(
                        ticker=trade.get("ticker", ""),
                        entry_date=trade.get("entry_date", ""),
                        final_pct=trade.get("realized_pct", 0),
                        close_reason=trade.get("close_reason", ""),
                        events=trade.get("events", []),
                    )
                except Exception as e:
                    logger.debug(f"상세 청산 업데이트 오류: {e}")

            logger.info(f"시뮬레이션 청산: {len(closed)}건")
        else:
            logger.info("시뮬레이션 청산 없음 - 보유 지속")
    except Exception as e:
        logger.exception(f"시뮬레이션 업데이트 오류: {e}")


def run_closing_summary():
    """16:00 마감 요약"""
    if not today_picks:
        return

    from src.api.kis_client import KISClient
    kis = KISClient()
    notifier = DiscordNotifier()

    results = []
    for pick in today_picks:
        try:
            current = kis.get_stock_price(pick["ticker"])
            results.append({"close_price": current["price"]})
        except Exception:
            results.append({"close_price": pick["price"]})

    notifier.send_closing_summary(today_picks, results)

    # 성과 추적용 저장 - results에 종가 포함
    from src.api.kis_client import KISClient as _KIS
    _kis = _KIS()
    full_results = []
    for pick, result in zip(today_picks, results):
        try:
            current = _kis.get_stock_price(pick["ticker"])
            full_results.append({
                "close_price": current["price"],
                "change_rate": current["change_rate"],
            })
        except Exception:
            full_results.append(result)
    save_results(full_results)
    logger.info("마감 요약 전송 완료")


def run_afternoon_screening():
    """13:00 장중 보조 스크리닝 - 오전과 다른 종목 발굴"""
    from src.api.kis_client import KISClient
    kis = KISClient()
    if not kis.is_market_open():
        return

    logger.info("=" * 60)
    logger.info("장중 보조 스크리닝 시작 (13:00)")
    logger.info("=" * 60)

    notifier = DiscordNotifier()
    try:
        screener = StockScreener()
        # 보조 스크리닝은 더 엄격한 기준 적용
        screener.min_score = max(screener.min_score, 5.5)
        screener.final_picks = max(2, screener.final_picks - 2)

        afternoon_picks = screener.run()

        # 오전 추천과 중복 제외
        morning_tickers = {p.get("ticker") for p in today_picks}
        new_picks = [p for p in afternoon_picks
                     if p.get("ticker") not in morning_tickers]

        if not new_picks:
            logger.info("보조 스크리닝: 신규 종목 없음")
            return

        # 추가 종목 알림
        notifier._send({"embeds": [{
            "title": "🌤️ 장중 추가 추천 종목 (13:00)",
            "description": (
                f"오전과 다른 신규 종목 {len(new_picks)}개 발굴\n"
                f"오후 모멘텀 종목 추가 분석 결과"
            ),
            "color": 0xFFA500,
        }]})

        for i, pick in enumerate(new_picks, 1):
            embed = notifier._build_stock_embed(i, pick, is_realtime=False)
            embed["title"] = f"🌤️ 장중추가 #{i} {pick.get('name', '')} ({pick.get('ticker', '')})"
            notifier._send({"embeds": [embed]})

        # 트레일링 스탑에도 등록
        try:
            tm = get_trailing_manager()
            for pick in new_picks:
                tm.add(pick)
        except Exception as e:
            logger.warning(f"보조 트레일링 등록 오류: {e}")

        # today_picks에 추가
        today_picks.extend(new_picks)
        logger.info(f"장중 보조 추천: {[p['name'] for p in new_picks]}")

    except Exception as e:
        logger.exception(f"장중 보조 스크리닝 오류: {e}")


def run_supply_collection():
    """16:05 일별 수급 데이터 자동 수집"""
    try:
        from src.utils.supply_collector import collect_daily_supply
        logger.info("일별 수급 데이터 수집 시작")
        result = collect_daily_supply(top_n=100)
        count = result.get("count", 0)
        logger.info(f"수급 데이터 수집 완료: {count}종목")

        # Discord 알림 (간단히)
        notifier = DiscordNotifier()
        notifier._send({
            "embeds": [{
                "title": "💾 수급 데이터 저장 완료",
                "description": (
                    f"날짜: {result.get('date', '')}\n"
                    f"수집 종목: **{count}개**\n"
                    "백테스트 정확도 향상에 활용됩니다."
                ),
                "color": 0x2196F3,
            }]
        })
    except Exception as e:
        logger.exception(f"수급 데이터 수집 오류: {e}")


def run_weekly_review_auto():
    """금요일 16:30 주간 자동 리뷰"""
    try:
        from src.utils.weekly_review import run_weekly_review
        from src.notifier.discord_review import send_weekly_report
        logger.info("주간 자동 리뷰 시작")
        review_data = run_weekly_review()
        notifier = DiscordNotifier()
        send_weekly_report(review_data, notifier)

        # 3단계 알람 전송
        stage3 = review_data.get("stage3_alert")
        if stage3:
            color = 0x9C27B0 if stage3.get("trigger") else 0x2196F3
            title = "🎯 3단계 분석 준비 완료!" if stage3.get("trigger") else "📊 데이터 누적 진행 상황"
            notifier._send({"embeds": [{
                "title": title,
                "description": stage3.get("message", ""),
                "color": color,
                "footer": {"text": f"청산 데이터: {stage3.get('closed_count', 0)}건"},
            }]})
            logger.info(f"3단계 알람 전송: {stage3.get('closed_count')}건")

        logger.info("주간 자동 리뷰 완료")
    except Exception as e:
        logger.exception(f"주간 리뷰 오류: {e}")


def _get_market_trend() -> str:
    try:
        from src.api.kis_client import KISClient
        kis = KISClient()
        idx = kis.get_stock_price("0001")
        direction = "상승" if idx["change_rate"] > 0 else "하락"
        return f"코스피 {direction} ({idx['change_rate']:+.2f}%)"
    except Exception:
        return "시장 데이터 조회 불가"


def setup_schedule():
    morning = os.getenv("SCHEDULE_MORNING", "09:10")
    close = os.getenv("SCHEDULE_CLOSE", "16:00")
    interval = int(os.getenv("SCHEDULE_REALTIME_INTERVAL", "30"))

    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("09:05").do(rebuild_stock_db)
        getattr(schedule.every(), day).at("06:30").do(run_us_market_collection)
        getattr(schedule.every(), day).at("07:00").do(run_overnight_news_collection)
        getattr(schedule.every(), day).at("08:40").do(run_premarket_analysis)
        getattr(schedule.every(), day).at("09:07").do(run_macro_analysis)
        getattr(schedule.every(), day).at(morning).do(run_morning_analysis)
        getattr(schedule.every(), day).at(close).do(run_closing_summary)

    schedule.every(interval).minutes.do(run_realtime_check)
    schedule.every(interval).minutes.do(run_watchlist_check)
    schedule.every(5).minutes.do(run_surge_detection)  # 급등 감지는 5분마다
    schedule.every().friday.at("16:30").do(run_weekly_review_auto)
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at("16:05").do(run_supply_collection)
        getattr(schedule.every(), day).at("16:10").do(run_simulation_update)
        getattr(schedule.every(), day).at("13:00").do(run_afternoon_screening)
        getattr(schedule.every(), day).at("15:35").do(run_closing_stop_check)
    logger.info(f"스케줄 등록: 미국마감=06:30, 야간뉴스=07:00, 프리장=08:40, DB갱신=09:05, 거시판단=09:07, 장전={morning}, 보조스크리닝=13:00, 종가손절=15:35, 수급수집=16:05, 장후={close}, 장중={interval}분, 주간리뷰=금16:30")


def handle_shutdown(signum, frame):
    logger.info("종료 신호 수신 - 정상 종료")
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    logger.info("🚀 Stock Alert Bot 시작")
    logger.info(f"  환경: {'실서버' if os.getenv('KIS_IS_REAL', 'false') == 'true' else '모의투자'}")

    # 오늘 추천 종목 복구 (재시작 대비)
    _restore_today_picks()

    # 전종목 DB 백그라운드 초기화
    init_stock_db()

    notifier = DiscordNotifier()
    notifier.send_startup_message()

    # Discord 봇 시작 (별도 스레드)
    start_discord_bot()

    if "--run-now" in sys.argv:
        logger.info("즉시 실행 모드")
        run_morning_analysis()
        while True:
            time.sleep(60)

    setup_schedule()

    logger.info("스케줄러 루프 시작")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
