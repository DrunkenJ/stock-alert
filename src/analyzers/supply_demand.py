"""
수급 분석 모듈
- 외국인/기관 순매수 트렌드 분석
- 수급 강도 점수화
- 동반 매수 패턴 감지
"""
from loguru import logger


class SupplyDemandAnalyzer:
    """외국인 · 기관 수급 분석"""

    @staticmethod
    def _net_ratio(net_shares: int, price: float, market_cap: float) -> float | None:
        """순매수 주식 수를 시가총액 대비 비중(%)으로 환산

        절대 주식 수(예: 50만주)는 3천원짜리 주식과 30만원짜리 주식에서
        전혀 다른 의미를 갖는다. 저가주 편향을 없애기 위해 금액 비중으로 정규화한다.
        시총 정보가 없으면 None → 호출부에서 기존 주식 수 기준으로 폴백.
        """
        if not price or not market_cap or market_cap <= 0:
            return None
        return net_shares * price / market_cap * 100

    def analyze(self, investor_data: dict, price_data: dict) -> dict:
        """수급 데이터 종합 분석 → 점수 반환"""
        score = 0
        signals = []

        foreign_net = investor_data.get("foreign_net", 0)
        inst_net = investor_data.get("inst_net", 0)
        foreign_consec = investor_data.get("foreign_consecutive", 0)
        inst_consec = investor_data.get("inst_consecutive", 0)
        detail = investor_data.get("detail", [])

        price = price_data.get("price", 0)
        market_cap = price_data.get("market_cap", 0)
        f_ratio = self._net_ratio(foreign_net, price, market_cap)
        i_ratio = self._net_ratio(inst_net, price, market_cap)

        # ── 외국인 순매수 분석 ────────────────────
        # 시총 대비 비중 기준 (폴백: 절대 주식 수)
        if f_ratio is not None:
            big, mid, big_sell = f_ratio > 0.5, f_ratio > 0.15, f_ratio < -0.5
            f_desc = f"5일 합산 시총 대비 {f_ratio:+.2f}%"
        else:
            big, mid, big_sell = foreign_net > 500_000, foreign_net > 100_000, foreign_net < -500_000
            f_desc = f"5일 합산 {foreign_net:+,}주"

        if foreign_net > 0:
            # 순매수 강도
            if big:
                score += 4
                signals.append({
                    "type": "positive",
                    "name": "외국인 대규모 순매수",
                    "detail": f_desc
                })
            elif mid:
                score += 2
                signals.append({
                    "type": "positive",
                    "name": "외국인 순매수",
                    "detail": f_desc
                })
            else:
                score += 1
        elif big_sell:
            score -= 3
            signals.append({
                "type": "negative",
                "name": "외국인 대규모 순매도",
                "detail": f_desc
            })

        # 연속 매수일
        if foreign_consec >= 5:
            score += 3
            signals.append({
                "type": "positive",
                "name": f"외국인 {foreign_consec}일 연속 순매수",
                "detail": "지속적인 외국인 매수세"
            })
        elif foreign_consec >= 3:
            score += 1
            signals.append({
                "type": "positive",
                "name": f"외국인 {foreign_consec}일 연속 순매수",
                "detail": ""
            })

        # ── 기관 순매수 분석 ──────────────────────
        if i_ratio is not None:
            i_big, i_mid, i_big_sell = i_ratio > 0.3, i_ratio > 0.1, i_ratio < -0.3
            i_desc = f"5일 합산 시총 대비 {i_ratio:+.2f}%"
        else:
            i_big, i_mid, i_big_sell = inst_net > 300_000, inst_net > 50_000, inst_net < -300_000
            i_desc = f"5일 합산 {inst_net:+,}주"

        if inst_net > 0:
            if i_big:
                score += 3
                signals.append({
                    "type": "positive",
                    "name": "기관 대규모 순매수",
                    "detail": i_desc
                })
            elif i_mid:
                score += 2
                signals.append({
                    "type": "positive",
                    "name": "기관 순매수",
                    "detail": i_desc
                })
            else:
                score += 1
        elif i_big_sell:
            score -= 2
            signals.append({
                "type": "negative",
                "name": "기관 순매도",
                "detail": i_desc
            })

        if inst_consec >= 5:
            score += 2
            signals.append({
                "type": "positive",
                "name": f"기관 {inst_consec}일 연속 순매수",
                "detail": ""
            })

        # ── 외국인 + 기관 동반 매수 (핵심 시그널) ──
        if foreign_net > 0 and inst_net > 0:
            score += 3
            signals.append({
                "type": "positive",
                "name": "⭐ 외국인·기관 동반 순매수",
                "detail": f"외국인 +{foreign_net:,} / 기관 +{inst_net:,}"
            })

        # ── 최근 3일 추세 가속 ────────────────────
        if len(detail) >= 3:
            recent_3 = detail[:3]
            recent_foreign = [d.get("foreign", 0) for d in recent_3]
            recent_inst = [d.get("inst", 0) for d in recent_3]

            if all(f > 0 for f in recent_foreign) and recent_foreign[0] > recent_foreign[2]:
                score += 1
                signals.append({
                    "type": "positive",
                    "name": "외국인 매수 가속",
                    "detail": "최근 3일 매수량 증가"
                })

            if all(i > 0 for i in recent_inst) and recent_inst[0] > recent_inst[2]:
                score += 1
                signals.append({
                    "type": "positive",
                    "name": "기관 매수 가속",
                    "detail": "최근 3일 매수량 증가"
                })

        # ── 외국인 보유 비중 변화 ────────────────
        # (가격 데이터 활용: 외국인 순매수량 / 전체 거래량 비율)
        total_vol = price_data.get("volume", 1)
        if total_vol > 0:
            foreign_vol_ratio = abs(foreign_net) / total_vol if foreign_net > 0 else 0
            if foreign_vol_ratio > 0.3:
                score += 1
                signals.append({
                    "type": "positive",
                    "name": "외국인 거래 집중",
                    "detail": f"전체 거래량의 {foreign_vol_ratio:.1%}"
                })

        return {
            "score": max(0, min(score, 15)),
            "signals": signals,
            "summary": {
                "foreign_net": foreign_net,
                "inst_net": inst_net,
                "foreign_consecutive": foreign_consec,
                "inst_consecutive": inst_consec,
                "is_double_buy": foreign_net > 0 and inst_net > 0,
                "foreign_cap_ratio": round(f_ratio, 3) if f_ratio is not None else None,
                "inst_cap_ratio": round(i_ratio, 3) if i_ratio is not None else None,
            }
        }

    def screen_candidates(
        self,
        foreign_ranking: list[dict],
        inst_ranking: list[dict],
        top_n: int = 30
    ) -> list[str]:
        """
        외국인/기관 순매수 랭킹에서 후보 티커 추출
        - 두 리스트에 동시 등장한 종목 우선
        - 단독 등장 종목도 포함
        """
        foreign_tickers = {s["ticker"]: i for i, s in enumerate(foreign_ranking)}
        inst_tickers = {s["ticker"]: i for i, s in enumerate(inst_ranking)}

        # 동반 매수 종목
        double_buy = []
        for ticker in foreign_tickers:
            if ticker in inst_tickers:
                rank_score = (len(foreign_ranking) - foreign_tickers[ticker]) + \
                             (len(inst_ranking) - inst_tickers[ticker])
                double_buy.append((ticker, rank_score))

        double_buy.sort(key=lambda x: -x[1])
        result = [t for t, _ in double_buy[:top_n // 2]]

        # 외국인 단독
        for ticker in foreign_tickers:
            if ticker not in result and len(result) < top_n * 2 // 3:
                result.append(ticker)

        # 기관 단독
        for ticker in inst_tickers:
            if ticker not in result and len(result) < top_n:
                result.append(ticker)

        return result[:top_n]
