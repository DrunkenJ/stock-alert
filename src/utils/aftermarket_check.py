"""애프터마켓 반영 점검 (KRX 애프터마켓 2026-09-14~, 16:00~20:00 접속매매)

KRX 애프터마켓 체결이 KIS 의 KRX(J) 일봉·시세·수급에 어떻게 섞여 들어오는지는
공지로 확인되지 않았다. 기존 시간외 단일가로 실측해 보니(2026-09-10) 장후 체결이
거래량과 투자자 수급에는 더해졌고, 고가·저가·종가는 그대로였다. 다만 단일가는
종가 ±10% 안에서만 체결돼 원래 범위를 벗어나기 어려웠으므로, ±30% 연속 체결인
애프터마켓에서도 같을지는 직접 봐야 한다.

15:40(정규장 확정 직후)과 20:02(애프터마켓 종료 직후)에 같은 종목들을 찍어
비교하고, 결과를 로그와 data/aftermarket_check/YYYYMMDD.json 에 남긴다.
시뮬레이터가 일봉 고가·저가로 체결·손절을 판정하므로, 고가·저가가 저녁 체결을
포함하는지가 이후 판단(시뮬을 정규장 기준으로 할지)의 근거가 된다.
"""
import json
import time
from datetime import datetime
from pathlib import Path

from loguru import logger

CHECK_DIR = Path("data/aftermarket_check")
# 유동성 큰 기준 종목. 여기에 그날 보유·추천 종목을 더한다.
BASE_TICKERS = ["005930", "000660", "035420", "005380", "373220"]


def _tickers() -> list[str]:
    tickers = list(BASE_TICKERS)
    try:
        from src.utils.trade_simulator import get_simulator
        tickers += list(get_simulator().active.keys())
    except Exception as e:
        logger.debug(f"애프터마켓 점검: 보유 종목 조회 실패: {e}")
    try:
        from src.utils.session_state import sync_today_picks
        tickers += [p.get("ticker") for p in sync_today_picks() if p.get("ticker")]
    except Exception as e:
        logger.debug(f"애프터마켓 점검: 추천 종목 조회 실패: {e}")
    return list(dict.fromkeys(t for t in tickers if t))


def _snap_one(kis, ticker: str, today: str) -> dict:
    out = {}
    candle = next((c for c in reversed(kis.get_daily_ohlcv(ticker, days=5))
                   if c["date"] == today), None)
    if candle:
        out["daily"] = {k: candle[k] for k in ("high", "low", "close", "volume")}
    out["price"] = kis.get_stock_price(ticker).get("price")
    flow = next((d for d in kis.get_investor_trend(ticker, days=2).get("detail", [])
                 if d.get("date") == today), None)
    if flow:
        out["flow"] = {"foreign": flow["foreign"], "inst": flow["inst"]}
    return out


def _path(today: str) -> Path:
    return CHECK_DIR / f"{today}.json"


def _save(today: str, data: dict) -> None:
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    with open(_path(today), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def snapshot(label: str) -> dict:
    """오늘 점검 파일에 label 이름으로 스냅샷을 더한다"""
    from src.api.kis_client import KISClient
    from src.utils.json_state import load_json_state
    kis = KISClient()
    today = datetime.now().strftime("%Y%m%d")

    snap = {}
    for t in _tickers():
        try:
            snap[t] = _snap_one(kis, t, today)
        except Exception as e:
            logger.debug(f"  [{t}] 애프터마켓 점검 스냅샷 실패: {e}")
        time.sleep(0.15)

    data = load_json_state(_path(today), {}, "aftermarket_check")
    data[label] = {"at": datetime.now().strftime("%H:%M:%S"), "data": snap}
    _save(today, data)
    logger.info(f"애프터마켓 점검 스냅샷 [{label}] {len(snap)}종목")
    return data


def compare(before: str = "close", after: str = "evening") -> dict | None:
    """after 스냅샷을 찍고 before 와 비교해 무엇이 바뀌었는지 기록한다"""
    today = datetime.now().strftime("%Y%m%d")
    data = snapshot(after)
    a = data.get(before, {}).get("data", {})
    b = data.get(after, {}).get("data", {})
    if not a:
        logger.warning(f"애프터마켓 점검: '{before}' 스냅샷이 없어 비교 생략")
        return None

    counts = {"high": 0, "low": 0, "close": 0, "price": 0, "flow": 0}
    vol_change, details, n = [], [], 0
    for t, sa in a.items():
        sb = b.get(t, {})
        da, db = sa.get("daily"), sb.get("daily")
        if not (da and db):
            continue
        n += 1
        changed = []
        for k in ("high", "low", "close"):
            if da[k] != db[k]:
                counts[k] += 1
                changed.append(f"{k} {da[k]:,}→{db[k]:,}")
        if sa.get("price") is not None and sa.get("price") != sb.get("price"):
            counts["price"] += 1
            changed.append(f"현재가 {sa['price']:,}→{sb.get('price') or 0:,}")
        # 15:40 에 당일 수급이 아직 비어 있을 수 있다. 비어 있다가 채워진 것은
        # '변경'이 아니므로 양쪽 모두 값이 있을 때만 비교한다.
        if sa.get("flow") and sb.get("flow") and sa["flow"] != sb["flow"]:
            counts["flow"] += 1
        if da["volume"]:
            vol_change.append((db["volume"] - da["volume"]) / da["volume"] * 100)
        if changed:
            details.append(f"{t}: " + ", ".join(changed))

    avg_vol = sum(vol_change) / len(vol_change) if vol_change else 0.0
    summary = {"n": n, **counts, "avg_volume_change_pct": round(avg_vol, 1), "details": details}
    data["summary"] = summary
    _save(today, data)

    logger.info(
        f"애프터마켓 반영 점검 ({n}종목, {data[before]['at']}→{data[after]['at']}): "
        f"고가 변경 {counts['high']} / 저가 {counts['low']} / 종가 {counts['close']} / "
        f"현재가 {counts['price']} / 수급 {counts['flow']} / 거래량 평균 {avg_vol:+.1f}%"
    )
    for d in details[:10]:
        logger.info(f"   {d}")
    return summary
