import sys, requests, os
sys.path.insert(0, '/app')
from dotenv import load_dotenv
load_dotenv()
from src.api.kis_client import KISClient

kis = KISClient()
# 새 토큰 발급 안 하고 기존 싱글톤 토큰 재사용
if not KISClient._access_token:
    print("토큰 없음 - 내일 테스트 필요")
    sys.exit(0)

token = KISClient._access_token
print(f"기존 토큰 재사용")

def test(label, tr_id, path, params):
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": kis.app_key,
        "appsecret": kis.app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    r = requests.get(
        f"https://openapi.koreainvestment.com:9443{path}",
        headers=headers, params=params, timeout=10
    )
    body = r.json()
    output = body.get("output", body.get("output1", {}))
    if isinstance(output, list):
        output = output[0] if output else {}
    price = output.get("bstp_nmix_prpr", output.get("stck_prpr", "N/A"))
    change = output.get("bstp_nmix_prdy_ctrt", output.get("prdy_ctrt", "N/A"))
    msg = body.get("msg1", "")[:50]
    print(f"[{label}] HTTP={r.status_code} price={price} change={change} msg={msg}")

test("코스피", "FHKUP03500100",
    "/uapi/domestic-stock/v1/quotations/inquire-index-price",
    {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001"})

test("코스닥", "FHKUP03500100",
    "/uapi/domestic-stock/v1/quotations/inquire-index-price",
    {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "1001"})
