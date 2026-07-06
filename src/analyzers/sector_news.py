"""
섹터 분류 + 뉴스 감성 분석 모듈
- 섹터 분산: 동일 섹터 최대 2종목 제한
- 뉴스 감성: 종목별 최근 뉴스 GPT 분석
  → 악재 종목 사전 차단
  → 호재 종목 점수 보너스
"""
import os
import json
import requests
import xml.etree.ElementTree as ET
from loguru import logger
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────────────────
# 섹터 매핑 (티커 앞 2자리 기준 + 종목명 키워드)
# ─────────────────────────────────────────────────────────
SECTOR_KEYWORDS = {
    "반도체":    ["반도체", "하이닉스", "삼성전자", "DB하이텍", "한미반도체", "리노공업", "ISC"],
    "2차전지":   ["에코프로", "포스코퓨처엠", "엘앤에프", "천보", "솔루스", "이차전지", "배터리"],
    "바이오":    ["바이오", "제약", "의약", "헬스케어", "셀트리온", "한미약품", "HLB", "알테오젠"],
    "자동차":    ["현대차", "기아", "모비스", "만도", "한온", "오토에버", "자동차"],
    "조선":      ["조선", "중공업", "HD현대", "삼성중공업", "한국조선"],
    "방산":      ["한화", "LIG넥스원", "한국항공", "방산", "에어로스페이스"],
    "금융":      ["금융", "은행", "증권", "보험", "KB", "신한", "하나", "우리", "메리츠"],
    "IT서비스":  ["네이버", "카카오", "NHN", "더존", "아이티"],
    "게임":      ["게임", "엔씨소프트", "넷마블", "크래프톤", "펄어비스", "카카오게임"],
    "엔터":      ["하이브", "SM", "JYP", "YG", "엔터테인먼트"],
    "유통":      ["이마트", "롯데", "신세계", "BGF", "GS리테일"],
    "화학":      ["화학", "케미칼", "LG화학", "롯데케미칼", "SKC"],
    "철강":      ["철강", "POSCO", "현대제철", "고려아연", "포스코"],
    "건설":      ["건설", "현대건설", "GS건설", "대우건설", "DL이앤씨"],
    "통신":      ["통신", "SKT", "KT", "LGU", "유플러스"],
    "에너지":    ["에너지", "한국전력", "한전", "두산에너빌", "원자력"],
    "기타":      [],
}


def classify_sector(name: str, ticker: str = "") -> str:
    """종목명으로 섹터 분류"""
    for sector, keywords in SECTOR_KEYWORDS.items():
        if sector == "기타":
            continue
        if any(kw in name for kw in keywords):
            return sector
    return "기타"


def filter_by_sector_diversity(picks: list[dict], max_per_sector: int = 2) -> list[dict]:
    """
    섹터 다변화 필터
    - 동일 섹터 최대 max_per_sector 종목
    - 점수 높은 순으로 선택
    """
    # 섹터 분류
    for pick in picks:
        pick["sector"] = classify_sector(
            pick.get("name", ""), pick.get("ticker", "")
        )

    # 섹터별 그룹화 (점수 높은 순 유지)
    sector_counts = {}
    result = []
    excluded = []

    for pick in picks:
        sector = pick["sector"]
        count = sector_counts.get(sector, 0)
        if count < max_per_sector:
            sector_counts[sector] = count + 1
            result.append(pick)
        else:
            excluded.append(pick)

    if excluded:
        excluded_names = [f"{p['name']}({p['sector']})" for p in excluded]
        logger.info(f"섹터 집중 제외: {', '.join(excluded_names)}")

    # 섹터 분포 로그
    sector_dist = {}
    for p in result:
        s = p["sector"]
        sector_dist[s] = sector_dist.get(s, 0) + 1
    logger.info(f"섹터 배분: {sector_dist}")

    return result


# ─────────────────────────────────────────────────────────
# 뉴스 감성 분석
# ─────────────────────────────────────────────────────────

