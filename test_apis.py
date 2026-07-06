import sys
sys.path.insert(0, '/app')
from src.api.kis_client import KISClient
import requests

kis = KISClient()
token = kis.get_access_token()

def test(label, tr_id, path, params):
    url = f"https://openapi.koreainvestment.com:9443{path}"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": kis.app_key,
        "appsecret": kis.app_secret,
        "tr_id": tr_id,
        "custtype": "P",
    }
    r = requests.get(url, headers=headers, params=params, timeout=10)
    rows = r.json().get("output", []) if r.status_code == 200 else []
    rt_cd = r.json().get("rt_cd", "?") if r.status_code == 200 else "?"
    msg = r.json().get("msg1", "")[:60] if r.status_code == 200 else r.text[:80]
    print(f"[{label}] {r.status_code} rt_cd={rt_cd} rows={len(rows)} msg={msg}")
    if rows:
        print(f"  샘플: {rows[0].get('mksc_shrn_iscd','')} {rows[0].get('hts_kor_isnm','')}")

print("=== 외국인 순매수 랭킹 ===")
test("외국인_22484", "FHPTJ04400000",
    "/uapi/domestic-stock/v1/ranking/foreigners-buying",
    {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"22484",
     "fid_input_iscd":"0000","fid_trgt_cls_code":"111111111",
     "fid_trgt_exls_cls_code":"000000","fid_input_price_1":"1000",
     "fid_input_price_2":"","fid_vol_cnt":"50000","fid_input_date_1":"",
     "fid_rank_sort_cls_code":"0","fid_blng_cls_code":"0"})

test("외국인_20533", "FHPTJ04400000",
    "/uapi/domestic-stock/v1/ranking/foreigners-buying",
    {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"20533",
     "fid_input_iscd":"0000","fid_trgt_cls_code":"111111111",
     "fid_trgt_exls_cls_code":"000000","fid_input_price_1":"1000",
     "fid_input_price_2":"","fid_vol_cnt":"50000","fid_input_date_1":"",
     "fid_rank_sort_cls_code":"0","fid_blng_cls_code":"0"})

print("\n=== 거래량 순위 (vol_cnt 변형) ===")
for vol in ["0", "100", "10000", "100000"]:
    test(f"거래량_vol={vol}", "FHPST01710000",
        "/uapi/domestic-stock/v1/ranking/volume",
        {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"20171",
         "fid_input_iscd":"0000","fid_div_cls_code":"0","fid_blng_cls_code":"0",
         "fid_trgt_cls_code":"111111111","fid_trgt_exls_cls_code":"000000",
         "fid_input_price_1":"1000","fid_input_price_2":"",
         "fid_vol_cnt":vol,"fid_input_date_1":""})
