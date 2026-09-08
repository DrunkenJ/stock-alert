"""
3단계 준비용 상세 데이터 수집기
- 실패/성공 종목의 모든 지표를 자동 기록
- 거시 환경 상관관계 추적
- 시간대별 진입 효과 측정
- 3주 후 충분한 데이터 쌓이면 알람 발송

저장 파일: data/detailed_trades.json
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger


DETAILED_FILE = Path("data/detailed_trades.json")
ALERT_STATE_FILE = Path("data/stage3_alert_state.json")
STAGE3_THRESHOLD = 30  # 3단계 분석에 필요한 최소 거래 수


def record_detailed_trade(pick: dict, candles: list = None,
                          investor: dict = None,
                          macro_data: dict = None,
                          regime: dict = None,
                          entry_time: str = None):
    """
    추천 시점의 상세 데이터를 모두 기록
    나중에 패턴 분석에 사용
    """
    try:
        data = _load()
        ticker = pick.get("ticker", "")
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{today}_{ticker}"

        # 기본 정보
        record = {
            "ticker":            ticker,
            "name":              pick.get("name", ""),
            "entry_date":        today,
            "entry_time":        entry_time or datetime.now().strftime("%H:%M"),
            # 추천 시점의 '현재가'다. trade_history.json 쪽 entry_price 는
            # 1차 분할매수 지정가라 값이 다르다. 둘을 대조할 수 있게 함께 남긴다.
            "entry_price":       pick.get("price", 0),
            "planned_entry":     pick.get("entry_strategy", {}).get("split_1st"),
            "sector":            pick.get("sector", ""),
            "market":            pick.get("market", ""),
            "market_cap":        pick.get("market_cap", 0),

            # 점수 정보
            "final_score":       pick.get("final_score", 0),
            "tech_score":        pick.get("tech_score", 0),
            "supply_score":      pick.get("supply_score", 0),
            "ai_score":          pick.get("ai_eval", {}).get("ai_score", 0),
            "score_confidence":  pick.get("score_confidence", ""),

            # 기술적 지표
            "rsi":               pick.get("indicators", {}).get("rsi", None),
            "vol_ratio":         pick.get("indicators", {}).get("vol_ratio", None),
            "change_rate":       pick.get("change_rate", 0),
            "open_price":        pick.get("open_price", 0),
            # 진입 품질 지표 (학습/사후분석용)
            "atr_pct":           pick.get("indicators", {}).get("atr_pct", None),
            "atr_ratio":         pick.get("indicators", {}).get("atr_ratio", None),
            "disparity20":       pick.get("indicators", {}).get("disparity20", None),
            "ret20":             pick.get("indicators", {}).get("ret20", None),
            "rs":                pick.get("indicators", {}).get("rs", None),

            # 수급 정보
            "foreign_net":       pick.get("supply_summary", {}).get("foreign_net", 0),
            "inst_net":          pick.get("supply_summary", {}).get("inst_net", 0),
            "foreign_cap_ratio": pick.get("supply_summary", {}).get("foreign_cap_ratio", None),
            "inst_cap_ratio":    pick.get("supply_summary", {}).get("inst_cap_ratio", None),
            "supply_pattern":    pick.get("supply_pattern", {}).get("pattern", ""),

            # 뉴스 감성
            "news_sentiment":    pick.get("news_sentiment", {}).get("sentiment", "neutral"),
            "news_score_adj":    pick.get("news_sentiment", {}).get("score_adj", 0),

            # AI 평가 상세
            "ai_recommendation": pick.get("ai_eval", {}).get("recommendation", ""),
            "ai_consensus":      pick.get("ai_eval", {}).get("consensus", ""),
            "risk_level":        pick.get("ai_eval", {}).get("_risk", {}).get("risk_level", ""),
        }

        # 시장 국면
        if regime:
            record["regime"] = regime.get("regime", "")
            record["regime_score"] = regime.get("regime_score", 0)

        # 거시 환경
        if macro_data:
            record["macro_judgment"] = macro_data.get("judgment", "")
            mkt = macro_data.get("market_data", {})
            record["vix"]       = mkt.get("VIX", {}).get("value", None)
            record["usdkrw"]    = mkt.get("USDKRW", {}).get("value", None)
            record["nasdaq_chg"] = mkt.get("NASDAQ", {}).get("change_pct", None)

        # 캔들 기반 추가 지표 (3일 룰, 52주 고점 위치 등)
        if candles and len(candles) >= 60:
            closes = [c["close"] for c in candles]
            highs  = [c["high"]  for c in candles]
            cur    = closes[-1]

            # 52주 고점 대비 위치
            high_52w = max(highs[-252:]) if len(highs) >= 252 else max(highs)
            record["pos_52w_pct"] = round(cur / high_52w * 100, 2) if high_52w else 0

            # 최근 5일 누적 상승률
            if len(closes) >= 6:
                record["change_5d_pct"] = round((cur - closes[-6]) / closes[-6] * 100, 2)

            # MA20 대비
            ma20 = sum(closes[-20:]) / 20
            record["above_ma20_pct"] = round((cur - ma20) / ma20 * 100, 2)

        data[key] = record
        _save(data)
        logger.debug(f"상세 데이터 기록: {ticker}")

    except Exception as e:
        logger.debug(f"상세 데이터 기록 오류: {e}")


def update_trade_outcome(ticker: str, entry_date: str,
                        final_pct: float, close_reason: str,
                        events: list = None):
    """청산 시 결과 업데이트"""
    try:
        data = _load()
        key = f"{entry_date}_{ticker}"

        if key in data:
            data[key]["final_pct"] = round(final_pct, 2)
            data[key]["close_reason"] = close_reason
            data[key]["close_date"] = datetime.now().strftime("%Y-%m-%d")
            data[key]["is_win"] = final_pct > 0
            if events:
                data[key]["events"] = events
            _save(data)
            logger.debug(f"상세 데이터 청산 업데이트: {ticker}")

    except Exception as e:
        logger.debug(f"청산 업데이트 오류: {e}")


def get_data_count() -> dict:
    """현재 누적된 데이터 통계"""
    data = _load()
    total = len(data)
    closed = sum(1 for v in data.values() if "final_pct" in v)
    return {
        "total":  total,
        "closed": closed,
        "active": total - closed,
    }


def check_stage3_readiness():
    """
    3주 후 3단계 분석 준비 알람
    매주 금요일 주간 리뷰 시 자동 호출
    """
    state = _load_state()
    stats = get_data_count()
    closed_count = stats["closed"]

    # 이미 알림 보낸 경우 스킵
    if state.get("notified", False):
        return None

    # 3단계 본격 분석 가능 시점
    if closed_count >= STAGE3_THRESHOLD:
        state["notified"]    = True
        state["notified_at"] = datetime.now().strftime("%Y-%m-%d")
        state["data_count"]  = closed_count
        _save_state(state)

        return {
            "trigger": True,
            "closed_count": closed_count,
            "message": (
                f"🎯 **3단계 분석 준비 완료!**\n\n"
                f"누적된 청산 데이터: **{closed_count}건**\n"
                f"이제 진짜 패턴 분석이 가능한 시점이에요.\n\n"
                f"**다음 작업 권장:**\n"
                f"• 실패 종목 공통 패턴 분석\n"
                f"• 종목별 시간대 패턴 학습\n"
                f"• 거시 환경 상관관계 분석\n"
                f"• Walk-Forward 검증\n\n"
                f"💡 Claude에게 \"3단계 분석 진행해줘\"라고 요청하시면\n"
                f"   실제 데이터 기반 정확한 패턴 분석을 진행합니다."
            ),
        }

    # 중간 진행 상황 알림 (15건 도달 시 한 번)
    if closed_count >= 15 and not state.get("mid_notified", False):
        state["mid_notified"] = True
        state["mid_notified_at"] = datetime.now().strftime("%Y-%m-%d")
        _save_state(state)

        return {
            "trigger": False,
            "closed_count": closed_count,
            "message": (
                f"📊 **데이터 누적 진행 중**\n\n"
                f"청산 데이터: **{closed_count}건** / 30건 목표\n"
                f"3단계 본격 분석까지 약 {30 - closed_count}건 더 필요해요.\n"
                f"현재 학습된 규칙은 `!학습규칙` 으로 확인 가능합니다."
            ),
        }

    return None


def _load() -> dict:
    if not DETAILED_FILE.exists():
        return {}
    try:
        with open(DETAILED_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    DETAILED_FILE.parent.mkdir(exist_ok=True)
    with open(DETAILED_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def _load_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    ALERT_STATE_FILE.parent.mkdir(exist_ok=True)
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
