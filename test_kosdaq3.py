import sys, requests, os
sys.path.insert(0, '/app')
from dotenv import load_dotenv
load_dotenv()

r = requests.post("https://openapi.koreainvestment.com:9443/oauth2/tokenP",
    json={"grant_type":"client_credentials",
          "appkey":os.getenv("KIS_APP_KEY"),
          "appsecret":os.getenv("KIS_APP_SECRET")}, timeout=10)
token = r.json()["access_token"]

h = {"Content-Type":"application/json; charset=utf-8",
     "authorization":f"Bearer {token}",
     "appkey":os.getenv("KIS_APP_KEY"),
     "appsecret":os.getenv("KIS_APP_SECRET"),
     "tr_id":"FHPST01710000","custtype":"P"}

# J로 조회 후 코스닥 종목(티커 0으로 시작) 포함 여부 확인
p = {"fid_cond_mrkt_div_code":"J","fid_cond_scr_div_code":"20171",
     "fid_input_iscd":"0000","fid_div_cls_code":"0","fid_blng_cls_code":"0",
     "fid_trgt_cls_code":"111111111","fid_trgt_exls_cls_code":"000000",
     "fid_input_price_1":"1000","fid_input_price_2":"",
     "fid_vol_cnt":"0","fid_input_date_1":""}

res = requests.get(
    "https://openapi.koreainvestment.com:9443/uapi/domestic-stock/v1/quotations/volume-rank",
    headers=h, params=p, timeout=10)

rows = res.json().get("output", [])
print(f"전체: {len(rows)}개")

kospi = [r for r in rows if not r.get('mksc_shrn_iscd','').startswith('0')]
kosdaq = [r for r in rows if r.get('mksc_shrn_iscd','').startswith('0')]
print(f"코스피 계열: {len(kospi)}개")
print(f"코스닥 계열(0으로 시작): {len(kosdaq)}개")
print("\n전체 목록:")
for r in rows:
    print(f"  {r.get('mksc_shrn_iscd','')} {r.get('hts_kor_isnm','')}")
