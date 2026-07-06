import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient

kis = KISClient()
print("토큰 발급...")
token = kis.get_access_token()
print(f"토큰 OK")

print("\n거래량 조회 시도...")
try:
    result = kis.get_volume_ranking(market="J", top_n=5)
    print(f"결과: {len(result)}개")
    for r in result:
        print(f"  {r}")
except Exception as e:
    print(f"오류: {e}")
