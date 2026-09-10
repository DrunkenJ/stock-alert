"""종목 선정 잡 (09:05 DB 갱신 / 09:07 거시 / 09:10 장전 / 13:00 보조)

main.py 에서 옮겨왔다. 등록과 실행 순서는 main.setup_schedule 이 계속 관장한다.
"""
from src.jobs.closing_jobs import _get_market_trend
import os
import threading
from loguru import logger
from src.analyzers.screener import StockScreener
from src.analyzers.ai_evaluator import AIEvaluator
from src.analyzers.macro_agent import MacroAgent
from src.notifier.discord import DiscordNotifier
from src.utils.trailing_stop import get_trailing_manager
from src.utils.trade_simulator import get_simulator
from src.utils.detailed_collector import record_detailed_trade
from src.utils.tracker import save_picks
from src.utils.session_state import set_today_picks as _set_today_picks
from src.utils.session_state import sync_today_picks as _sync_today_picks
from src.utils.session_state import get_macro_result
from src.utils.session_state import set_macro_result




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




def run_macro_analysis():
    """09:07 거시경제 판단"""
    logger.info("거시경제 판단 시작")
    try:
        agent = MacroAgent()
        macro_result = agent.analyze()
        set_macro_result(macro_result)

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
        set_macro_result(macro_result)




def run_morning_analysis():
    """09:10 장전 분석 및 알림"""
    notifier = DiscordNotifier()
    logger.info("장전 분석 시작")
    try:
        # 거시 판단 반영
        judgment = get_macro_result().get("judgment", "neutral")
        recommended = get_macro_result().get("recommended_picks", int(os.getenv("FINAL_PICKS", "5")))

        screener = StockScreener()
        screener.final_picks = recommended  # 거시 판단에 따라 종목 수 조정
        picks = screener.run()

        if not picks:
            logger.warning("추천 종목 없음")
            # 전일 픽이 메모리에 남아 실시간 알림이 계속 발사되지 않도록 초기화
            _set_today_picks([])
            notifier.send_no_picks_notice(get_macro_result().get("regime", {}))
            return

        _set_today_picks(picks)
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

        # 파일 백업은 _set_today_picks()에서 이미 완료 (컨테이너 재시작 대비)
        logger.info(f"장전 알림 완료: {[p['name'] for p in picks]}")

    except Exception as e:
        logger.exception(f"장전 분석 오류: {e}")
        notifier.send_error_alert(str(e), context="run_morning_analysis")




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
        morning_tickers = {p.get("ticker") for p in _sync_today_picks()}
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

        # today_picks에 추가 (날짜 도장 갱신 + 파일 백업 포함)
        _set_today_picks(_sync_today_picks() + new_picks)
        logger.info(f"장중 보조 추천: {[p['name'] for p in new_picks]}")

    except Exception as e:
        logger.exception(f"장중 보조 스크리닝 오류: {e}")
