"""Daily end-of-day total market-cap snapshot for listed A-share IPOs.

AKShare Eastmoney full A-share quotes: 总市值 in CNY, divide by 1e8.
Fall back to individual Eastmoney stock info for missing tickers (bounded).
Cache the last verified observation date; NEVER label stale data as live.
"""
import csv
import json
import math
import re
import time
import logging
import requests
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import akshare as ak

ROOT = Path("data")
DASH = Path("dashboard")
ASOF = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
CAP = "market_cap_100m_cny"
CAP_DATE = "market_cap_as_of"
CAP_SOURCE = "market_cap_source_url"
COLS = (CAP, CAP_DATE, CAP_SOURCE)
SOURCE = "https://quote.eastmoney.com/"
TENCENT = "https://qt.gtimg.cn/"

def obj(path, default):
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError): return default

def persist(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)

def cap_yi(raw):
    try:
        v = float(raw)
        if not math.isfinite(v) or v <= 0: return None
        return round(v / 1e8, 4)
    except (TypeError, ValueError): return None

def code_of(raw):
    s = str(raw or "").strip()
    if re.fullmatch(r"\d{1,6}(?:\.0)?",s):
        return str(int(float(s))).zfill(6)
    return ""

def bulk_snapshot():
    df = ak.stock_zh_a_spot_em()
    if df is None or len(df) < 2000:
        raise RuntimeError(f"Unexpectedly few full-market A-share records: {0 if df is None else len(df)}")
    if "代码" not in df.columns or "总市值" not in df.columns:
        raise RuntimeError(f"Missing essential quote columns: {list(df.columns)}")
    out = {}
    for _, row in df.iterrows():
        c = code_of(row.get("代码"))
        value = cap_yi(row.get("总市值"))
        if c and value is not None:
            out[c] = value
    if len(out) < 1800:
        raise RuntimeError(f"Too few valid market caps in snapshot: {len(out)}")
    return out

def single_cap(stock_code):
    df=ak.stock_individual_info_em(symbol=stock_code,timeout=8)
    if df is None or df.empty or not {"item","value"}.issubset(df.columns): return None
    values=dict(zip(df["item"],df["value"]))
    returned=code_of(values.get("股票代码"))
    if returned and returned != stock_code: return None
    return cap_yi(values.get("总市值"))

def tencent_symbol(code):
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    return "bj" + code

def tencent_snapshot(codes):
    # Tencent's A-share field 45 is *total* capitalization, already CNY 100m.
    # Request in small batches to avoid large payloads and accidental rate bursts.
    caps = {}
    for offset in range(0, len(codes), 30):
        batch = codes[offset:offset + 30]
        symbols = [tencent_symbol(c) for c in batch]
        try:
            resp = requests.get(
                TENCENT + "q=" + ",".join(symbols),
                timeout=16,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
            )
            resp.raise_for_status()
            resp.encoding = "gbk"
            for raw in resp.text.split(";"):
                match = re.search(r'v_(?:sh|sz|bj)(\d{6})="([^"]+)"', raw)
                if not match:
                    continue
                code, payload = match.groups()
                if code not in batch:
                    continue
                fields = payload.split("~")
                if len(fields) <= 45 or fields[2] != code:
                    continue
                try:
                    cap = float(fields[45])
                    price = float(fields[3])
                    if not (math.isfinite(cap) and math.isfinite(price) and cap > 0 and price > 0):
                        continue
                    if cap > 1e7:
                        continue
                except (ValueError, TypeError):
                    continue
                snapshot_date = ASOF
                stamp = fields[30].strip() if len(fields)>30 else ""
                if re.fullmatch(r"\d{14}", stamp):
                    snapshot_date = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"
                caps[code] = {CAP: round(cap, 4), CAP_DATE: snapshot_date, CAP_SOURCE: TENCENT}
        except requests.RequestException as exc:
            logging.warning("Tencent quote batch %s failed: %s", offset // 30 + 1, exc)
        if offset + 30 < len(codes):
            time.sleep(0.7)
    return caps

def main():
    path = ROOT / "ipo_master.csv"
    with path.open("r",encoding="utf-8-sig",newline="") as f:
        reader=csv.DictReader(f)
        cols=list(reader.fieldnames or [])
        rows=list(reader)
    for c in COLS:
        if c not in cols: cols.append(c)
    listed_codes={str(r.get("stock_code","")).strip() for r in rows if r.get("category")=="已上市" and re.fullmatch(r"\d{6}",str(r.get("stock_code","")).strip())}
    cache=obj(ROOT/"market_cap_cache.json",{})
    quote=None
    error=""
    for attempt in range(1,4):
        try:
            quote=bulk_snapshot()
            print(f"Market cap quotes: {len(quote)}, matched listed: {len(listed_codes & set(quote))}/{len(listed_codes)}",flush=True)
            break
        except Exception as e:
            error=str(e)
            logging.warning("Full-market cap attempt %s/3 failed: %s",attempt,e)
            if attempt < 3: time.sleep(attempt*6)
    refreshed=set()
    if quote is not None:
        for code in listed_codes:
            if code in quote:
                cache[code]={CAP:quote[code],CAP_DATE:ASOF,CAP_SOURCE:SOURCE}
                refreshed.add(code)
    missing=sorted(listed_codes-refreshed)
    if missing:
        print(f"Fetching Tencent market cap fallback: {len(missing)}", flush=True)
        fallback=tencent_snapshot(missing)
        for code, item in fallback.items():
            cache[code]=item
            refreshed.add(code)
        print(f"Tencent fallback matched: {len(fallback)}", flush=True)
    missing=sorted(listed_codes-refreshed)
    # Final individual fallback is deliberately bounded.
    for code in missing[:12]:
        try:
            v=single_cap(code)
            if v is not None:
                cache[code]={CAP:v,CAP_DATE:ASOF,CAP_SOURCE:SOURCE}
                refreshed.add(code)
        except Exception as e:
            logging.warning("Single-stock cap %s failed: %s",code,e)
    persist(ROOT/"market_cap_cache.json",cache)
    for row in rows:
        item=cache.get(str(row.get("stock_code","")).strip(),{}) if row.get("category")=="已上市" else {}
        for key in COLS: row[key]=item.get(key,"")
    temp=path.with_suffix(".tmp")
    with temp.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction="ignore")
        w.writeheader();w.writerows(rows)
    temp.replace(path)
    payload=obj(DASH/"ipo_data.json",{"summary":{},"rows":[]})
    payload["rows"]=rows
    latest_count=sum(bool(row[CAP]) for row in rows if row.get("category")=="已上市")
    payload.setdefault("summary",{})["market_cap"]={
        "fetch_date":ASOF,
        "refreshed_today":len(refreshed),
        "covered":latest_count,
        "listed":len(listed_codes),
        "source_ok": bool(refreshed),
        "source_error":error if quote is None and not refreshed else "",
        "unit":"亿元人民币",
        "note":"Total market capitalization (not free-float). From Eastmoney where available or Tencent A-share quotes (field 45, CNY 100m); quote date or collection date shown per row."
    }
    persist(DASH/"ipo_data.json",payload)
    print(f"Market cap: refreshed {len(refreshed)}/{len(listed_codes)}, covered {latest_count}/{len(listed_codes)}",flush=True)

if __name__ == "__main__":
    main()
