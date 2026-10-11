"""AI(GPT) 호출 장애 감지와 디스코드 알림

2026-10-01~09, OpenAI 크레딧이 소진돼 AI 평가가 9거래일 내내 실패했는데
사용자에게는 아무 신호도 가지 않았다. ai_evaluator 와 macro_agent 가 예외를
내부에서 삼키고 기본값(_fallback / _neutral_result)을 돌려주기 때문에,
추천이 0건이 되어도 화면에는 "최소 점수 충족 종목 없음"으로만 보였다.
'AI가 죽은 날'과 '추천할 종목이 없던 날'이 구별되지 않았고 9거래일을 날렸다.

여기서는 호출 결과만 기록하고, 장애로 보일 때 한 번만 알린다.
  · 크레딧 소진·인증 오류: 원인이 분명하므로 첫 실패에 즉시 알림
  · 그 외(일시적 오류·레이트리밋): 같은 날 3회부터 알림
  · 원인 종류별로 하루 1회만 (09:10·13:00 두 번 울리지 않게)
  · 알림을 보낸 날에 호출이 성공하면 복구 알림 1회

이 모듈은 절대 호출부로 예외를 올리지 않는다. 감시 장치가 본체를 멈추면 안 된다.
"""
import json
from datetime import datetime
from pathlib import Path

from loguru import logger

STATE_FILE = Path("data/ai_health.json")
OTHER_THRESHOLD = 3          # 원인 불명 오류는 하루 3회부터 알림

_KINDS = [
    ("quota", ("insufficient_quota", "credit_balance_exhausted", "no credits remaining",
               "exceeded your current quota", "billing")),
    ("auth",  ("invalid_api_key", "incorrect api key", "401", "authentication")),
    ("rate",  ("rate_limit", "429", "too many requests")),
]
_LABEL = {
    "quota": "크레딧 소진 / 결제 한도",
    "auth":  "API 키 인증 실패",
    "rate":  "호출 한도 초과",
    "other": "원인 불명",
}
_ACTION = {
    "quota": "OpenAI 결제 상태를 확인하세요. 복구 전까지 추천이 나오지 않습니다.",
    "auth":  ".env 의 OPENAI_API_KEY 를 확인하세요. 복구 전까지 추천이 나오지 않습니다.",
    "rate":  "잠시 후 자동 복구될 수 있습니다. 반복되면 모델·호출량을 확인하세요.",
    "other": "로그의 원문 오류를 확인하세요.",
}


def classify(error: str) -> str:
    text = str(error).lower()
    for kind, needles in _KINDS:
        if any(n in text for n in needles):
            return kind
    return "other"


def _load() -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        from src.utils.json_state import load_json_state
        state = load_json_state(STATE_FILE, {}, "ai_health")
    except Exception:
        state = {}
    if state.get("date") != today:          # 날이 바뀌면 초기화
        state = {"date": today, "fail": {}, "alerted": [], "recovered": False}
    return state


def _save(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"ai_health 상태 저장 실패: {e}")


def _notify(title: str, desc: str, color: int) -> None:
    try:
        from src.notifier.discord import DiscordNotifier
        DiscordNotifier()._send({"embeds": [{
            "title": title, "description": desc, "color": color,
            "footer": {"text": "AI 호출 상태 감시"},
        }]})
    except Exception as e:
        logger.warning(f"AI 장애 알림 전송 실패: {e}")


def record_failure(where: str, error) -> None:
    """AI 호출 실패 기록. 장애로 보이면 하루 한 번 알린다."""
    try:
        kind = classify(error)
        state = _load()
        state["fail"][kind] = state["fail"].get(kind, 0) + 1
        count = state["fail"][kind]
        logger.warning(f"AI 호출 실패 [{where}] {_LABEL[kind]} (오늘 {count}회): {str(error)[:200]}")

        threshold = 1 if kind in ("quota", "auth") else OTHER_THRESHOLD
        if count >= threshold and kind not in state["alerted"]:
            state["alerted"].append(kind)
            state["recovered"] = False
            _notify(
                f"🚨 AI 호출 장애 - {_LABEL[kind]}",
                f"**발생 위치:** {where}\n**오늘 실패:** {count}회\n"
                f"**원문:** `{str(error)[:300]}`\n\n{_ACTION[kind]}",
                0xFF4444,
            )
        _save(state)
    except Exception as e:
        logger.debug(f"ai_health 기록 실패: {e}")


def record_success(where: str) -> None:
    """AI 호출 성공. 오늘 장애 알림을 보냈다면 복구 알림을 한 번 보낸다."""
    try:
        state = _load()
        if not state.get("alerted") or state.get("recovered"):
            return
        state["recovered"] = True
        _notify("✅ AI 호출 정상 복구",
                f"**위치:** {where}\n오늘 장애 알림 이후 호출이 다시 성공했습니다.", 0x00C851)
        _save(state)
    except Exception as e:
        logger.debug(f"ai_health 복구 기록 실패: {e}")
