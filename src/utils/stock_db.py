"""
코스피/코스닥 전종목 DB
- KIS 거래량순위 API를 가격대/시장별로 분할 조회해서 전종목 수집
- data/stock_db.json에 저장 (볼륨 마운트로 영속)
- 매일 08:00 자동 갱신 (신규 상장 반영)
"""
import json
import time
from pathlib import Path
from loguru import logger

DB_PATH = Path("data/stock_db.json")

SEED_DB = {
    "삼성전자": "005930", "SK하이닉스": "000660", "LG에너지솔루션": "373220",
    "삼성바이오로직스": "207940", "현대차": "005380", "기아": "000270",
    "POSCO홀딩스": "005490", "삼성SDI": "006400", "LG화학": "051910",
    "현대모비스": "012330", "삼성물산": "028260", "KB금융": "105560",
    "신한지주": "055550", "셀트리온": "068270", "LG전자": "066570",
    "SK이노베이션": "096770", "하나금융지주": "086790", "카카오": "035720",
    "네이버": "035420", "한국전력": "015760", "SK텔레콤": "017670",
    "KT": "030200", "LG유플러스": "032640", "에코프로": "086520",
    "에코프로비엠": "247540", "포스코퓨처엠": "003670", "고려아연": "010130",
    "HLB": "028300", "현대오토에버": "307950", "한화에어로스페이스": "012450",
    "HD현대": "267250", "두산에너빌리티": "034020", "삼성생명": "032830",
    "삼성화재": "000810", "한미약품": "128940", "셀트리온헬스케어": "091990",
    "카카오뱅크": "323410", "카카오페이": "377300", "크래프톤": "259960",
    "엔씨소프트": "036570", "하이브": "352820", "엘앤에프": "066970",
    "알테오젠": "196170", "한국조선해양": "009540", "삼성중공업": "010140",
    "현대중공업": "329180", "한화시스템": "272210", "LIG넥스원": "079550",
    "파두": "440110", "에코프로머티리얼즈": "450080", "LS에코에너지": "229640",
}


