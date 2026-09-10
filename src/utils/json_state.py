"""data/*.json 상태 파일 로더

active_trades / trailing_stops / watchlist / holdings 처럼 컨테이너 재시작을
넘겨 살아남아야 하는 파일을 읽는다.

여태 각 모듈이 이렇게 읽고 있었다:

    try:
        return json.load(f)
    except Exception:
        return {}

파일이 깨져 있어도 조용히 빈 dict 가 되고, 잠시 뒤 _save() 가 그 빈 dict 를
같은 경로에 덮어쓴다. 추적 중이던 거래·트레일링 상태가 흔적 없이 사라지고
로그에도 아무것도 남지 않는다. 실제로 그런 일이 있었는지 확인할 방법조차 없다.

파일이 없는 것(정상)과 읽을 수 없는 것(사고)을 구분하고, 후자는 원본을
남겨둔 채 크게 알린다.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from loguru import logger


def load_json_state(path: Path, default=None, label: str = ""):
    """상태 파일 읽기

    · 파일 없음        → default (조용히. 첫 실행이면 정상이다)
    · 읽기/파싱 실패   → default + ERROR 로그 + 원본을 .corrupt-<시각> 으로 보존

    보존해두면 이후 _save() 가 덮어써도 사람이 복구할 수 있다.
    """
    if default is None:
        default = {}
    path = Path(path)
    name = label or path.name

    if not path.exists():
        return default

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        backup = path.with_suffix(path.suffix + f".corrupt-{datetime.now():%Y%m%d_%H%M%S}")
        try:
            shutil.copy2(path, backup)
            kept = f" (원본 보존: {backup.name})"
        except Exception:
            kept = " (원본 보존 실패)"
        logger.error(
            f"상태 파일 손상 - {name}: {e}{kept}. "
            f"빈 값으로 시작하므로 이 파일이 담고 있던 상태는 유실된다."
        )
        return default
