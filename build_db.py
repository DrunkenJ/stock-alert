import sys
sys.path.insert(0, '/app')
from src.utils.stock_db import get_db
db = get_db()
db.rebuild()
print(f"완료: {db.total_count()}개 종목")
