"""A-share IPO tracker, daily source snapshots via AKShare/Eastmoney."""
import csv, json, re, sys, logging
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import akshare as ak

DATA=Path("data"); WEB=Path("dashboard")
FIELDS=["id","category","status","company_name","stock_name","stock_code","exchange","board","industry","province","sponsor","audit_status","accept_date","audit_update_date","subscribe_date","list_date","issue_price_cny","funds_raised_100m_cny","prospectus_url","first_seen","last_seen"]
EVENTS=["observed_on","id","company_name","event","old_value","new_value"]
TODAY=datetime.now(ZoneInfo("Asia/Shanghai")).date()
ASOF=TODAY.isoformat()
CUTOFF=(TODAY-timedelta(days=730)).isoformat()
TERM=("终止","撤回","不予注册","未通过","否决","不予核准","取消审核")
ISSUE_URL="https://data.eastmoney.com/xg/xg/default_2.html"
AUDIT_URL="https://data.eastmoney.com/xg/ipo/"

def val(x):
    s=str(x if x is not None else "").strip()
    return "" if s.lower() in ("nan","nat","none","null","--","-","<na>") else s

def dt(x):
    m=re.search(r"(\d{4})[-/.]?(\d{1,2})[-/.]?(\d{1,2})",val(x))
    if not m:return ""
    try:return date(*map(int,m.groups())).isoformat()
    except ValueError:return ""

def code(x):
    m=re.search(r"(?<!\d)(\d{6})(?!\d)",val(x))
    if m:return m.group(1)
    s=val(x)
    return str(int(float(s))).zfill(6) if re.fullmatch(r"\d{1,6}(?:\.0)?",s) else ""

def norm(x):
    s=val(x).replace("（","(").replace("）",")")
    s=re.sub(r"\([^)]*\)","",s)
    s=re.sub(r"(股份有限公司|有限责任公司|有限公司|股份公司)$","",s)
    return re.sub(r"[^\w\u4e00-\u9fff]","",s).lower()

def get_board(x):
    s=val(x)
    if "北交" in s:return "北交所","北交所"
    if "科创" in s:return "上交所","科创板"
    if "创业" in s:return "深交所","创业板"
    if "上海" in s or "沪" in s:return "上交所","沪市主板"
    if "深圳" in s or "深" in s:return "深交所","深市主板"
    return "未知","未知"

def issue_board(c):
    if c.startswith(("920","8","4")):return "北交所","北交所"
    if c.startswith(("688","689")):return "上交所","科创板"
    if c.startswith(("300","301")):return "深交所","创业板"
    if c.startswith(("0","2")):return "深交所","深市主板"
    if c.startswith("6"):return "上交所","沪市主板"
    return "未知","未知"

def read_csv(p):
    if not p.exists():return []
    with p.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def write_csv(p, rows, fields):
    p.parent.mkdir(parents=True,exist_ok=True)
    tmp=p.with_suffix(".tmp")
    with tmp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction="ignore");w.writeheader();w.writerows(rows)
    tmp.replace(p)

def classify(r):
    ld=r.get("list_date","");sub=r.get("subscribe_date","")
    a=r.get("audit_status",""); upd=r.get("audit_update_date","")
    if ld and ld<=ASOF:return "已上市","已上市"
    if ld:return "待上市","已排期"
    if sub:
        if sub < (TODAY-timedelta(days=180)).isoformat():return "待核验","申购日期过旧"
        return "待发行/上市","已进入发行流程"
    if any(t in a for t in TERM):return "已终止",a
    if "中止" in a or "暂停" in a:return "审核中止",a
    if a and (not upd or upd<(TODAY-timedelta(days=365)).isoformat()):return "待核验","审核信息过旧"
    if a:return "审核中",a
    return "待核验","未取得审核状态"

