import sys
sys.path.insert(0, '/app')
from src.analyzers.screener import StockScreener
from src.notifier.discord import DiscordNotifier

print("스크리닝 시작...")
s = StockScreener()
picks = s.run()
print(f"선정 종목: {[p['name'] for p in picks]}")

if picks:
    from src.analyzers.ai_evaluator import AIEvaluator
    ai = AIEvaluator()
    summary = ai.generate_market_summary(picks, "테스트")
    n = DiscordNotifier()
    n.send_morning_picks(picks, summary)
    print("Discord 전송 완료!")
else:
    print("선정 종목 없음")
