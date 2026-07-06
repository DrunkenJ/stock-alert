"""
백테스트 실행 스크립트

사용법:
  # 1. NAS에서 직접 실행 (컨테이너 안)
  docker exec stock-alert-bot python run_backtest.py --start 20260101 --end 20260410

  # 2. 유니버스 제한 (빠른 테스트)
  docker exec stock-alert-bot python run_backtest.py --start 20260101 --end 20260410 --top 50
"""
import sys
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from src.backtest.engine import BacktestEngine
from loguru import logger


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="시작일 YYYYMMDD")
    parser.add_argument("--end", required=True, help="종료일 YYYYMMDD")
    parser.add_argument("--top", type=int, default=0, help="유니버스 제한 (거래량 상위 N종목, 0=전체)")
    args = parser.parse_args()

    # 유니버스 구성
    universe = None
    if args.top > 0:
        from src.utils.stock_db import get_db
        db = get_db()
        db.load()
        tickers = [t for t in db._ticker_to_name.keys()
                   if t.isdigit() and len(t) == 6 and not t.startswith("1")]
        universe = tickers[:args.top]
        logger.info(f"제한된 유니버스: {len(universe)}종목")

    engine = BacktestEngine(args.start, args.end, universe=universe)
    result = engine.run()

    # 결과 출력
    metrics = result["metrics"]
    print("\n" + "=" * 60)
    print(f"백테스트 결과: {result['period']}")
    print("=" * 60)
    print(f"유니버스 크기:      {result['universe_size']}종목")
    print(f"총 거래 수:         {metrics.get('total_trades', 0)}건")
    print(f"  수익 거래:        {metrics.get('win_trades', 0)}건")
    print(f"  손실 거래:        {metrics.get('loss_trades', 0)}건")
    print(f"승률:               {metrics.get('win_rate_pct', 0)}%")
    print(f"평균 수익률:        {metrics.get('avg_return_pct', 0)}%")
    print(f"  평균 수익:        {metrics.get('avg_win_pct', 0)}%")
    print(f"  평균 손실:        {metrics.get('avg_loss_pct', 0)}%")
    print(f"누적 수익률:        {metrics.get('total_return_pct', 0)}%")
    print(f"수익비 (PF):        {metrics.get('profit_factor', 0)}")
    print(f"샤프지수:           {metrics.get('sharpe_ratio', 0)}")
    print(f"최대낙폭 (MDD):     {metrics.get('max_drawdown_pct', 0)}%")
    print(f"청산 사유: {metrics.get('exit_reasons', {})}")
    print("=" * 60)

    # 리포트 저장
    engine.save_report(result)
    print(f"\n리포트 저장: data/backtest_{args.start}_{args.end}.json")

    # 평가
    print("\n[퀀트 관점 평가]")
    wr = metrics.get("win_rate_pct", 0)
    pf = metrics.get("profit_factor", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    mdd = abs(metrics.get("max_drawdown_pct", 0))

    score = 0
    if wr >= 55: score += 1
    if pf >= 1.5: score += 1
    if sharpe >= 1.0: score += 1
    if mdd <= 20: score += 1

    if score >= 3:
        print("✅ 전략 유효성 양호 - 실전 운용 검토 가능")
    elif score >= 2:
        print("🟡 전략 개선 필요 - 파라미터 튜닝 권장")
    else:
        print("🔴 전략 재설계 필요 - 현재 상태로 실전 운용 위험")


if __name__ == "__main__":
    main()
