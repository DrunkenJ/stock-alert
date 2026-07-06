"""
프리장 분석기 (08:40 실행)
- 미국 시장 마감 영향 분석 (S&P500, 나스닥, 반도체 등)
- 야간 뉴스/공시 호재 종목 스캔
- 어제 강한 종목 + 시간외 매수 종목 추출
- 본장 시초가 진입 후보 사전 안내
"""
import os
import json
import yfinance as yf
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

PREMARKET_CACHE = Path("data/premarket_cache.json")

# 미국 주요 지수 + 한국 영향 큰 ETF
US_TICKERS = {
    "S&P500":   "^GSPC",
    "NASDAQ":   "^IXIC",
    "SOX":      "^SOX",        # 반도체 지수
    "VIX":      "^VIX",
    "DXY":      "DX-Y.NYB",    # 달러 인덱스
    "USDKRW":   "KRW=X",
    "OIL":      "CL=F",        # WTI 유가
}

# 미국→한국 영향 매핑 (섹터별)
US_TO_KR_SECTOR = {
    "SOX_up":      ["반도체", "AI"],
    "OIL_up":      ["에너지", "정유"],
    "OIL_down":    ["항공", "운송"],
    "USDKRW_up":   ["수출주", "자동차", "조선"],
    "USDKRW_down": ["내수", "유통"],
}


