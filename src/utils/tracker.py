"""
수익률 추적 모듈
- 추천 종목 성과 기록
- 누적 수익률 계산
- JSON 파일 기반 영속성
"""
import os
import json
from datetime import datetime
from pathlib import Path
from loguru import logger


DATA_FILE = Path("data/performance.json")


def save_picks(picks: list[dict]):
    """오늘 추천 종목 저장"""
    DATA_FILE.parent.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    history = _load()
    history[today] = {
        "picks": [
            {
                "ticker": p["ticker"],
                "name": p["name"],
                "entry_price": p["price"],
                "target": p.get("ai_eval", {}).get("target_price", 0),
                "stoploss": p.get("ai_eval", {}).get("stop_loss", 0),
                "scores": {
                    "tech": p.get("tech_score", 0),
                    "supply": p.get("supply_score", 0),
                    "ai": p.get("ai_eval", {}).get("ai_score", 0),
                    "final": p.get("final_score", 0),
                },
            }
            for p in picks
        ],
        "results": [],
    }
    _save(history)
    logger.info(f"추천 종목 저장 완료: {today}")


def save_results(results: list[dict]):
    """마감 결과 저장"""
    today = datetime.now().strftime("%Y-%m-%d")
    history = _load()
    if today in history:
        history[today]["results"] = results
        _save(history)


def get_stats() -> dict:
    """누적 통계 계산"""
    history = _load()
    all_returns = []

    for date, data in history.items():
        picks = data.get("picks", [])
        results = data.get("results", [])
        for pick, result in zip(picks, results):
            entry = pick.get("entry_price", 0)
            close = result.get("close_price", entry)
            if entry > 0:
                ret = (close - entry) / entry * 100
                all_returns.append(ret)

    if not all_returns:
        return {"total_days": 0, "avg_return": 0, "win_rate": 0}

    wins = sum(1 for r in all_returns if r > 0)
    return {
        "total_days": len(history),
        "total_picks": len(all_returns),
        "avg_return": sum(all_returns) / len(all_returns),
        "win_rate": wins / len(all_returns) * 100,
        "best": max(all_returns),
        "worst": min(all_returns),
    }


def _load() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def _save(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
