"""스케줄러 프로세스가 하루 동안 들고 있는 상태

main.py 의 모듈 전역(today_picks / today_picks_date / macro_result /
_realtime_alerted)을 옮겨온 것이다. 15개 스케줄 잡이 전부 `global` 로 같은
변수를 건드리고 있어서, 잡을 다른 파일로 옮기는 순간 전부 깨지는 구조였다.
상태를 여기 한 곳에 두고 함수로만 드나들게 한다.

프로세스가 죽으면 사라지는 값이지만 today_picks 만은 예외다. NAS 재부팅이나
컨테이너 재시작으로 그날의 추천을 잃으면 안 되므로 data/today_picks.json 에
같이 남긴다.
"""
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

TODAY_PICKS_FILE = Path("data/today_picks.json")

_today_picks: list[dict] = []
_today_picks_date: str = ""
_macro_result: dict = {"judgment": "neutral", "recommended_picks": 5}
_realtime_alerted: set = set()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ── 오늘 추천 종목 ────────────────────────────────────────
def set_today_picks(picks: list[dict]) -> None:
    """오늘 추천 종목 갱신 + 파일 백업 (날짜 도장 포함)"""
    global _today_picks, _today_picks_date
    _today_picks = picks
    _today_picks_date = _today()
    try:
        TODAY_PICKS_FILE.parent.mkdir(exist_ok=True)
        with open(TODAY_PICKS_FILE, "w", encoding="utf-8") as f:
            json.dump(picks, f, ensure_ascii=False, default=str)
    except Exception as e:
        logger.warning(f"today_picks 저장 실패 - 재시작 시 복구 불가: {e}")


def sync_today_picks() -> list[dict]:
    """날짜가 바뀌었으면 전일 추천 종목을 비운다.

    스케줄러는 재시작 없이 몇 주씩 도는 프로세스라, 픽이 0개인 날에도
    메모리의 today_picks 가 그대로 남으면 지난 종목의 목표가/손절가로
    실시간 알림이 매일 반복 발사된다.
    """
    global _today_picks, _today_picks_date
    today = _today()
    if _today_picks and _today_picks_date != today:
        logger.info(
            f"전일({_today_picks_date or '미상'}) 추천 종목 정리: "
            f"{[p.get('name', '') for p in _today_picks]}"
        )
        _today_picks = []
        _today_picks_date = today
    return _today_picks


def restore_today_picks() -> None:
    """컨테이너 재시작 시 오늘 추천 종목 복구"""
    global _today_picks, _today_picks_date
    if not TODAY_PICKS_FILE.exists():
        return
    try:
        mtime = datetime.fromtimestamp(TODAY_PICKS_FILE.stat().st_mtime)
        if mtime.date() != datetime.now().date():
            return          # 어제 것이면 복구하지 않는다
        from src.utils.json_state import load_json_state
        _today_picks = load_json_state(TODAY_PICKS_FILE, [], "today_picks")
        _today_picks_date = _today()
        logger.info(f"오늘 추천 종목 복구: {[p.get('name', '') for p in _today_picks]}")
    except Exception as e:
        logger.warning(f"today_picks 복구 실패 - 오늘 추천 없이 시작: {e}")


# ── 거시 판단 ─────────────────────────────────────────────
def get_macro_result() -> dict:
    return _macro_result


def set_macro_result(result: dict) -> None:
    global _macro_result
    _macro_result = result


# ── 실시간 알림 중복 방지 ─────────────────────────────────
def mark_realtime_alert(ticker: str, kind: str) -> bool:
    """같은 종목/트리거는 하루 1회만 발송

    목표가·손절가는 한 번 도달하면 그 뒤로 계속 조건을 만족하므로,
    체크 주기(기본 15분)마다 같은 알림이 반복 발사되는 것을 막는다.
    Returns: True 면 발송해도 되는 첫 알림
    """
    today = _today()
    key = f"{today}_{ticker}_{kind}"
    if key in _realtime_alerted:
        return False
    # 전일 기록만 정리 (같은 날 다른 종목 기록은 유지)
    for old in [k for k in _realtime_alerted if not k.startswith(today)]:
        _realtime_alerted.discard(old)
    _realtime_alerted.add(key)
    return True
