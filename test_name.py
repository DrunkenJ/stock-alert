import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient

kis = KISClient()
for ticker in ['005930', '000660', '003280']:
    data = kis.get_stock_price(ticker)
    print(f"{ticker} → name='{data['name']}' price={data['price']}")
