import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient

kis = KISClient()

# 다양한 지수 코드 테스트
for code in ["0001", "1001", "U001", "U201"]:
    try:
        data = kis.get_stock_price(code)
        print(f"{code} → price={data['price']} change={data['change_rate']}")
    except Exception as e:
        print(f"{code} → 오류: {e}")
