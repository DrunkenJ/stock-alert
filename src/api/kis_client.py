"""
한국투자증권 OpenAPI 클라이언트
- 토큰 자동 갱신
- 싱글톤 패턴으로 토큰 재사용 (한투 API는 하루 1회 발급 권장)
- 시세 조회, 수급 데이터, 종목 스크리닝
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


class KISClient:
    """한국투자증권 REST API 클라이언트 (싱글톤)"""

    # 실서버 / 모의투자 URL
    REAL_URL = "https://openapi.koreainvestment.com:9443"
    MOCK_URL = "https://openapivts.koreainvestment.com:29443"

    # ── 싱글톤: 프로세스 전체에서 토큰 1개만 유지 ──
    _instance: Optional["KISClient"] = None
    _access_token: Optional[str] = None
    _token_expires: Optional[datetime] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return  # 이미 초기화된 경우 스킵
        self.app_key = os.getenv("KIS_APP_KEY")
        self.app_secret = os.getenv("KIS_APP_SECRET")
        self.account_no = os.getenv("KIS_ACCOUNT_NO")
        self.account_prod = os.getenv("KIS_ACCOUNT_PROD_CODE", "01")
        self.is_real = os.getenv("KIS_IS_REAL", "false").lower() == "true"
        self.base_url = self.REAL_URL if self.is_real else self.MOCK_URL
        self._initialized = True
        logger.info(f"KIS API 초기화 - {'실서버' if self.is_real else '모의투자'} 모드")

    # ─────────────────────────────────────────
    # 인증
    # ─────────────────────────────────────────
    def get_access_token(self) -> str:
        """Access Token 발급 (만료 전 자동 갱신, 싱글톤으로 재사용)"""
        if KISClient._access_token and KISClient._token_expires:
            if datetime.now() < KISClient._token_expires - timedelta(minutes=5):
                return KISClient._access_token

        logger.info("KIS Access Token 신규 발급 요청")
        url = f"{self.base_url}/oauth2/tokenP"
        payload = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        KISClient._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        KISClient._token_expires = datetime.now() + timedelta(seconds=expires_in)
        logger.info(f"KIS Access Token 발급 완료 (만료: {KISClient._token_expires.strftime('%H:%M')})")
        return KISClient._access_token

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.get_access_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if extra:
            headers.update(extra)
        return headers

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        """GET 요청 공통 처리"""
        url = f"{self.base_url}{path}"
        resp = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise ValueError(f"KIS API 오류: {data.get('msg1', '알 수 없는 오류')}")
        return data

    # ─────────────────────────────────────────
    # 시장 데이터
    # ─────────────────────────────────────────
    def get_stock_price(self, ticker: str) -> dict:
        """주식 현재가 조회"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        output = data["output"]
        # hts_kor_isnm이 없으면 stock_db에서 종목명 보완
        # bstp_kor_isnm은 업종명이므로 사용 안 함
        name = output.get("hts_kor_isnm", "").strip()
        if not name:
            try:
                from src.utils.stock_db import get_db
                name = get_db().ticker_to_name(ticker) or ticker
            except Exception:
                name = ticker
        # 대표시장명(rprs_mrkt_kor_name)으로 코스피/코스닥 구분
        # 티커 앞자리로 추정하던 방식은 실제 배정 규칙과 무관해
        # 삼성전자(005930) 같은 코스피 종목도 코스닥으로 잘못 분류되고 있었음
        market_name = output.get("rprs_mrkt_kor_name", "")
        if "코스닥" in market_name:
            market = "KOSDAQ"
        elif "코스피" in market_name:
            market = "KOSPI"
        else:
            market = ""
        return {
            "ticker": ticker,
            "name": name,
            "price": int(output.get("stck_prpr", 0)),
            "open_price": int(output.get("stck_oprc", 0)),  # 당일 시가
            "change_rate": float(output.get("prdy_ctrt", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "market_cap": int(output.get("hts_avls", 0)) * 100_000_000,
            "market": market,
            "per": float(output.get("per", 0)),
            "pbr": float(output.get("pbr", 0)),
            "high_52w": int(output.get("w52_hgpr", 0)),
            "low_52w": int(output.get("w52_lwpr", 0)),
        }

    def get_daily_ohlcv(self, ticker: str, days: int = 120) -> list[dict]:
        """일봉 OHLCV 조회"""
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=days * 1.5)).strftime("%Y%m%d")

        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": start_date,
                "FID_INPUT_DATE_2": end_date,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        candles = []
        for row in data.get("output2", []):
            try:
                candles.append({
                    "date": row["stck_bsop_date"],
                    "open": int(row["stck_oprc"]),
                    "high": int(row["stck_hgpr"]),
                    "low": int(row["stck_lwpr"]),
                    "close": int(row["stck_clpr"]),
                    "volume": int(row["acml_vol"]),
                })
            except (KeyError, ValueError):
                continue
        return sorted(candles, key=lambda x: x["date"])

    def get_investor_trend(self, ticker: str, days: int = 20) -> dict:
        """투자자별 매매동향 (기관/외국인/개인)"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            "FHKST01010900",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker},
        )
        rows = data.get("output", [])

        result = {
            "foreign_net": 0,
            "inst_net": 0,
            "indiv_net": 0,
            "foreign_consecutive": 0,
            "inst_consecutive": 0,
            "detail": [],
        }

        foreign_consec, inst_consec = 0, 0
        for i, row in enumerate(rows[:days]):
            try:
                foreign = int(row.get("frgn_ntby_qty", 0))
                inst = int(row.get("orgn_ntby_qty", 0))
                indiv = int(row.get("indv_ntby_qty", 0))
                result["foreign_net"] += foreign
                result["inst_net"] += inst
                result["indiv_net"] += indiv

                if foreign > 0:
                    foreign_consec += 1
                else:
                    foreign_consec = 0
                if inst > 0:
                    inst_consec += 1
                else:
                    inst_consec = 0

                result["detail"].append({
                    "date": row.get("stck_bsop_date", ""),
                    "foreign": foreign,
                    "inst": inst,
                    "indiv": indiv,
                })
            except (ValueError, TypeError):
                continue

        result["foreign_consecutive"] = foreign_consec
        result["inst_consecutive"] = inst_consec
        return result

    # 시가총액 상위 랭킹의 시장 구분 코드
    MCAP_KOSPI = "0001"
    MCAP_KOSDAQ = "1001"

    def get_market_cap_ranking(self, market: str = MCAP_KOSPI, top_n: int = 100) -> list[dict]:
        """시가총액 상위 종목 조회

        market: "0001"=코스피, "1001"=코스닥 (KIS 랭킹 API의 fid_input_iscd)
        한 번 호출당 30건까지만 반환된다.
        """
        data = self._get(
            "/uapi/domestic-stock/v1/ranking/market-cap",
            "FHPST01740000",
            {
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20174",
                "fid_input_iscd": market,
                "fid_div_cls_code": "0",
                "fid_blng_cls_code": "0",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_input_date_1": "",
            },
        )
        stocks = []
        for row in data.get("output", [])[:top_n]:
            try:
                stocks.append({
                    "ticker": row["mksc_shrn_iscd"],
                    "name": row["hts_kor_isnm"],
                    "price": int(row.get("stck_prpr", 0)),
                    "change_rate": float(row.get("prdy_ctrt", 0)),
                    "volume": int(row.get("acml_vol", 0)),
                    # stck_avls 는 억원 단위 → get_stock_price 와 동일하게 원 단위로 맞춘다
                    "market_cap": int(row.get("stck_avls", 0)) * 100_000_000,
                    "market": "KOSDAQ" if market == self.MCAP_KOSDAQ else "KOSPI",
                })
            except (KeyError, ValueError):
                continue
        return stocks

    def get_volume_ranking(self, market: str = "J", top_n: int = 50) -> list[dict]:
        """거래량 상위 종목 (fid_vol_cnt=0: 필터 없음)"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "fid_cond_mrkt_div_code": market,
                "fid_cond_scr_div_code": "20171",
                "fid_input_iscd": "0000",
                "fid_div_cls_code": "0",
                "fid_blng_cls_code": "0",
                "fid_trgt_cls_code": "111111111",
                "fid_trgt_exls_cls_code": "000000",
                "fid_input_price_1": "1000",
                "fid_input_price_2": "",
                "fid_vol_cnt": "0",
                "fid_input_date_1": "",
            },
        )
        stocks = []
        for row in data.get("output", [])[:top_n]:
            try:
                stocks.append({
                    "ticker": row["mksc_shrn_iscd"],
                    "name": row["hts_kor_isnm"],
                    "price": int(row.get("stck_prpr", 0)),
                    "change_rate": float(row.get("prdy_ctrt", 0)),
                    "volume": int(row.get("acml_vol", 0)),
                    "vol_increase_rate": float(row.get("vol_inrt", 0)),
                })
            except (KeyError, ValueError):
                continue
        return stocks

    def get_foreign_buying_ranking(self, top_n: int = 30) -> list[dict]:
        """외국인 순매수 상위 종목
        실서버에서 외국인 랭킹 API 미제공 → 거래량 상위 종목의 수급 개별 조회로 대체
        """
        return self._get_supply_demand_ranking(top_n=top_n, investor="foreign")

    def get_institution_buying_ranking(self, top_n: int = 30) -> list[dict]:
        """기관 순매수 상위 종목
        실서버에서 기관 랭킹 API 미제공 → 거래량 상위 종목의 수급 개별 조회로 대체
        """
        return self._get_supply_demand_ranking(top_n=top_n, investor="inst")

    def _get_supply_demand_ranking(self, top_n: int = 30, investor: str = "foreign") -> list[dict]:
        """
        거래량 상위 종목 수집 후 종목별 수급 조회
        investor: "foreign" or "inst"
        """
        import time as _time

        # 1단계: 거래량 상위 종목 수집 (코스피+코스닥)
        candidates = []
        for market in ["J", "Q"]:
            try:
                data = self._get(
                    "/uapi/domestic-stock/v1/quotations/volume-rank",
                    "FHPST01710000",
                    {
                        "fid_cond_mrkt_div_code": market,
                        "fid_cond_scr_div_code": "20171",
                        "fid_input_iscd": "0000",
                        "fid_div_cls_code": "0",
                        "fid_blng_cls_code": "0",
                        "fid_trgt_cls_code": "111111111",
                        "fid_trgt_exls_cls_code": "000000",
                        "fid_input_price_1": "1000",
                        "fid_input_price_2": "",
                        "fid_vol_cnt": "0",
                        "fid_input_date_1": "",
                    },
                )
                for row in data.get("output", []):
                    ticker = row.get("mksc_shrn_iscd", "").strip()
                    name = row.get("hts_kor_isnm", "").strip()
                    if ticker and name:
                        candidates.append({
                            "ticker": ticker,
                            "name": name,
                            "price": int(row.get("stck_prpr", 0)),
                            "change_rate": float(row.get("prdy_ctrt", 0)),
                            "volume": int(row.get("acml_vol", 0)),
                        })
            except Exception as e:
                logger.debug(f"거래량 조회 실패 ({market}): {e}")

        # 중복 제거
        seen = set()
        unique = []
        for s in candidates:
            if s["ticker"] not in seen:
                seen.add(s["ticker"])
                unique.append(s)

        # 2단계: 각 종목 수급 조회 후 순매수 상위 필터
        results = []
        for stock in unique[:top_n * 3]:  # 여유있게 조회
            try:
                inv = self.get_investor_trend(stock["ticker"], days=5)
                net = inv.get("foreign_net", 0) if investor == "foreign" else inv.get("inst_net", 0)
                if net > 0:
                    key = "foreign_net_buy" if investor == "foreign" else "inst_net_buy"
                    results.append({**stock, key: net})
                _time.sleep(0.2)
            except Exception:
                continue

        # 순매수 많은 순으로 정렬
        key = "foreign_net_buy" if investor == "foreign" else "inst_net_buy"
        results.sort(key=lambda x: x.get(key, 0), reverse=True)
        return results[:top_n]

    def is_market_open(self) -> bool:
        """현재 주식시장 개장 여부"""
        import pytz
        kst = pytz.timezone("Asia/Seoul")
        now = datetime.now(kst)
        if now.weekday() >= 5:
            return False
        market_open = now.replace(hour=9, minute=0, second=0)
        market_close = now.replace(hour=15, minute=30, second=0)
        return market_open <= now <= market_close

    def search_stock_name(self, name: str) -> dict | None:
        """종목명으로 티커 검색
        1순위: 로컬 DB (500여 종목, 네트워크 불필요, 빠름)
        2순위: KIS API 검색
        """
        from src.utils.stock_db import search_by_name

        # 1순위: 로컬 DB
        result = search_by_name(name)
        if result:
            logger.debug(f"로컬DB 검색: {name} → {result['name']}({result['ticker']})")
            return result

        # 2순위: KIS API 검색
        result = self._search_kis_by_name(name)
        if result:
            return result

        return None

    def _search_kis_by_name(self, name: str) -> dict | None:
        """KIS API로 종목명 검색 (CTPF1002R)"""
        try:
            url = f"{self.base_url}/uapi/domestic-stock/v1/quotations/search-stock-info"
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "authorization": f"Bearer {self.get_access_token()}",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
                "tr_id": "CTPF1002R",
                "custtype": "P",
            }
            params = {
                "PRDT_TYPE_CD": "300",
                "PDNO": name,
            }
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code != 200:
                logger.debug(f"KIS 종목검색 HTTP오류: {resp.status_code}")
                return None

            data = resp.json()
            # rt_cd 체크 없이 output만 확인 (검색은 오류코드가 달라서)
            output = data.get("output")
            if not output:
                logger.debug(f"KIS 종목검색 결과없음: {name}")
                return None

            # output이 리스트인 경우
            if isinstance(output, list):
                for row in output:
                    ticker = row.get("PDNO", "").strip()
                    stock_name = row.get("PRDT_ABRV_NAME", "").strip()
                    # 6자리 숫자코드 + 이름에 검색어 포함 확인
                    if ticker.isdigit() and len(ticker) == 6 and stock_name:
                        logger.debug(f"KIS 종목검색: {name} → {stock_name}({ticker})")
                        return {"ticker": ticker, "name": stock_name}

            # output이 단일 dict인 경우
            elif isinstance(output, dict):
                ticker = output.get("PDNO", "").strip()
                stock_name = output.get("PRDT_ABRV_NAME", "").strip()
                if ticker.isdigit() and len(ticker) == 6:
                    return {"ticker": ticker, "name": stock_name}

        except Exception as e:
            logger.debug(f"KIS 종목검색 실패 ({name}): {e}")
        return None

    def _fallback_name_search(self, name: str) -> dict | None:
        """로컬 매핑 폴백 (네이버/KIS API 모두 실패 시)"""
        COMMON = {
            "삼성전자": "005930", "SK하이닉스": "000660", "LG에너지솔루션": "373220",
            "삼성바이오로직스": "207940", "현대차": "005380", "기아": "000270",
            "POSCO홀딩스": "005490", "삼성SDI": "006400", "LG화학": "051910",
            "네이버": "035420", "카카오": "035720", "셀트리온": "068270",
            "KB금융": "105560", "신한지주": "055550", "하나금융지주": "086790",
            "삼성물산": "028260", "현대모비스": "012330", "SK이노베이션": "096770",
            "LG전자": "066570", "두산에너빌리티": "034020", "한국전력": "015760",
            "카카오뱅크": "323410", "크래프톤": "259960", "넷마블": "251270",
            "엔씨소프트": "036570", "에코프로": "086520", "에코프로비엠": "247540",
            "포스코퓨처엠": "003670", "고려아연": "010130", "HLB": "028300",
            "삼성생명": "032830", "삼성화재": "000810", "현대건설": "000720",
            "GS건설": "006360", "대우건설": "047040", "롯데케미칼": "011170",
            "한화솔루션": "009830", "SK텔레콤": "017670", "KT": "030200",
            "LG유플러스": "032640", "카카오페이": "377300", "토스": "403870",
        }
        for k, v in COMMON.items():
            if name in k or k in name:
                return {"ticker": v, "name": k}
        return None