def fetch_stock_news(name: str, max_articles: int = 5) -> list[str]:
    """종목별 최근 뉴스 헤드라인 수집"""
    headlines = []
    queries = [name, f"{name} 주가", f"{name} 실적"]

    for query in queries[:2]:
        try:
            url = (
                f"https://news.google.com/rss/search"
                f"?q={requests.utils.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
            )
            resp = requests.get(
                url, timeout=5, headers={"User-Agent": "Mozilla/5.0"}
            )
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item")[:3]:
                    title = item.find("title")
                    if title is not None and title.text:
                        headlines.append(title.text.strip())
        except Exception as e:
            logger.debug(f"뉴스 수집 실패 ({name}): {e}")

    return list(dict.fromkeys(headlines))[:max_articles]  # 중복 제거


def analyze_news_sentiment(name: str, ticker: str,
                           headlines: list[str]) -> dict:
    """GPT로 뉴스 감성 분석"""
    if not headlines:
        return {
            "sentiment": "neutral",
            "score_adj": 0,
            "reason": "뉴스 없음",
            "is_buyable": True,
        }

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    news_text = "\n".join(f"- {h}" for h in headlines)

    prompt = f"""
다음은 {name}({ticker}) 종목의 최근 뉴스 헤드라인입니다.
투자자 관점에서 단기(1~5일) 주가에 미치는 영향을 분석하세요.

[뉴스]
{news_text}

다음 JSON으로만 응답:
{{
  "sentiment": "positive 또는 negative 또는 neutral",
  "severity": "high 또는 medium 또는 low",
  "score_adj": -3에서 +3 사이 정수 (주가 영향 점수),
  "key_news": "핵심 뉴스 한 줄 요약",
  "is_buyable": true 또는 false,
  "reason": "판단 이유 한 줄"
}}

판단 기준:
- negative + high: 횡령/소송패소/실적쇼크/대규모리콜 → is_buyable=false
- negative + medium: 실적부진/경쟁심화 → score_adj=-1~-2
- positive + high: 대규모수주/실적서프라이즈/M&A → score_adj=+2~+3
- neutral: 영향 없음
"""
    try:
        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)
        result["headlines"] = headlines
        logger.debug(
            f"뉴스분석 {name}: {result['sentiment']}({result['severity']}) "
            f"adj={result['score_adj']} buyable={result['is_buyable']}"
        )
        return result
    except Exception as e:
        logger.debug(f"감성분석 GPT 오류 ({name}): {e}")
        return {
            "sentiment": "neutral",
            "score_adj": 0,
            "reason": f"분석 오류: {e}",
            "is_buyable": True,
            "headlines": headlines,
        }


def apply_news_filter(picks: list[dict], max_news_per_run: int = 10) -> list[dict]:
    """
    전체 추천 종목에 뉴스 감성 필터 적용
    - 악재 종목 제외
    - 호재 종목 점수 보너스
    - API 비용 절감: 상위 max_news_per_run 종목만 분석
    """
    result = []
    excluded = []
    analyzed = 0

    for pick in picks:
        name = pick.get("name", "")
        ticker = pick.get("ticker", "")

        # 뉴스 수집
        if analyzed < max_news_per_run:
            headlines = fetch_stock_news(name)
            sentiment = analyze_news_sentiment(name, ticker, headlines)
            analyzed += 1
        else:
            # 분석 한도 초과 시 중립 처리
            sentiment = {
                "sentiment": "neutral",
                "score_adj": 0,
                "reason": "뉴스 분석 한도 초과",
                "is_buyable": True,
                "headlines": [],
            }

        pick["news_sentiment"] = sentiment

        # 매수 불가 종목 제외
        if not sentiment.get("is_buyable", True):
            excluded.append(f"{name}({sentiment.get('reason', '')})")
            continue

        # 점수 보정 적용
        adj = sentiment.get("score_adj", 0)
        if adj != 0:
            pick["final_score"] = pick.get("final_score", 0) + adj * 0.3
            pick["news_adj"] = adj

        result.append(pick)

    if excluded:
        logger.info(f"뉴스 악재 제외: {', '.join(excluded)}")

    # 점수 재정렬
    result.sort(key=lambda x: x.get("final_score", 0), reverse=True)
    return result
