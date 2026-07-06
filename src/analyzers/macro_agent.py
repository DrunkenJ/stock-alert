"""
거시경제 판단 에이전트 (강화판)
- VIX (공포지수) - 시장 불안 수치
- 원달러 환율 - 외국인 자금 흐름
- 미국 10년물 금리 - 글로벌 유동성
- 나스닥/S&P500 전일 방향 - 미국 시장 방향
- 뉴스 헤드라인 - 정성적 판단
→ GPT로 종합 판단 (Risk On/Off/중립)
"""
import os
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from loguru import logger
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


def _get_market_data() -> dict:
    """주요 거시 지표 수집 (Yahoo chart API 직접 호출)"""
    from src.utils.yahoo_direct import fetch_quote

    tickers = {
        "VIX":     "^VIX",      # 공포지수
        "USDKRW":  "USDKRW=X",  # 원달러 환율
        "US10Y":   "^TNX",      # 미국 10년물 금리
        "NASDAQ":  "^IXIC",     # 나스닥
        "SP500":   "^GSPC",     # S&P500
        "DXY":     "DX-Y.NYB",  # 달러인덱스
    }

    result = {}
    for name, symbol in tickers.items():
        q = fetch_quote(symbol, range_period="5d")
        if q:
            result[name] = {
                "value": q["value"],
                "change_pct": q["change_pct"],
            }

    logger.info(f"거시 데이터 수집: {list(result.keys())}")
    return result


def _get_news() -> list[str]:
    """Google RSS로 주요 경제 뉴스 수집"""
    headlines = []
    queries = ["코스피 오늘", "미국 증시", "원달러 환율", "연준 금리", "한국 경제"]

    for query in queries:
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
            resp = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:2]:
                    title = item.find("title")
                    if title is not None and title.text:
                        headlines.append(title.text.strip())
        except Exception as e:
            logger.debug(f"뉴스 수집 실패 ({query}): {e}")

    logger.info(f"뉴스 수집: {len(headlines)}건")
    return headlines[:15]


class MacroAgent:
    """거시경제 판단 에이전트"""

    RISK_ON  = "risk_on"
    RISK_OFF = "risk_off"
    NEUTRAL  = "neutral"

    def __init__(self):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def analyze(self) -> dict:
        """거시 판단 실행"""
        logger.info("거시경제 판단 시작")

        market_data = _get_market_data()
        news        = _get_news()

        # ── 데이터 요약 텍스트 ────────────────────────────────
        data_lines = []
        if market_data:
            vix    = market_data.get("VIX",    {})
            usdkrw = market_data.get("USDKRW", {})
            us10y  = market_data.get("US10Y",  {})
            nasdaq = market_data.get("NASDAQ", {})
            sp500  = market_data.get("SP500",  {})
            dxy    = market_data.get("DXY",    {})

            if vix:
                level = "공포" if vix["value"] > 30 else ("경계" if vix["value"] > 20 else "안정")
                data_lines.append(f"VIX(공포지수): {vix['value']} ({level}) {vix['change_pct']:+.2f}%")
            if usdkrw:
                data_lines.append(f"원달러환율: {usdkrw['value']:,.0f}원 {usdkrw['change_pct']:+.2f}%")
            if us10y:
                data_lines.append(f"미국10년물금리: {us10y['value']:.2f}% {us10y['change_pct']:+.2f}%")
            if nasdaq:
                data_lines.append(f"나스닥: {nasdaq['value']:,.0f} {nasdaq['change_pct']:+.2f}%")
            if sp500:
                data_lines.append(f"S&P500: {sp500['value']:,.0f} {sp500['change_pct']:+.2f}%")
            if dxy:
                data_lines.append(f"달러인덱스: {dxy['value']:.2f} {dxy['change_pct']:+.2f}%")

        news_lines = "\n".join(f"- {h}" for h in news)
        data_text  = "\n".join(data_lines) if data_lines else "데이터 수집 실패"
        today      = datetime.now().strftime("%Y년 %m월 %d일")

        prompt = f"""
당신은 한국 주식시장 거시경제 분석가입니다.
오늘({today}) 글로벌 지표와 뉴스를 분석하여 한국 주식시장의 단기 방향을 판단하세요.

[글로벌 시장 지표]
{data_text}

[주요 뉴스 헤드라인]
{news_lines if news_lines else "수집 실패"}

판단 기준:
- VIX > 30: 강한 Risk Off 신호
- VIX 20~30: 경계 구간, 신중 접근
- VIX < 15: 안정, Risk On 우호
- 원달러 상승(원화 약세): 외국인 매도 압력
- 나스닥/S&P500 하락: 한국 시장 동반 하락 가능성

다음 JSON 형식으로만 응답하세요:
{{
  "judgment": "risk_on 또는 risk_off 또는 neutral",
  "confidence": 0~100,
  "vix_signal": "안정 또는 경계 또는 공포",
  "key_factors": ["핵심 요인 1", "핵심 요인 2", "핵심 요인 3"],
  "market_summary": "시장 상황 한 줄 요약 (40자 이내)",
  "recommended_picks": 1~5,
  "caution_message": "주의사항 (없으면 빈 문자열)"
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
            result = json.loads(resp.choices[0].message.content)
            result["market_data"] = market_data
            result["news_count"]  = len(news)
            result["top_news"]    = news[:5]
            logger.info(f"거시 판단: {result.get('judgment')} / VIX={market_data.get('VIX',{}).get('value','N/A')} / 확신도={result.get('confidence')}%")
            return result

        except Exception as e:
            logger.error(f"거시 판단 GPT 오류: {e}")
            return self._neutral_result(str(e))

    def _neutral_result(self, reason: str = "") -> dict:
        return {
            "judgment": self.NEUTRAL,
            "confidence": 0,
            "vix_signal": "알 수 없음",
            "key_factors": [reason or "판단 불가"],
            "market_summary": "판단 불가 - 기본 전략 유지",
            "recommended_picks": int(os.getenv("FINAL_PICKS", "5")),
            "caution_message": "",
            "market_data": {},
            "news_count": 0,
            "top_news": [],
        }

    def get_picks_count(self, judgment: str, confidence: int) -> int:
        """거시 판단에 따른 추천 종목 수 결정"""
        base = int(os.getenv("FINAL_PICKS", "5"))

        if judgment == self.RISK_OFF:
            if confidence >= 70:
                return max(1, base - 3)
            elif confidence >= 50:
                return max(2, base - 2)
            else:
                return max(3, base - 1)
        elif judgment == self.RISK_ON:
            if confidence >= 70:
                return min(base + 1, 7)
            else:
                return base
        else:
            return base
