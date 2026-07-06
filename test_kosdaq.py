import sys, requests, os
sys.path.insert(0, '/app')
from dotenv import load_dotenv
load_dotenv()

r = requests.post("https://openapi.koreainvestment.com:9443/oauth2/tokenP",
    json={"grant_type":"client_credentials",
          "appkey":os.getenv("KIS_APP_KEY"),
          "appsecret":os.getenv("KIS_APP_SECRET")}, timeout=10)
token = r.json()["access_token"]

for code in ["J","K","Q","N","D","E","W"]:
    h = {"Content-Type":"application/json; charset=utf-8",
         "authorization":f"Bearer {token}",
         "appkey":os.getenv("KIS_APP_KEY"),
         "appsecret":os.getenv("KIS_APP_SECRET"),
         "tr_id":"FHPST01710000","custtype":"P"}
    p = {"fid_cond_mrkt_div_code":code,"fid_cond_scr_div_code":"20171",
         "fid_input_iscd":"0000","fid_div_cls_code":"0","fid_blng_cls_code":"0",
         "fid_trgt_cls_code":"111111111","fid_trgt_exls_cls_code":"000000",
         "fid_input_price_1":"1000","fid_input_price_2":"",
         "fid_vol_cnt":"100000","fid_input_date_1":""}
    res = requests.get("https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/volume-rank",
        headers=h, params=p, timeout=10)
    body = res.json()
    rows = body.get("output",[])
    msg = body.get("msg1","")[:50]
    sample = f"{rows[0].get('mksc_shrn_iscd','')} {rows[0].get('hts_kor_isnm','')}" if rows else ""
    print(f"[{code}] {res.status_code} rows={len(rows)} msg={msg} {sample}")
