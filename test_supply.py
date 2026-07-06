import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient

kis = KISClient()

# 삼성전자로 수급 테스트
ticker = "005930"
print(f"=== {ticker} 투자자별 매매동향 ===")

data = kis.get_investor_trend(ticker, days=5)
print(f"외국인 5일 순매수: {data['foreign_net']:+,}주")
print(f"기관 5일 순매수:   {data['inst_net']:+,}주")
print(f"개인 5일 순매수:   {data['indiv_net']:+,}주")
print(f"외국인 연속 매수:  {data['foreign_consecutive']}일")
print(f"기관 연속 매수:    {data['inst_consecutive']}일")

print("\n[일별 상세]")
for d in data['detail'][:5]:
    print(f"  {d['date']}: 외국인 {d['foreign']:+,} / 기관 {d['inst']:+,} / 개인 {d['indiv']:+,}")
