"""장 마감 후 잡 (15:35 종가손절 / 16:00 요약 / 20:05 수급 / 20:10 시뮬 / 금 20:20 리뷰)

수집·시뮬 잡은 KRX 애프터마켓(2026-09-14~, 16:00~20:00)이 끝난 뒤에 돈다. 장후 체결이
거래량과 투자자 수급에 더해지므로(2026-09-10 실측) 16시대에 읽으면 미확정 값이다.

main.py 에서 옮겨왔다. 등록과 실행 순서는 main.setup_schedule 이 계속 관장한다.
"""
import os
from loguru import logger
from src.notifier.discord import DiscordNotifier
from src.utils.trailing_stop import get_trailing_manager
from src.utils.trade_simulator import get_simulator
from src.utils.detailed_collector import update_trade_outcome
from src.utils.tracker import save_results
from src.utils.session_state import sync_today_picks as _sync_today_picks




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
    """20:10 시뮬레이션 진행 업데이트 (애프터마켓 종료 후 확정 일봉 기준)"""
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
    """16:00 마감 요약 (정규장 종가 기준)"""
    if not _sync_today_picks():
        return

    from src.api.kis_client import KISClient
    kis = KISClient()
    notifier = DiscordNotifier()

    # 종가는 '현재가'가 아니라 일봉의 공식 종가로 읽는다. 16:00 은 KRX 애프터마켓
    # (2026-09-14~) 개장 시각이라, 스케줄러가 몇 초만 늦어도 현재가가 저녁 첫 체결가일 수 있다.
    # (예전에는 같은 종목의 현재가를 알림용·저장용으로 두 번씩 조회했다)
    results = []
    for pick in _sync_today_picks():
        try:
            oc = kis.get_official_close(pick["ticker"])
            if oc:
                results.append({"close_price": oc["close"], "change_rate": oc["change_rate"]})
                continue
            cur = kis.get_stock_price(pick["ticker"])
            logger.warning(f"[{pick['ticker']}] 당일 일봉 없음 - 현재가로 대체")
            results.append({"close_price": cur["price"], "change_rate": cur["change_rate"]})
        except Exception as e:
            logger.warning(f"[{pick.get('ticker')}] 종가 조회 실패 - 추천가로 대체: {e}")
            results.append({"close_price": pick["price"], "change_rate": 0.0})

    notifier.send_closing_summary(_sync_today_picks(), results)
    save_results(results)
    logger.info("마감 요약 전송 완료")




def run_supply_collection():
    """20:05 일별 수급 데이터 자동 수집 (애프터마켓 종료 후 확정 수급)"""
    try:
        from src.utils.supply_collector import collect_daily_supply
        logger.info("일별 수급 데이터 수집 시작")
        # backfill=True: 호출당 받아오는 30일치를 모두 저장한다.
        # 누락된 과거 날짜가 있으면 자동으로 메워지므로 재실행에 안전하다.
        result = collect_daily_supply(
            top_n=int(os.getenv("SUPPLY_COLLECT_TOP_N", "300")),
            backfill=True,
        )
        count = result.get("count", 0)
        logger.info(f"수급 데이터 수집 완료: {count}종목")

        # 다음 날 아침 브레드스 게이트가 쓸 전 유니버스 브레드스 (전일 종가 기준)
        try:
            from src.utils.market_breadth import compute_universe_breadth
            compute_universe_breadth()
        except Exception as e:
            logger.warning(f"전 유니버스 브레드스 산출 실패 - 내일은 후보 풀 기준으로 대체: {e}")

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
    """금요일 20:20 주간 자동 리뷰"""
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


def run_aftermarket_snapshot():
    """15:40 애프터마켓 반영 점검 - 정규장 확정 직후 스냅샷"""
    if os.getenv("AFTERMARKET_CHECK", "1") == "0":
        return
    try:
        from src.utils.aftermarket_check import snapshot
        snapshot("close")
    except Exception as e:
        logger.warning(f"애프터마켓 점검 스냅샷 실패: {e}")


def run_aftermarket_compare():
    """20:02 애프터마켓 반영 점검 - 종료 직후 스냅샷을 찍어 15:40 과 비교"""
    if os.getenv("AFTERMARKET_CHECK", "1") == "0":
        return
    try:
        from src.utils.aftermarket_check import compare
        compare("close", "evening")
    except Exception as e:
        logger.warning(f"애프터마켓 점검 비교 실패: {e}")
