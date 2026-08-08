"""MIN_MARKET_BREADTH 임계값 스윕 (일회성 검증 스크립트)

일봉/수급 데이터는 한 번만 받아서 재사용하고, 임계값만 바꿔가며
_simulate_trades 를 반복 호출한다. (매번 백테스트를 새로 돌리면
KIS 토큰 발급 제한에 걸리고 API 호출도 4배가 된다)
"""
import os
import sys
import argparse

sys.path.insert(0, "/app")

from loguru import logger

from src.backtest.engine import BacktestEngine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--top", type=int, default=60)
    ap.add_argument("--levels", default="40,45,50,55")
    args = ap.parse_args()

    from src.utils.stock_db import get_db
    db = get_db()
    db.load()
    tickers = [t for t in db._ticker_to_name.keys()
               if t.isdigit() and len(t) == 6 and not t.startswith("1")]
    universe = tickers[:args.top]

    engine = BacktestEngine(args.start, args.end, universe=universe)
    logger.info("[1/2] 데이터 수집 (1회만)...")
    price_data = engine._fetch_all_data()
    logger.info(f"  → {len(price_data)}종목")

    rows = []
    for lvl in [int(x) for x in args.levels.split(",")]:
        os.environ["MIN_MARKET_BREADTH"] = str(lvl)
        engine._filter_stats = {}
        engine._score_pass = 0
        engine._breadth_series = []
        logger.info(f"\n===== MIN_MARKET_BREADTH={lvl} =====")
        trades = engine._simulate_trades(price_data)
        m = engine._calculate_metrics(trades)
        rows.append((lvl, m, trades))

    print("\n" + "=" * 92)
    print(f"브레드스 임계값 스윕: {args.start}~{args.end} / 유니버스 {len(price_data)}종목")
    print("=" * 92)
    print(f"{'임계값':>6} {'거래':>5} {'승률':>7} {'평균':>8} {'누적':>9} "
          f"{'PF':>6} {'샤프':>7} {'MDD':>8}  청산사유")
    print("-" * 92)
    for lvl, m, trades in rows:
        if m.get("error"):
            print(f"{lvl:>5}% {0:>5} {'-':>7} {'-':>8} {'-':>9} {'-':>6} {'-':>7} {'-':>8}  거래없음")
            continue
        print(f"{lvl:>5}% {m['total_trades']:>5} {m['win_rate_pct']:>6.1f}% "
              f"{m['avg_return_pct']:>7.2f}% {m['total_return_pct']:>8.2f}% "
              f"{m['profit_factor']:>6.2f} {m['sharpe_ratio']:>7.2f} "
              f"{m['max_drawdown_pct']:>7.2f}%  {m['exit_reasons']}")
    print("=" * 92)

    for lvl, m, trades in rows:
        if not trades:
            continue
        print(f"\n[{lvl}%] 거래 상세")
        for t in trades:
            print(f"  {t['entry_date']} {t['ticker']} score={t['score']:.1f} "
                  f"ATR={t.get('atr_pct', 0):.1f}% → {t['return_pct']:+.2f}% "
                  f"({t['exit_reason']}, {t.get('holding_days', 0)}일)")


if __name__ == "__main__":
    main()
