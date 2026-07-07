"""
실시간 급등 감지기
- 5분마다 거래량 + 가격 변화 모니터링
- 초기 급등 신호 감지 시 즉시 알림
- 이미 추천된 종목은 제외

감지 조건:
  - 거래량 직전 5분 대비 +200% 이상
  - 가격 변동 +1~3% (이미 너무 오른 종목 제외)
  - 외국인/기관 매수 진입 흔적
"""
import json
from datetime import datetime
from pathlib import Path
from loguru import logger


SURGE_HISTORY_FILE = Path("data/surge_alerts.json")

# 감지 기준
VOLUME_SURGE_RATIO = 2.0   # 거래량 직전 대비 배수 (2배 이상)
PRICE_MIN_PCT      = 1.0   # 최소 가격 상승률
PRICE_MAX_PCT      = 4.0   # 최대 가격 상승률 (이상은 이미 늦음)
MIN_VOLUME_KRW     = 100_000_000  # 최소 거래대금 (1억원)


class SurgeDetector:
    """실시간 급등 감지기"""

    def __init__(self):
        self.alert_history = self._load_history()

    def detect(self, kis_client, exclude_tickers: set = None) -> list[dict]:
        """
        급등 종목 감지

        Args:
            kis_client: KIS 클라이언트
            exclude_tickers: 이미 추천한 종목 (중복 알림 방지)
        """
        if not kis_client.is_market_open():
            return []

        exclude_tickers = exclude_tickers or set()
        today = datetime.now().strftime("%Y-%m-%d")

        # 오늘 이미 알림 보낸 종목 추가 제외
        already_alerted = {
            t for t, info in self.alert_history.items()
            if info.get("date") == today
        }
        exclude_tickers = exclude_tickers | already_alerted

        # 거래량 상위 30개 가져오기
        try:
            volume_top = kis_client.get_volume_ranking(top_n=30)
        except Exception as e:
            logger.debug(f"거래량 조회 실패: {e}")
            return []

        signals = []
        for stock in volume_top:
            try:
                ticker = stock.get("ticker", "")
                if ticker in exclude_tickers:
                    continue

                # 가격 데이터 확인
                price_data = kis_client.get_stock_price(ticker)
                if not price_data:
                    continue

                price = price_data.get("price", 0)
                change_rate = price_data.get("change_rate", 0)
                volume = price_data.get("volume", 0)

                # 1차 필터: 가격 변동 적정 범위
                if not (PRICE_MIN_PCT <= change_rate <= PRICE_MAX_PCT):
                    continue

                # 2차 필터: 거래대금
                trade_value = price * volume
                if trade_value < MIN_VOLUME_KRW * 100:  # 최소 100억
                    continue

                # 3차 필터: 갭 상승 종목 제외 (시초가 대비 변동)
                open_price = price_data.get("open_price", price)
                intraday_change = (price - open_price) / open_price * 100 if open_price else 0

                # 시초가 대비 1% 이상 상승 = 장중 모멘텀 (좋음)
                if intraday_change < 0.5:
                    continue

                # 일봉 데이터로 거래량 급증 확인
                candles = kis_client.get_daily_ohlcv(ticker, days=10)
                if len(candles) < 5:
                    continue

                avg_vol = sum(c["volume"] for c in candles[-5:-1]) / 4
                today_vol_ratio = volume / avg_vol if avg_vol > 0 else 0

                if today_vol_ratio < VOLUME_SURGE_RATIO:
                    continue

                # 외국인/기관 수급 (당일)
                investor = kis_client.get_investor_trend(ticker, days=3)
                foreign_net = investor.get("foreign_net", 0) if investor else 0
                inst_net = investor.get("inst_net", 0) if investor else 0

                # 신호 등록
                signal = {
                    "ticker": ticker,
                    "name": stock.get("name", ""),
                    "price": price,
                    "change_rate": change_rate,
                    "intraday_change": round(intraday_change, 2),
                    "volume_ratio": round(today_vol_ratio, 1),
                    "trade_value_billion": round(trade_value / 100_000_000, 1),
                    "foreign_net": foreign_net,
                    "inst_net": inst_net,
                    "detected_at": datetime.now().strftime("%H:%M"),
                }

                # 강도 판단
                strength = self._evaluate_strength(signal)
                signal["strength"] = strength

                if strength in ["strong", "medium"]:
                    signals.append(signal)
                    # 알림 기록
                    self.alert_history[ticker] = {
                        "date": today,
                        "time": signal["detected_at"],
                        "price": price,
                        "strength": strength,
                    }

            except Exception as e:
                logger.debug(f"급등 감지 오류 ({stock.get('ticker','')}): {e}")

        if signals:
            self._save_history()
            logger.info(f"급등 감지: {len(signals)}종목")

        return signals

    def _evaluate_strength(self, signal: dict) -> str:
        """급등 신호 강도 평가"""
        score = 0

        # 거래량 급증
        if signal["volume_ratio"] >= 5.0:
            score += 3
        elif signal["volume_ratio"] >= 3.0:
            score += 2
        elif signal["volume_ratio"] >= 2.0:
            score += 1

        # 외국인/기관 매수
        if signal["foreign_net"] > 0:
            score += 1
        if signal["inst_net"] > 0:
            score += 1
        if signal["foreign_net"] > 0 and signal["inst_net"] > 0:
            score += 1  # 동반 매수 보너스

        # 가격 변동 (너무 많이 오르지 않은 게 좋음)
        if 1.5 <= signal["change_rate"] <= 3.0:
            score += 1

        # 거래대금 (큰 종목)
        if signal["trade_value_billion"] >= 100:  # 100억 이상
            score += 1

        if score >= 5:
            return "strong"
        elif score >= 3:
            return "medium"
        else:
            return "weak"

    def _load_history(self) -> dict:
        if not SURGE_HISTORY_FILE.exists():
            return {}
        try:
            with open(SURGE_HISTORY_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_history(self):
        SURGE_HISTORY_FILE.parent.mkdir(exist_ok=True)
        with open(SURGE_HISTORY_FILE, "w") as f:
            json.dump(self.alert_history, f, ensure_ascii=False, indent=2)

    def clear_old(self):
        """오늘 이전 기록 정리"""
        today = datetime.now().strftime("%Y-%m-%d")
        self.alert_history = {
            t: info for t, info in self.alert_history.items()
            if info.get("date") == today
        }
        self._save_history()
