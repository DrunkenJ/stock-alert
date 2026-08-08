"""
일별 수급 데이터 수집기
- 매일 16:05 장마감 후 자동 실행
- 거래량 상위 종목의 외국인/기관 수급 저장
- data/supply_history/YYYYMMDD.json
- 백테스트 시 실제 당일 수급 데이터로 활용
"""
import json
import time
from datetime import datetime
from pathlib import Path
from loguru import logger


SUPPLY_DIR = Path("data/supply_history")


def build_universe(top_n: int = 300) -> list[dict]:
    """수급 수집 대상 종목 구성 (시총 상위 + 거래량 상위)

    기존에는 거래량 상위만 모았는데, 스크리너 유니버스가 시총 기반으로 바뀐 뒤로
    저장된 수급이 실제 후보 풀과 거의 겹치지 않게 됐다.
    랭킹 API는 호출당 30건이 한계라 시장별로 나눠 호출한다.
    """
    from src.api.kis_client import KISClient
    kis = KISClient()

    seen, universe = set(), []

    def _add(stocks):
        for st in stocks or []:
            t = st.get("ticker", "")
            if not t or not t.isdigit() or len(t) != 6 or t.startswith("1"):
                continue
            if t in seen:
                continue
            seen.add(t)
            universe.append(st)

    for market in (kis.MCAP_KOSPI, kis.MCAP_KOSDAQ):
        try:
            _add(kis.get_market_cap_ranking(market=market, top_n=top_n))
        except Exception as e:
            logger.debug(f"  시총 랭킹 실패({market}): {e}")
        time.sleep(0.2)

    try:
        _add(kis.get_volume_ranking(market="J", top_n=top_n))
    except Exception as e:
        logger.debug(f"  거래량 랭킹 실패: {e}")

    # 종목 DB 전체로 보강 (랭킹 API 30건 제한 보완)
    if len(universe) < top_n:
        try:
            from src.utils.stock_db import get_db
            db = get_db()
            if not db._loaded:
                db.load()
            for t, name in db._ticker_to_name.items():
                if len(universe) >= top_n:
                    break
                if t.isdigit() and len(t) == 6 and not t.startswith("1") and t not in seen:
                    seen.add(t)
                    universe.append({"ticker": t, "name": name})
        except Exception as e:
            logger.debug(f"  종목 DB 보강 실패: {e}")

    return universe[:top_n]


def collect_daily_supply(top_n: int = 300, backfill: bool = True) -> dict:
    """수급 데이터 수집 및 저장

    KIS 투자자 API(inquire-investor)는 호출 한 번에 30일치를 돌려준다.
    예전에는 days=1 로 잘라 당일 1행만 저장했는데, 이는 매 호출마다 29일치를
    버리는 것이었다. 그 결과 76일을 모으고도 종목별 연속 시계열이 없어
    (전 기간 등장 202종목 중 70일 이상 등장은 5종목) 백테스트에 쓸 수 없었다.

    이제 받은 30행을 각 날짜 파일로 분배 저장한다. 호출 수는 그대로인데
    한 번 실행으로 최근 30일이 채워진다.

    Args:
        backfill: True면 30일 전체 저장, False면 당일만
    """
    today = datetime.now().strftime("%Y%m%d")
    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)

    universe = build_universe(top_n)
    if not universe:
        logger.error("수급 수집 대상 없음")
        return {}

    logger.info(f"수급 데이터 수집 시작: {len(universe)}종목 "
                f"({'최근 30일 백필' if backfill else '당일만'})")

    from src.api.kis_client import KISClient
    kis = KISClient()

    # 날짜별 누적 버퍼 { "20260807": { ticker: {...} } }
    by_date: dict[str, dict] = {}
    success = fail = 0

    for i, stock in enumerate(universe):
        ticker = stock["ticker"]
        try:
            investor = kis.get_investor_trend(ticker, days=30)
            detail = investor.get("detail", [])
            if not detail:
                fail += 1
                continue

            rows = detail if backfill else [d for d in detail if d.get("date") == today]

            for d in rows:
                date = d.get("date")
                if not date:
                    continue
                rec = {
                    "name":    stock.get("name", ""),
                    "foreign": d.get("foreign", 0),
                    "inst":    d.get("inst", 0),
                    "indiv":   d.get("indiv", 0),
                }
                # 당일 행에만 시세/누적 지표를 함께 저장
                if date == today:
                    rec.update({
                        "price":       stock.get("price", 0),
                        "volume":      stock.get("volume", 0),
                        "change_rate": stock.get("change_rate", 0),
                        "foreign_5d":  investor.get("foreign_net", 0),
                        "inst_5d":     investor.get("inst_net", 0),
                        "foreign_consecutive": investor.get("foreign_consecutive", 0),
                        "inst_consecutive":    investor.get("inst_consecutive", 0),
                    })
                by_date.setdefault(date, {})[ticker] = rec

            success += 1
            time.sleep(0.2)

        except Exception as e:
            logger.debug(f"  [{ticker}] 수급 조회 실패: {e}")
            fail += 1

        if (i + 1) % 50 == 0:
            logger.info(f"  진행: {i+1}/{len(universe)} (성공:{success} 실패:{fail})")

    # 날짜별 파일에 병합 저장 (기존 종목을 지우지 않는다)
    written = 0
    for date, data in sorted(by_date.items()):
        existing = _load_supply(date)
        merged = existing.get("data", {})
        merged.update(data)
        with open(SUPPLY_DIR / f"{date}.json", "w", encoding="utf-8") as f:
            json.dump({
                "date": date,
                "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "count": len(merged),
                "data": merged,
            }, f, ensure_ascii=False, indent=2)
        written += 1

    logger.info(f"수급 저장 완료: {written}개 날짜 파일 갱신 "
                f"(조회 성공 {success} / 실패 {fail})")
    return _load_supply(today)


def _load_supply(date: str) -> dict:
    """저장된 수급 데이터 로드"""
    path = SUPPLY_DIR / f"{date}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_supply_for_date(date: str) -> dict:
    """특정 날짜 수급 데이터 반환 (백테스트용)"""
    data = _load_supply(date)
    return data.get("data", {})


def get_available_dates() -> list[str]:
    """수급 데이터가 있는 날짜 목록"""
    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)
    dates = sorted([f.stem for f in SUPPLY_DIR.glob("*.json")])
    return dates


def get_history_summary() -> dict:
    """수집 현황 요약"""
    dates = get_available_dates()
    if not dates:
        return {"count": 0, "first": None, "last": None}
    return {
        "count": len(dates),
        "first": dates[0],
        "last": dates[-1],
        "dates": dates,
    }
