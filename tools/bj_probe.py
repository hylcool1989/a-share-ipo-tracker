import akshare as ak
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
print("Testing BJ source alternatives",flush=True)
def one(name,fn):
    try:
        a=fn()
        if hasattr(a,"columns"):
            return (name,list(a.columns),a.head(3).to_json(orient="records",force_ascii=False)[:1300],len(a))
        return (name,str(a)[:900])
    except Exception as e:
        return (name,type(e).__name__,str(e)[:500])
tests=[
("BJ-only Eastmoney",lambda:ak.stock_bj_a_spot_em()),
("Individual BJ 920060",lambda:ak.stock_individual_info_em(symbol="920060")),
("Snowball 920060",lambda:ak.stock_individual_spot_xq(symbol="BJ920060")),
("Tencent BJ",lambda:requests.get("https://qt.gtimg.cn/q=bj920060,bj920112,bj920165",timeout=10,headers={"User-Agent":"Mozilla/5.0"}).text[:1000]),
("Sina BJ",lambda:requests.get("https://hq.sinajs.cn/list=bj920060",timeout=10,headers={"Referer":"https://finance.sina.com.cn/","User-Agent":"Mozilla/5.0"}).text[:800])]
with ThreadPoolExecutor(max_workers=5) as ex:
    fs=[ex.submit(one,n,f) for n,f in tests]
    for fut in as_completed(fs): print(fut.result(),flush=True)
