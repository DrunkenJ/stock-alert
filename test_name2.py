import sys, requests, os
sys.path.insert(0, '/app')
from dotenv import load_dotenv
load_dotenv()
from src.api.kis_client import KISClient

kis = KISClient()
# 원본 응답 필드 전체 출력
import requests
url = "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/inquire-price"
headers = {
    "Content-Type": "application/json; charset=utf-8",
    "authorization": f"Bearer {kis.get_access_token()}",
    "appkey": kis.app_key,
    "appsecret": kis.app_secret,
    "tr_id": "FHKST01010100",
    "custtype": "P",
}
params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": "005930"}
r = requests.get(url, headers=headers, params=params, timeout=10)
output = r.json().get("output", {})
# 이름 관련 필드만 출력
for k, v in output.items():
    if v and any(x in k.lower() for x in ['nm', 'name', 'isnm', 'kor']):
        print(f"  {k}: {v}")
print("\n전체 필드:")
for k, v in output.items():
    if v:
        print(f"  {k}: {v}")
