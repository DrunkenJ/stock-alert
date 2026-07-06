"""
Yahoo Finance 직접 호출 유틸
- yfinance 라이브러리가 응답 포맷 변경으로 실패하는 문제 우회
- chart API를 curl_cffi로 직접 호출 → JSON 파싱
- macro_agent, premarket 공통 사용

핵심: yfinance(0.2.54) 대신 query1.finance.yahoo.com/v8/finance/chart 직접 호출
"""
import json
from loguru import logger

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"


def _get_session():
    """curl_cffi 세션 생성 (봇 감지 우회)"""
    try:
        from curl_cffi import requests as cffi_requests
        return cffi_requests.Session(impersonate="chrome")
    except ImportError:
        import requests
        logger.warning("curl_cffi 없음 - requests 폴백")
        return requests.Session()


def fetch_quote(symbol: str, range_period: str = "5d") -> dict | None:
    """
    단일 심볼의 최신가 + 전일 대비 변동률 조회

    Returns:
        {"value": float, "change_pct": float, "closes": [list]} or None
    """
    import urllib.parse
    encoded = urllib.parse.quote(symbol)
    url = CHART_URL.format(symbol=encoded, range=range_period)

    session = _get_session()
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            logger.debug(f"{symbol} HTTP {resp.status_code}")
            return None

        data = json.loads(resp.text)
        chart = data.get("chart", {})
        result = chart.get("result")
        if not result:
            logger.debug(f"{symbol} result 없음")
            return None

        r0 = result[0]
        meta = r0.get("meta", {})

        # 종가 배열 추출
        indicators = r0.get("indicators", {})
        quotes = indicators.get("quote", [{}])
        closes_raw = quotes[0].get("close", []) if quotes else []
        # None 제거
        closes = [c for c in closes_raw if c is not None]

        if not closes:
            # 종가 배열 없으면 meta의 현재가만이라도
            cur = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if cur is None:
                return None
            if prev is None:
                prev = cur
            change_pct = (cur - prev) / prev * 100 if prev else 0
            return {
                "value": round(float(cur), 2),
                "change_pct": round(float(change_pct), 2),
                "closes": [cur],
            }

        # 종가 배열 기반 계산
        cur = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else cur
        change_pct = (cur - prev) / prev * 100 if prev else 0

        return {
            "value": round(float(cur), 2),
            "change_pct": round(float(change_pct), 2),
            "closes": [round(float(c), 2) for c in closes],
        }

    except Exception as e:
        logger.debug(f"{symbol} 조회 실패: {e}")
        return None


def fetch_ohlcv(symbol: str, range_period: str = "3mo") -> list:
    """
    OHLCV 캔들 데이터 조회 (regime_classifier 등에서 사용 가능)

    Returns:
        [{"open","high","low","close","volume"}, ...] (오래된 순)
    """
    import urllib.parse
    encoded = urllib.parse.quote(symbol)
    url = CHART_URL.format(symbol=encoded, range=range_period)

    session = _get_session()
    try:
        resp = session.get(url, timeout=10)
        if resp.status_code != 200:
            return []

        data = json.loads(resp.text)
        r0 = data.get("chart", {}).get("result", [{}])[0]
        quotes = r0.get("indicators", {}).get("quote", [{}])[0]

        opens   = quotes.get("open", [])
        highs   = quotes.get("high", [])
        lows    = quotes.get("low", [])
        closes  = quotes.get("close", [])
        volumes = quotes.get("volume", [])

        candles = []
        for i in range(len(closes)):
            if closes[i] is None:
                continue
            candles.append({
                "open":   opens[i]   if i < len(opens)   and opens[i]   else closes[i],
                "high":   highs[i]   if i < len(highs)   and highs[i]   else closes[i],
                "low":    lows[i]    if i < len(lows)    and lows[i]    else closes[i],
                "close":  closes[i],
                "volume": volumes[i] if i < len(volumes) and volumes[i] else 0,
            })
        return candles

    except Exception as e:
        logger.debug(f"{symbol} OHLCV 조회 실패: {e}")
        return []
