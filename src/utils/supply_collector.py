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


def collect_daily_supply(top_n: int = 100) -> dict:
    """당일 수급 데이터 수집 및 저장"""
    from src.api.kis_client import KISClient

    today = datetime.now().strftime("%Y%m%d")
    save_path = SUPPLY_DIR / f"{today}.json"

    # 이미 오늘 데이터 있으면 스킵
    if save_path.exists():
        logger.info(f"수급 데이터 이미 존재: {today}")
        return _load_supply(today)

    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)
    kis = KISClient()

    logger.info(f"수급 데이터 수집 시작: {today} (상위 {top_n}종목)")

    # 거래량 상위 종목 수집
    try:
        vol_stocks = kis.get_volume_ranking(market="J", top_n=top_n)
    except Exception as e:
        logger.error(f"거래량 조회 실패: {e}")
        return {}

    supply_data = {}
    success = 0
    fail = 0

    for i, stock in enumerate(vol_stocks):
        ticker = stock.get("ticker", "")
        if not ticker or not ticker.isdigit() or len(ticker) != 6:
            continue
        if ticker.startswith("1"):  # ETF 제외
            continue

        try:
            investor = kis.get_investor_trend(ticker, days=1)
            detail = investor.get("detail", [])

            # 오늘 날짜 수급만 추출
            today_data = next(
                (d for d in detail if d["date"] == today),
                detail[0] if detail else None
            )

            if today_data:
                supply_data[ticker] = {
                    "name": stock.get("name", ""),
                    "price": stock.get("price", 0),
                    "volume": stock.get("volume", 0),
                    "change_rate": stock.get("change_rate", 0),
                    "foreign": today_data.get("foreign", 0),
                    "inst": today_data.get("inst", 0),
                    "indiv": today_data.get("indiv", 0),
                    # 5일 누적 수급
                    "foreign_5d": investor.get("foreign_net", 0),
                    "inst_5d": investor.get("inst_net", 0),
                    "foreign_consecutive": investor.get("foreign_consecutive", 0),
                    "inst_consecutive": investor.get("inst_consecutive", 0),
                }
                success += 1

            time.sleep(0.2)

        except Exception as e:
            logger.debug(f"  [{ticker}] 수급 조회 실패: {e}")
            fail += 1

        if (i + 1) % 20 == 0:
            logger.info(f"  진행: {i+1}/{len(vol_stocks)} (성공:{success} 실패:{fail})")

    # 저장
    result = {
        "date": today,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "count": len(supply_data),
        "data": supply_data,
    }

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info(f"수급 데이터 저장 완료: {today} ({len(supply_data)}종목) → {save_path}")
    return result


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