class PremarketAnalyzer:
    """프리장 분석기"""

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if api_key else None
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def collect_us_market(self) -> dict:
        """06:30 - 미국 시장 마감 데이터 수집만"""
        logger.info("미국 시장 마감 데이터 수집 시작")
        us_data = self._fetch_us_market()
        logger.info(f"미국 시장: {len(us_data)}개 지수 수집")

        # 기존 캐시 + 미국 데이터 저장
        cache = self._load_cache() or {}
        cache["us_market"] = us_data
        cache["us_collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save_cache(cache)
        return us_data

    def collect_overnight_news(self) -> list:
        """07:00 - 야간 뉴스 수집만"""
        logger.info("야간 뉴스 수집 시작")
        news = self._fetch_overnight_news()
        logger.info(f"야간 뉴스: {len(news)}건")

        cache = self._load_cache() or {}
        cache["overnight_news"] = news
        cache["news_collected_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save_cache(cache)
        return news

    def analyze(self) -> dict:
        """08:40 - 종합 분석 (이전 수집 데이터 활용)"""
        logger.info("프리장 종합 분석 시작")

        # 캐시에서 06:30/07:00 수집 데이터 로드
        cache = self._load_cache() or {}
        us_data = cache.get("us_market", {})
        news    = cache.get("overnight_news", [])

        # 캐시 없으면 즉시 수집 (백업)
        if not us_data:
            logger.info("미국 시장 캐시 없음 - 즉시 수집")
            us_data = self._fetch_us_market()
        if not news:
            logger.info("야간 뉴스 캐시 없음 - 즉시 수집")
            news = self._fetch_overnight_news()

        # AI 종합 분석
        impact = self._analyze_impact(us_data, news)
        candidates = self._extract_candidates(us_data, impact)

        result = {
            "us_market": us_data,
            "overnight_news": news,
            "impact": impact,
            "candidates": candidates,
            "us_collected_at":  cache.get("us_collected_at", ""),
            "news_collected_at": cache.get("news_collected_at", ""),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._save_cache(result)
        return result

    def _load_cache(self) -> dict:
        if not PREMARKET_CACHE.exists():
            return {}
        try:
            with open(PREMARKET_CACHE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _fetch_us_market(self) -> dict:
        """미국 시장 데이터 수집 (Yahoo chart API 직접 호출)"""
        from src.utils.yahoo_direct import fetch_quote

        result = {}
        for name, ticker in US_TICKERS.items():
            q = fetch_quote(ticker, range_period="5d")
            if q:
                change_pct = q["change_pct"]
                result[name] = {
                    "value": q["value"],
                    "change_pct": change_pct,
                    "trend": "up" if change_pct > 0 else ("down" if change_pct < 0 else "flat"),
                }
        return result

    def _fetch_overnight_news(self) -> list:
        """야간 뉴스 수집 (Google RSS) - 한국 주식 특화 키워드"""
        keywords = [
            # 수급/외국인 동향
            "코스피 외국인 순매수",
            # 기업 호재
            "수주 계약 체결 코스피",
            # 실적
            "실적 서프라이즈 코스닥",
            # 주요 섹터
            "반도체 SK하이닉스 삼성전자",
            # 공시
            "공시 대규모 수주",
            # 글로벌 영향
            "나스닥 반도체 한국",
        ]
        headlines = []
        seen = set()

        for kw in keywords:
            try:
                url = (
                    f"https://news.google.com/rss/search"
                    f"?q={requests.utils.quote(kw)}&hl=ko&gl=KR&ceid=KR:ko"
                )
                resp = requests.get(
                    url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}
                )
                if resp.status_code != 200:
                    continue

                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:4]:
                    title = item.find("title")
                    pub   = item.find("pubDate")
                    if title is None or not title.text:
                        continue
                    text = title.text.strip()
                    # 중복 제거
                    if text in seen:
                        continue
                    # 최근 14시간 이내만
                    if pub is not None and not self._is_recent(pub.text, hours=14):
                        continue
                    # 무관한 뉴스 필터 (외국 종목, 광고성)
                    skip_keywords = ["미국 주식", "해외 ETF", "비트코인", "암호화폐", "부동산"]
                    if any(sk in text for sk in skip_keywords):
                        continue
                    seen.add(text)
                    headlines.append(text)

            except Exception as e:
                logger.debug(f"뉴스 수집 실패 ({kw}): {e}")

        logger.info(f"야간 뉴스 수집 완료: {len(headlines)}건")
        return headlines[:20]

    def _is_recent(self, pub_date: str, hours: int = 14) -> bool:
        """최근 N시간 이내 뉴스인지 확인 (기존 메서드 대체)"""
        try:
            from email.utils import parsedate_to_datetime
            pub = parsedate_to_datetime(pub_date)
            now = datetime.now(pub.tzinfo) if pub.tzinfo else datetime.now()
            return (now - pub) < timedelta(hours=hours)
        except Exception:
            return True  # 파싱 실패 시 포함



    def _analyze_impact(self, us_data: dict, news: list) -> dict:
        """AI로 한국 시장 영향 분석"""
        if not self.client or not us_data:
            return {"summary": "데이터 부족", "sectors": [], "stance": "neutral"}

        us_text = "\n".join(
            f"- {k}: {v['value']} ({v['change_pct']:+.2f}%)"
            for k, v in us_data.items()
        )
        news_text = "\n".join(f"- {n}" for n in news[:10])

        prompt = f"""
미국 시장 마감 결과와 야간 뉴스를 바탕으로 오늘 한국 시장 영향을 분석하세요.

[미국 시장]
{us_text}

[야간 뉴스]
{news_text or "주요 뉴스 없음"}

다음 JSON으로만 응답:
{{
  "summary": "한국 시장 전망 한 줄 요약",
  "stance": "bullish 또는 bearish 또는 neutral",
  "confidence": 0~100,
  "key_sectors": ["오를 가능성 높은 섹터 1~3개"],
  "avoid_sectors": ["하락 우려 섹터 0~2개"],
  "key_drivers": ["주요 동인 2~3개"],
  "entry_strategy": "시초가 진입 전략 한 줄"
}}
"""
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
                response_format={"type": "json_object"},
            )
            return json.loads(resp.choices[0].message.content)
        except Exception as e:
            logger.warning(f"AI 영향 분석 실패: {e}")
            return {"summary": "분석 불가", "stance": "neutral"}

    def _extract_candidates(self, us_data: dict, impact: dict) -> list[dict]:
        """본장 진입 후보 추출"""
        candidates = []
        key_sectors = impact.get("key_sectors", [])

        # 어제 picks 파일에서 강세 종목 가져오기
        try:
            picks_file = Path("data/today_picks.json")
            if picks_file.exists():
                with open(picks_file) as f:
                    yesterday_picks = json.load(f)

                for pick in yesterday_picks[:5]:
                    candidates.append({
                        "type": "yesterday_strong",
                        "ticker": pick.get("ticker", ""),
                        "name": pick.get("name", ""),
                        "reason": f"어제 추천 (점수 {pick.get('final_score', 0):.1f})",
                    })
        except Exception:
            pass

        # 미국 영향 큰 섹터 표시 (구체적 종목은 본장에서 결정)
        if "반도체" in key_sectors or "AI" in key_sectors:
            sox_data = us_data.get("SOX", {})
            if sox_data.get("change_pct", 0) > 1.0:
                candidates.append({
                    "type": "us_influence",
                    "ticker": "",
                    "name": "반도체/AI 섹터 전반",
                    "reason": f"미국 SOX {sox_data.get('change_pct'):+.2f}% → 갭상승 예상",
                })

        return candidates[:8]

    def _save_cache(self, result: dict):
        PREMARKET_CACHE.parent.mkdir(exist_ok=True)
        with open(PREMARKET_CACHE, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