def main():
    DATA.mkdir(exist_ok=True);WEB.mkdir(exist_ok=True)
    old={r["id"]:r for r in read_csv(DATA/"ipo_master.csv")}
    source_ok={};errs={};issue=[];audits=[]
    for key,fn in (("issuance",lambda:ak.stock_xgsglb_em(symbol="全部股票")),
                   ("audit",ak.stock_register_all_em)):
        try:
            rows=fn().to_dict(orient="records")
            if len(rows)<100:raise RuntimeError(f"Only {len(rows)} records received; refusing overwrite")
            source_ok[key]=True
            if key=="issuance":issue=rows
            else:audits=rows
            print(key,len(rows),flush=True)
        except Exception as e:
            source_ok[key]=False;errs[key]=str(e)
            logging.exception("Source %s failed",key)
    if not any(source_ok.values()):raise RuntimeError("Both data sources failed: "+str(errs))
    items={}
    for raw in audits:
        name=val(raw.get("企业名称",raw.get("发行人全称","")))
        if not name:continue
        exch,board=get_board(raw.get("拟上市地点",raw.get("拟上市地","")))
        a=val(raw.get("最新状态",raw.get("审核状态","")))
        update=dt(raw.get("更新日期"))
        rid="APP:"+board+":"+norm(name)
        if (any(t in a for t in TERM) or not update or update<(TODAY-timedelta(days=730)).isoformat()) and rid not in old:continue
        r={"id":rid,"company_name":name,"exchange":exch,"board":board,
           "audit_status":a,"industry":val(raw.get("行业",raw.get("证监会行业",""))),
           "province":val(raw.get("注册地")),"sponsor":val(raw.get("保荐机构")),
           "accept_date":dt(raw.get("受理日期")),"audit_update_date":update,
           "prospectus_url":val(raw.get("招股说明书"))}
        if rid not in items or update>=items[rid].get("audit_update_date",""):items[rid]=r
    for raw in issue:
        c=code(raw.get("股票代码",raw.get("代码","")))
        if not c:continue
        listing=dt(raw.get("上市日期"));sub=dt(raw.get("申购日期",raw.get("申购日")))
        if listing and listing<CUTOFF:continue
        exch,board=issue_board(c);name=val(raw.get("股票简称",raw.get("简称","")))
        price=val(raw.get("发行价格"));shares=val(raw.get("发行总数"));funds=""
        try:
            p=float(price);q=float(shares)
            if p>0 and q>0:funds=round(p*q/1e8,4)
        except (TypeError,ValueError):pass
        candidates=[k for k,v in items.items() if v["board"]==board and len(norm(name))>=3 and norm(name) in norm(v["company_name"])]
        matched=items.pop(candidates[0]) if len(candidates)==1 else {}
        rid="STOCK:"+c
        r={**matched,"id":rid,"stock_name":name,"stock_code":c,"exchange":exch,"board":board,
           "subscribe_date":sub,"list_date":listing,"issue_price_cny":price,
           "funds_raised_100m_cny":funds}
        r["company_name"]=r.get("company_name") or name
        items[rid]=r
    # Never delete an earlier record just because a source temporarily omits it.
    for rid,r in old.items():
        if rid in items:continue
        if rid.startswith("STOCK:") and r.get("list_date") and r["list_date"]<CUTOFF:continue
        if rid.startswith("APP:") and any(norm(r.get("company_name"))==norm(v.get("company_name")) for v in items.values() if v.get("stock_code")):continue
        items[rid]=dict(r)
    prev_events=read_csv(DATA/"ipo_events.csv")
    event_keys={(r["observed_on"],r["id"],r["event"],r["new_value"]) for r in prev_events}
    out=[];new_events=[]
    for rid,r in items.items():
        prev=old.get(rid,{})
        for field in FIELDS:
            if field in ("category","status","last_seen"):continue
            if val(r.get(field))=="" and val(prev.get(field))!="":r[field]=prev[field]
        cat,status=classify(r)
        r["category"]=cat;r["status"]=status
        r["first_seen"]=prev.get("first_seen") or ASOF
        r["last_seen"]=ASOF if (rid.startswith("STOCK:") and source_ok["issuance"] or rid.startswith("APP:") and source_ok["audit"]) else prev.get("last_seen",ASOF)
        checks=[("首次发现","",cat+" / "+status)] if not prev else [(f+"变化",val(prev.get(f)),val(r.get(f))) for f in ("category","status","audit_status","list_date","subscribe_date") if val(prev.get(f))!=val(r.get(f))]
        for ev,oldv,newv in checks:
            e={"observed_on":ASOF,"id":rid,"company_name":r["company_name"],"event":ev,"old_value":oldv,"new_value":newv}
            key=(ASOF,rid,ev,newv)
            if key not in event_keys:new_events.append(e);event_keys.add(key)
        out.append({f:r.get(f,"") for f in FIELDS})
    priority={"审核中":0,"待发行/上市":1,"待上市":2,"审核中止":3,"待核验":4,"已上市":5,"已终止":6}
    out.sort(key=lambda r:(priority.get(r["category"],9),r.get("company_name","")))
    events=prev_events+new_events
    summary={"as_of":ASOF,"scope_start":CUTOFF,"total":len(out),"counts":dict(Counter(r["category"] for r in out)),
             "source_ok":source_ok,"source_errors":errs,"new_events_today":sum(e["observed_on"]==ASOF for e in events),
             "sources":{"issuance":ISSUE_URL,"audit":AUDIT_URL}}
    write_csv(DATA/"ipo_master.csv",out,FIELDS)
    write_csv(DATA/"ipo_events.csv",events,EVENTS)
    (DATA/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    (WEB/"ipo_data.json").write_text(json.dumps({"summary":summary,"rows":out,"events":events[-300:]},ensure_ascii=False,default=str),encoding="utf-8")
    print("IPO rows:",len(out),"new events:",len(new_events),flush=True)
    if errs:sys.exit(2)

if __name__=="__main__":
    main()