class StockDatabase:
    """전종목 DB 관리 클래스"""

    def __init__(self):
        self._db: dict[str, str] = {}
        self._ticker_to_name: dict[str, str] = {}
        self._loaded = False

    def load(self):
        """DB 로드: 파일 있으면 파일에서, 없으면 API 빌드"""
        if DB_PATH.exists():
            self._load_from_file()
        else:
            logger.info("종목 DB 파일 없음 → API로 신규 빌드")
            self.rebuild()

        self._merge_seed()
        self._loaded = True
        logger.info(f"종목 DB 로드 완료: {len(self._db):,}개 종목")

    def rebuild(self):
        """KIS API로 전종목 재빌드"""
        logger.info("전종목 DB 빌드 시작 (코스피 + 코스닥)")
        new_db = {}

        try:
            from src.api.kis_client import KISClient
            kis = KISClient()

            # J = 코스피+코스닥 통합 (KIS 실서버 기준)
            all_stocks = self._collect_by_price_range(kis, market_div="J", market_iscd="0000")
            new_db.update(all_stocks)
            kospi_cnt = sum(1 for t in all_stocks.values() if not t.startswith("0"))
            kosdaq_cnt = sum(1 for t in all_stocks.values() if t.startswith("0"))
            logger.info(f"  코스피: {kospi_cnt:,}개 / 코스닥: {kosdaq_cnt:,}개 (총 {len(all_stocks):,}개)")

        except Exception as e:
            logger.warning(f"API 빌드 실패: {e} → 시드 DB만 사용")
            new_db = dict(SEED_DB)

        # 시드 보완
        for name, ticker in SEED_DB.items():
            new_db.setdefault(name, ticker)

        self._db = new_db
        self._ticker_to_name = {v: k for k, v in new_db.items()}
        self._save_to_file()
        logger.info(f"전종목 DB 빌드 완료: {len(new_db):,}개")

    def _collect_by_price_range(self, kis, market_div: str, market_iscd: str) -> dict:
        """
        거래량 순위 API를 가격대별로 나눠서 전종목 수집
        - fid_cond_mrkt_div_code: J=코스피, Q=코스닥
        - fid_input_iscd: 0000 (전체)
        - 1회 최대 100건 → 가격대 11구간 분할
        """
        result = {}

        price_ranges = [
            ("0",      "500",    "500원 미만"),
            ("500",    "1000",   "500~1000원"),
            ("1000",   "3000",   "1000~3000원"),
            ("3000",   "5000",   "3000~5000원"),
            ("5000",   "10000",  "5000~1만원"),
            ("10000",  "30000",  "1~3만원"),
            ("30000",  "50000",  "3~5만원"),
            ("50000",  "100000", "5~10만원"),
            ("100000", "300000", "10~30만원"),
            ("300000", "500000", "30~50만원"),
            ("500000", "",       "50만원 이상"),
        ]

        for price_from, price_to, label in price_ranges:
            try:
                data = kis._get(
                    "/uapi/domestic-stock/v1/quotations/volume-rank",
                    "FHPST01710000",
                    {
                        "fid_cond_mrkt_div_code": market_div,  # J or Q
                        "fid_cond_scr_div_code": "20171",
                        "fid_input_iscd": "0000",              # 0000=전체
                        "fid_div_cls_code": "0",
                        "fid_blng_cls_code": "0",
                        "fid_trgt_cls_code": "111111111",
                        "fid_trgt_exls_cls_code": "000000",
                        "fid_input_price_1": price_from,
                        "fid_input_price_2": price_to,
                        "fid_vol_cnt": "0",
                        "fid_input_date_1": "",
                    },
                )
                count = 0
                for row in data.get("output", []):
                    ticker = row.get("mksc_shrn_iscd", "").strip()
                    name = row.get("hts_kor_isnm", "").strip()
                    if ticker and name and ticker.isdigit() and len(ticker) == 6:
                        result[name] = ticker
                        count += 1

                logger.debug(f"    {label}: {count}개")
                time.sleep(0.3)

            except Exception as e:
                logger.debug(f"    {label} 조회 실패: {e}")
                continue

        return result

    def search(self, query: str) -> dict | None:
        """
        종목명 검색
        1. 완전 일치
        2. 검색어로 시작하는 종목 (최단 이름 우선)
        3. 검색어가 포함된 종목 (최단 이름 우선)
        """
        if not self._loaded:
            self.load()

        query = query.strip()

        # 1. 완전 일치
        if query in self._db:
            return {"ticker": self._db[query], "name": query}

        # 2. 시작 일치
        starts = [(n, t) for n, t in self._db.items() if n.startswith(query)]
        if starts:
            starts.sort(key=lambda x: len(x[0]))
            name, ticker = starts[0]
            return {"ticker": ticker, "name": name}

        # 3. 포함 일치
        contains = [(n, t) for n, t in self._db.items() if query in n]
        if contains:
            contains.sort(key=lambda x: len(x[0]))
            name, ticker = contains[0]
            return {"ticker": ticker, "name": name}

        return None

    def ticker_to_name(self, ticker: str) -> str:
        if not self._loaded:
            self.load()
        return self._ticker_to_name.get(ticker, "")

    def total_count(self) -> int:
        return len(self._db)

    def _merge_seed(self):
        for name, ticker in SEED_DB.items():
            if name not in self._db:
                self._db[name] = ticker
                self._ticker_to_name[ticker] = name

    def _load_from_file(self):
        try:
            with open(DB_PATH, encoding="utf-8") as f:
                self._db = json.load(f)
            self._ticker_to_name = {v: k for k, v in self._db.items()}
            logger.info(f"종목 DB 파일 로드: {len(self._db):,}개")
        except Exception as e:
            logger.warning(f"DB 파일 로드 실패: {e} → 재빌드")
            self.rebuild()

    def _save_to_file(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(self._db, f, ensure_ascii=False, indent=2)
        logger.info(f"종목 DB 저장 완료: {DB_PATH} ({len(self._db):,}개)")


# ── 싱글톤 ──────────────────────────────────
_stock_db = StockDatabase()


def get_db() -> StockDatabase:
    return _stock_db


def search_by_name(query: str) -> dict | None:
    return _stock_db.search(query)


def rebuild_db():
    _stock_db.rebuild()
