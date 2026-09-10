"""시장 브레드스 (전 유니버스 20MA 상회 비율)

스크리너의 브레드스 게이트가 쓰는 값이다. 백테스트 엔진과 같은 정의, 같은
모집단으로 잰다: 종목 DB 의 일반 종목 전체, 종가 > 20일 단순이동평균.

예전 라이브 게이트는 거래량·시총 랭킹으로 뽑힌 후보 34~37개만 셌다. 원래 강한
종목들이라 50~85% 가 나왔고, 같은 기간 전 유니버스는 12~28% 였다. 임계값 50 은
전 유니버스 기준 백테스트에서 고른 값이라, 라이브에서는 게이트가 거의 작동하지
않았다.

장중에 300종목 일봉을 다 받으면 09:10 스크리닝이 늘어지므로 16:05 수급 수집 직후
한 번 재서 파일로 남기고, 다음 날 아침에 읽는다. 아침 시점에 쓸 수 있는 가장
최신 정보가 어차피 전일 종가다.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

BREADTH_FILE = Path("data/market_breadth.json")
MAX_AGE_DAYS = 4   # 주말·연휴를 넘겨도 유효하게


def _universe() -> list[str]:
    """백테스트 엔진 기본 유니버스와 같은 규칙 (ETF 등 1로 시작하는 코드 제외)"""
    from src.utils.stock_db import get_db
    db = get_db()
    if not db._loaded:
        db.load()
    return [t for t in db._ticker_to_name
            if t.isdigit() and len(t) == 6 and not t.startswith("1")]


def compute_universe_breadth() -> dict | None:
    """전 유니버스 브레드스를 재서 data/market_breadth.json 에 저장"""
    from src.api.kis_client import KISClient
    kis = KISClient()

    above = total = failed = 0
    last_date = ""
    for t in _universe():
        try:
            cs = kis.get_daily_ohlcv(t, days=40)
        except Exception as e:
            failed += 1
            logger.debug(f"  [{t}] 브레드스용 일봉 실패: {e}")
            continue
        if len(cs) < 20:
            continue
        ma20 = sum(c["close"] for c in cs[-20:]) / 20
        total += 1
        if cs[-1]["close"] > ma20:
            above += 1
        last_date = max(last_date, cs[-1]["date"])
        time.sleep(0.05)

    if total < 10:
        logger.warning(f"브레드스 표본 부족({total}개, 조회 실패 {failed}) - 저장하지 않음")
        return None

    result = {
        "date": last_date,
        "above": above,
        "total": total,
        "pct": round(above / total * 100, 1),
        "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    BREADTH_FILE.parent.mkdir(exist_ok=True)
    with open(BREADTH_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    logger.info(f"전 유니버스 브레드스: {result['pct']}% ({above}/{total}, {last_date} 종가"
                + (f", 조회 실패 {failed}" if failed else "") + ")")
    return result


def load_universe_breadth() -> dict | None:
    """저장된 전 유니버스 브레드스. 없거나 오래됐으면 None"""
    from src.utils.json_state import load_json_state
    b = load_json_state(BREADTH_FILE, {}, "market_breadth")
    if not b.get("date"):
        return None
    try:
        age = (datetime.now() - datetime.strptime(b["date"], "%Y%m%d")).days
    except ValueError:
        return None
    if age > MAX_AGE_DAYS:
        logger.warning(f"브레드스 파일이 {age}일 지남 ({b['date']}) - 사용하지 않음")
        return None
    return b
