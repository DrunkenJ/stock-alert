from typing import Optional, Tuple, List, Dict, Any
"""
AI 종목 평가 모듈 - 3단계 멀티 에이전트 구조
[1단계] 분석가: 매수 근거 생성
[2단계] 리스크 검증가: 반론/리스크 제기
[3단계] CIO: 두 의견 종합 -> 최종 판단
"""
import os
import json
from openai import OpenAI
from loguru import logger
from dotenv import load_dotenv

load_dotenv()


def _format_signals(signals) -> str:
    """tech_signals를 안전하게 문자열로 변환"""
    result = []
    for s in signals:
        if isinstance(s, str):
            result.append(s)
        elif isinstance(s, dict):
            text = s.get("name") or s.get("signal") or s.get("message") or str(s)
            result.append(str(text))
        else:
            result.append(str(s))
    return ", ".join(result)


class AIEvaluator:
    """3단계 멀티 에이전트 종목 평가"""

    def __init__(self):
        api_key = os.getenv("OPENAI_API_KEY")
        model   = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        if not api_key:
            raise ValueError("OPENAI_API_KEY 미설정")
        self.client = OpenAI(api_key=api_key)
        self.model  = model
        logger.info(f"AI Evaluator 초기화 - 모델: {model} (3단계)")

    def evaluate_stock(self, stock_info: dict) -> dict:
        """3단계 AI 평가 실행"""
        try:
            analysis    = self._run_analyst(stock_info)
            logger.debug(f"[1단계] 분석가 완료: {stock_info['name']} -> {analysis.get('recommendation')}")
            risk_review = self._run_risk_verifier(stock_info, analysis)
            logger.debug(f"[2단계] 검증가 완료: 위험도={risk_review.get('risk_level')}")
            final       = self._run_cio(stock_info, analysis, risk_review)
            logger.debug(f"[3단계] CIO 완료: 점수={final.get('ai_score')} 판단={final.get('recommendation')}")
            final = self._validate_prices(stock_info["price"], final)
            final["_analyst"] = analysis
            final["_risk"]    = risk_review
            return final
        except Exception as e:
            logger.error(f"AI 평가 오류 ({stock_info.get('name', '')}): {e}")
            return self._fallback(stock_info)

    def generate_market_summary(self, picks: list, regime: str = "") -> str:
        """장 전체 시황 요약 생성"""
        if not picks:
            return "오늘 추천 종목 없음"
        names   = [p.get("name", "") for p in picks[:5]]
        sectors = list(set(p.get("sector", "") for p in picks if p.get("sector")))
        prompt  = (
            f"오늘 한국 주식 추천 종목: {', '.join(names)}\n"
            f"주요 섹터: {', '.join(sectors)}\n"
            f"시장 국면: {regime}\n\n"
            "위 정보를 바탕으로 오늘의 투자 전략을 2~3줄로 요약해주세요."
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=150,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"시황 요약 오류: {e}")
            return f"외국인/기관 수급 중심 {', '.join(names[:3])} 주목"

    def _run_analyst(self, s: dict) -> dict:
        """1단계: 매수 근거 생성"""
        supply_pattern   = s.get("supply_pattern", {}).get("pattern", "일반")
        score_confidence = s.get("score_confidence", "")
        signals_str      = _format_signals(s.get("tech_signals", [])[:3])

        prompt = (
            f"당신은 한국 주식 전문 퀀트 애널리스트입니다.\n"
            f"아래 데이터를 분석하여 매수 관점의 투자 근거를 작성하세요.\n\n"
            f"종목: {s['name']} ({s['ticker']}) | 현재가: {s['price']:,}원 ({s['change_rate']:+.2f}%)\n"
            f"점수: 종합 {s.get('total_score', 0):.1f}점 | 신뢰도: {score_confidence}\n"
            f"기술적 분석 점수: {s['tech_score']}/15\n"
            f"- RSI: {s['indicators'].get('rsi', 'N/A')}\n"
            f"- 거래량 배율: {s['indicators'].get('vol_ratio', 'N/A')}배\n"
            f"- 신호: {signals_str}\n"
            f"수급 분석 점수: {s['supply_score']}/15\n"
            f"- 외국인 5일: {s['supply_summary']['foreign_net']:+,}주\n"
            f"- 기관 5일: {s['supply_summary']['inst_net']:+,}주\n"
            f"- 동반매수: {s['supply_summary']['is_double_buy']}\n"
            f"- 수급패턴: {supply_pattern}\n\n"
            f"다음 JSON으로만 응답:\n"
            f'{{"recommendation":"강력매수|매수|관망|매도회피",'
            f'"confidence":0~100,'
            f'"bull_case":"매수 근거 핵심 2가지",'
            f'"target_price":{s["price"]}원 기준 목표가 정수,'
            f'"stop_loss":{s["price"]}원 기준 손절가 정수,'
            f'"holding_days":1~10}}'
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "당신은 매수 관점의 퀀트 애널리스트입니다. JSON으로만 응답하세요."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)

    def _run_risk_verifier(self, s: dict, analysis: dict) -> dict:
        """2단계: 반론/리스크 제기"""
        prompt = (
            f"당신은 투자 리스크 관리 전문가입니다.\n"
            f"분석가의 매수 의견에 반론을 제기하고 숨겨진 리스크를 찾아내세요.\n\n"
            f"종목: {s['name']} ({s['ticker']})\n"
            f"추천: {analysis.get('recommendation')} (확신도: {analysis.get('confidence')}%)\n"
            f"매수 근거: {analysis.get('bull_case')}\n"
            f"RSI: {s['indicators'].get('rsi', 'N/A')}\n"
            f"거래량 배율: {s['indicators'].get('vol_ratio', 'N/A')}배\n"
            f"수급패턴: {s.get('supply_pattern', {}).get('pattern', '일반')}\n\n"
            f"다음 JSON으로만 응답:\n"
            f'{{"risk_level":"high|medium|low",'
            f'"bear_case":"반론/리스크 핵심 1~2가지",'
            f'"is_buyable":true또는false,'
            f'"score_penalty":0~2,'
            f'"key_risk":"가장 주의할 리스크 한 줄"}}\n\n'
            f"주의: 일반적인 시장 리스크는 medium/low로 판단하세요.\n"
            f"횡령/소송/실적쇼크 등 명백한 악재가 없으면 is_buyable=true로 판단하세요."
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "당신은 비관적 관점의 리스크 관리자입니다. JSON으로만 응답하세요."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.4,
            max_tokens=250,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)

    def _run_cio(self, s: dict, analysis: dict, risk: dict) -> dict:
        """3단계: 최종 투자 판단"""
        prompt = (
            f"당신은 최고투자책임자(CIO)입니다.\n"
            f"분석가와 리스크 검증가의 의견을 종합하여 최종 투자 판단을 내리세요.\n\n"
            f"[분석가]\n"
            f"추천: {analysis.get('recommendation')} / 확신도: {analysis.get('confidence')}%\n"
            f"근거: {analysis.get('bull_case')}\n"
            f"목표가: {analysis.get('target_price', 0):,}원\n\n"
            f"[리스크 검증가]\n"
            f"위험도: {risk.get('risk_level')} / 매수가능: {risk.get('is_buyable')}\n"
            f"반론: {risk.get('bear_case')}\n"
            f"핵심리스크: {risk.get('key_risk')}\n\n"
            f"{s['name']} ({s['ticker']}) | {s['price']:,}원\n"
            f"종합점수: {s.get('total_score', 0):.1f}점\n\n"
            f"리스크가 high이고 is_buyable=false인 경우에만 ai_score를 4 이하로 설정.\n"
            f"일반적인 medium 리스크는 ai_score에 크게 영향주지 마세요.\n"
            f"수급과 기술적 신호가 좋으면 6~8점을 줄 수 있습니다.\n\n"
            f"다음 JSON으로만 응답:\n"
            f'{{"ai_score":0~10,'
            f'"recommendation":"강력매수|매수|관망|매도회피",'
            f'"target_price":목표가정수,'
            f'"stop_loss":손절가정수,'
            f'"reason":"최종 판단 근거 2~3줄",'
            f'"risk":"주요 리스크 1줄",'
            f'"holding_days":예상보유일수정수,'
            f'"consensus":"strong|moderate|weak"}}'
        )
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "당신은 균형 잡힌 시각의 CIO입니다. JSON으로만 응답하세요."},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.2,
            max_tokens=350,
            response_format={"type": "json_object"},
        )
        result = json.loads(resp.choices[0].message.content)

        penalty = risk.get("score_penalty", 0)
        if penalty > 0:
            result["ai_score"] = max(0, result.get("ai_score", 5) - penalty * 0.2)

        if not risk.get("is_buyable", True):
            result["ai_score"] = min(result.get("ai_score", 5), 4.0)
            if result.get("recommendation") in ["강력매수", "매수"]:
                result["recommendation"] = "관망"

        return result

    def _validate_prices(self, price: int, result: dict) -> dict:
        """목표가/손절가 비정상값 보정"""
        target = result.get("target_price", 0)
        stop   = result.get("stop_loss", 0)
        if not (price * 0.5 <= target <= price * 2.0):
            result["target_price"] = int(price * 1.06)
            logger.warning(f"목표가 비정상({target:,}) -> {result['target_price']:,}원 보정")
        if not (price * 0.7 <= stop <= price * 1.0):
            result["stop_loss"] = int(price * 0.97)
            logger.warning(f"손절가 비정상({stop:,}) -> {result['stop_loss']:,}원 보정")
        return result

    def _fallback(self, s: dict) -> dict:
        """오류 시 기본값 반환"""
        price = s.get("price", 0)
        return {
            "ai_score":       5,
            "recommendation": "관망",
            "target_price":   int(price * 1.06),
            "stop_loss":      int(price * 0.97),
            "reason":         "AI 평가 오류 - 기본값 적용",
            "risk":           "데이터 부족",
            "holding_days":   3,
            "consensus":      "weak",
        }
