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

# 스케줄 잡은 시간대별 모듈로 분리했다 (src/jobs/).
from src.jobs.premarket_jobs import (
    run_us_market_collection, run_overnight_news_collection, run_premarket_analysis,
)
from src.jobs.screening_jobs import (
    init_stock_db, rebuild_stock_db, run_macro_analysis,
    run_morning_analysis, run_afternoon_screening,
)
from src.jobs.intraday_jobs import (
    run_realtime_check, run_surge_detection, run_watchlist_check,
)
from src.jobs.closing_jobs import (
    run_closing_stop_check, run_simulation_update, run_closing_summary,
    run_supply_collection, run_weekly_review_auto,
    run_aftermarket_snapshot, run_aftermarket_compare,
)

load_dotenv()

# 로그 설정
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}", level="INFO")
logger.add("logs/app.log", rotation="1 day", retention="30 days", level="DEBUG")

KST = pytz.timezone("Asia/Seoul")
from src.utils.session_state import (
    set_today_picks as _set_today_picks,
    sync_today_picks as _sync_today_picks,
    restore_today_picks as _restore_today_picks,
    mark_realtime_alert as _mark_realtime_alert,
    get_macro_result, set_macro_result,
)


def setup_schedule():
    morning = os.getenv("SCHEDULE_MORNING", "09:10")
    close = os.getenv("SCHEDULE_CLOSE", "16:00")
    interval = int(os.getenv("SCHEDULE_REALTIME_INTERVAL", "30"))
    # 확정 데이터를 읽는 장후 잡은 KRX 애프터마켓(2026-09-14~, 16:00~20:00)이 끝난 뒤에
    # 돈다. 장후 체결이 거래량·투자자 수급에 더해지므로(2026-09-10 실측) 16시대에
    # 읽으면 미확정 값이다. 16:00 장후 요약은 정규장 요약이라 그대로 둔다.
    supply_at = os.getenv("SCHEDULE_SUPPLY", "20:05")
    sim_at = os.getenv("SCHEDULE_SIM_UPDATE", "20:10")
    weekly_at = os.getenv("SCHEDULE_WEEKLY", "20:20")

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
    schedule.every().friday.at(weekly_at).do(run_weekly_review_auto)
    for day in ["monday", "tuesday", "wednesday", "thursday", "friday"]:
        getattr(schedule.every(), day).at(supply_at).do(run_supply_collection)
        getattr(schedule.every(), day).at(sim_at).do(run_simulation_update)
        # 애프터마켓이 KRX 일봉·수급에 어떻게 반영되는지 매일 점검 (정규장 확정 직후 vs 종료 직후)
        getattr(schedule.every(), day).at("15:40").do(run_aftermarket_snapshot)
        getattr(schedule.every(), day).at("20:02").do(run_aftermarket_compare)
        getattr(schedule.every(), day).at("13:00").do(run_afternoon_screening)
        getattr(schedule.every(), day).at("15:35").do(run_closing_stop_check)
    logger.info(f"스케줄 등록: 미국마감=06:30, 야간뉴스=07:00, 프리장=08:40, DB갱신=09:05, 거시판단=09:07, 장전={morning}, 보조스크리닝=13:00, 종가손절=15:35, 장후={close}, 애프터마켓점검=15:40/20:02, 수급수집={supply_at}, 시뮬={sim_at}, 장중={interval}분, 주간리뷰=금{weekly_at}")


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
    # 잡 하나가 예외를 흘리면 run_pending() 이 그대로 전파해 루프가 끝나고
    # 프로세스가 죽는다. 등록된 잡들이 각자 try 를 갖고 있긴 하지만 그건 잡
    # 본문 안의 얘기일 뿐, 진입 이전(인자 평가·지연 임포트)에서 터지면 못 막는다.
    consecutive_errors = 0
    while True:
        try:
            schedule.run_pending()
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            logger.exception(f"스케줄 실행 중 예외 (연속 {consecutive_errors}회): {e}")
            if consecutive_errors >= 20:
                # 같은 자리에서 계속 터지는 중이다. 조용히 도는 것보다
                # 죽어서 재시작되는 편이 낫다.
                logger.critical("스케줄 예외가 연속 20회 - 프로세스를 종료한다")
                raise
        time.sleep(30)


if __name__ == "__main__":
    main()
