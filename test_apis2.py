import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient
import requests, os
from dotenv import load_dotenv

load_dotenv()

# 토큰 직접 발급 (403 나면 내일 다시 시도)
url = "https://openapi.koreainvestment.com:9443/oauth2/tokenP"
payload = {
    "grant_type": "client_credentials",
    "appkey": os.getenv("KIS_APP_KEY"),
    "appsecret": os.getenv("KIS_APP_SECRET"),
}
r = requests.post(url, json=payload, timeout=10)
print(f"토큰발급: {r.status_code} {r.text[:200]}")

if r.status_code != 200:
    print("토큰 발급 실패 - 한투 실서버는 하루 1회만 발급 가능")
    print("내일 장 시작 후 다시 테스트하거나, 컨테이너 재시작 후 바로 실행하세요")
    sys.exit(1)

token = r.json()["access_token"]
app_key = os.getenv("KIS_APP_KEY")
app_secret = os.getenv("KIS_APP_SECRET")

def test(label, tr_id, path, params):
    url = f"https://openapi.koreainvestment.com:9443{path}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": app_key,
        "appsecret": app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    r = requests.get(url, headers=headers, params=params, timeout=10)
    try:
        body = r.json()
        output = body.get("output", [])
        rows = output if isinstance(output, list) else [output]
        rt_cd = body.get("rt_cd", "?")
        msg = body.get("msg1", "")[:80]
    except:
        rows = []
        rt_cd = "?"
        msg = r.text[:100]
    print(f"[{label}] HTTP={r.status_code} rt_cd={rt_cd} rows={len(rows)} msg={msg}")
    if rows and len(rows) > 0 and isinstance(rows[0], dict):
        row = rows[0]
        ticker = row.get("mksc_shrn_iscd", row.get("stck_shrn_iscd", "N/A"))
        name = row.get("hts_kor_isnm", "N/A")
        print(f"  샘플: {ticker} {name}")
    print()

print("\n=== 외국인 순매수 랭킹 ===")
test("외국인_FHKST04030200", "FHKST04030200",
    "/uapi/domestic-stock/v1/ranking/foreigners-net-buy",
    {"fid_cond_mrkt_div_code":"J","fid_input_iscd":"0000",
     "fid_input_date_1":"","fid_trgt_cls_code":"0",
     "fid_trgt_exls_cls_code":"0","fid_input_price_1":"",
     "fid_input_price_2":"","fid_vol_cnt":""})

test("외국인_FHKST04030100", "FHKST04030100",
    "/uapi/domestic-stock/v1/ranking/foreigners-net-buy",
    {"fid_cond_mrkt_div_code":"J","fid_input_iscd":"0000",
     "fid_input_date_1":"","fid_trgt_cls_code":"0",
     "fid_trgt_exls_cls_code":"0","fid_input_price_1":"",
     "fid_input_price_2":"","fid_vol_cnt":""})

print("=== 거래량 순위 ===")
test("거래량_volume-rank", "FHPST01710000",
    "/uapi/domestic-stock/v1/quotations/volume-rank",
    {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"20171",
     "fid_input_iscd":"0000","fid_div_cls_code":"0","fid_blng_cls_code":"0",
     "fid_trgt_cls_code":"111111111","fid_trgt_exls_cls_code":"000000",
     "fid_input_price_1":"1000","fid_input_price_2":"",
     "fid_vol_cnt":"100000","fid_input_date_1":""})

test("거래량_ranking_volume", "FHPST01710000",
    "/uapi/domestic-stock/v1/ranking/volume",
    {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"20171",
     "fid_input_iscd":"0000","fid_div_cls_code":"0","fid_blng_cls_code":"0",
     "fid_trgt_cls_code":"111111111","fid_trgt_exls_cls_code":"000000",
     "fid_input_price_1":"1000","fid_input_price_2":"",
     "fid_vol_cnt":"100000","fid_input_date_1":""})
