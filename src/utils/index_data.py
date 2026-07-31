"""
지수 대용 데이터 (상대강도 RS 계산용)

KIS 실서버는 지수 일봉 API를 별도 TR로 제공하지만, 종목 일봉 API로
지수 추종 ETF를 조회하면 동일한 목적을 훨씬 안정적으로 달성할 수 있어
대표 ETF를 지수 대용(proxy)으로 사용한다.

  KOSPI  → 069500 (KODEX 200)
  KOSDAQ → 229200 (KODEX 코스닥150)

토큰 발급이 하루 1회로 제한되므로 결과는 data/index_cache.json 에
당일 캐시한다. 조회 실패 시 None 을 반환하며, 호출부는 RS 계산을
건너뛰고 정상 동작해야 한다.
"""
import json
import os
from datetime import datetime
from pathlib import Path

from loguru import logger

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
CACHE_FILE = DATA_DIR / "index_cache.json"

INDEX_PROXY = {
    "KOSPI": "069500",
    "KOSDAQ": "229200",
}


class IndexData:
    """지수 대용 ETF의 최근 수익률 제공 (일 단위 캐시)"""

    def __init__(self):
        self._cache: dict = {}
        self._loaded = False

    # ── 캐시 입출력 ──────────────────────────────
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            if CACHE_FILE.exists():
                self._cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.debug(f"지수 캐시 로드 실패: {e}")
            self._cache = {}

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug(f"지수 캐시 저장 실패: {e}")

    # ── 조회 ────────────────────────────────────
    def get_ret(self, market: str = "KOSDAQ", days: int = 20) -> float | None:
        """지수(대용 ETF)의 최근 `days` 영업일 수익률 % 반환"""
        self._load()
        ticker = INDEX_PROXY.get((market or "KOSDAQ").upper())
        if not ticker:
            return None

        today = datetime.now().strftime("%Y-%m-%d")
        entry = self._cache.get(ticker) or {}

        if entry.get("date") != today:
            closes = self._fetch(ticker)
            if closes:
                entry = {"date": today, "closes": closes}
                self._cache[ticker] = entry
                self._save()
            else:
                # 신규 조회 실패 → 있으면 직전 캐시라도 사용
                if not entry.get("closes"):
                    return None

        closes = entry.get("closes") or []
        if len(closes) < days + 1:
            return None
        return (closes[-1] - closes[-1 - days]) / closes[-1 - days] * 100

    def _fetch(self, ticker: str) -> list[float] | None:
        try:
            from src.api.kis_client import KISClient

            candles = KISClient().get_daily_ohlcv(ticker, days=60)
            if not candles or len(candles) < 21:
                return None
            # get_daily_ohlcv 는 과거→최근 순으로 반환
            return [float(c["close"]) for c in candles]
        except Exception as e:
            logger.debug(f"지수 대용 ETF({ticker}) 조회 실패: {e}")
            return None


_instance: IndexData | None = None


def get_index_data() -> IndexData:
    global _instance
    if _instance is None:
        _instance = IndexData()
    return _instance


def get_market_ret20(market: str = "KOSDAQ") -> float | None:
    """지수 20일 수익률 % (실패 시 None)"""
    if os.getenv("USE_RELATIVE_STRENGTH", "true").lower() != "true":
        return None
    return get_index_data().get_ret(market, days=20)
