"""Enrich recent A-share IPO listings using source-backed business descriptions and financial results.

Business: CNINFO individual profile, '主营业务' (not hallucinated from sector).
Financial: Eastmoney dated earnings snapshots, revenue and net profit (CNY).
Never invent unavailable values; keep last successful cached content.
"""
import csv
import json
import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import akshare as ak

DATA = Path("data")
WEB = Path("dashboard")
TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()
ASOF = TODAY.isoformat()
BIZ_KEYS = ["business_summary", "business_source_url", "business_updated"]
FIN_KEYS = ["revenue_2025_100m", "net_profit_2025_100m", "revenue_2026_9m_100m",
            "net_profit_2026_9m_100m", "financial_source_url", "financial_updated"]
SOURCE_BIZ = "https://webapi.cninfo.com.cn/"
SOURCE_FIN = "https://data.eastmoney.com/bbsj/"
PROFILE_WORKERS = max(1, min(8, int(os.getenv("PROFILE_WORKERS", "5"))))
PROFILE_BATCH = int(os.getenv("PROFILE_BATCH", "350"))

def clean(x):
    if x is None:
        return ""
    s = str(x).strip()
    return "" if s.lower() in ("nan", "nat", "none", "null", "<na>", "-", "--") else " ".join(s.split())

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)

def valid_fresh(s, days):
    try:
        return (TODAY - datetime.strptime(s[:10], "%Y-%m-%d").date()).days < days
    except (TypeError, ValueError):
        return False

def fetch_business(stock_code):
    for attempt in range(2):
        try:
            df = ak.stock_profile_cninfo(symbol=stock_code)
            if df is None or df.empty:
                return {"business_summary": "", "business_updated": ASOF}
            row = df.iloc[0]
            # Preferred: main business from issuer's official company profile.
            desc = clean(row.get("主营业务"))
            if not desc:
                desc = clean(row.get("经营范围"))
            return {"business_summary": desc[:700], "business_updated": ASOF,
                    "business_source_url": SOURCE_BIZ}
        except Exception as e:
            if attempt == 1:
                logging.warning("CNINFO %s unavailable: %s", stock_code, e)
            else:
                time.sleep(1.4)
    return None

def financial_snapshot(period):
    df = ak.stock_yjbb_em(date=period)
    result = {}
    for _, r in df.iterrows():
        code = clean(r.get("股票代码")).split(".")[0].zfill(6)
        if not re.fullmatch(r"\d{6}", code):
            continue
        revenue = r.get("营业总收入-营业总收入", r.get("营业收入-营业收入"))
        profit = r.get("净利润-净利润")
        def yi(v):
            try:
                n = float(v)
                if n != n:
                    return ""
                return round(n / 1e8, 4)
            except (TypeError, ValueError):
                return ""
        values = {"revenue": yi(revenue), "net_profit": yi(profit)}
        if not values["revenue"] and not values["net_profit"]:
            continue
        # Where multiple announcements occur for a code, latest disclosure wins.
        ann = str(r.get("最新公告日期", "") or "")
        if code not in result or ann >= result[code].get("_ann", ""):
            result[code] = {**values, "_ann": ann}
    return result

def main():
    path = DATA / "ipo_master.csv"
    if not path.exists():
        raise RuntimeError("Run update.py to generate IPO master table first")
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = list(reader)
    for k in BIZ_KEYS + FIN_KEYS:
        if k not in columns:
            columns.append(k)
    profiles = load_json(DATA / "company_profiles.json", {})
    finance = load_json(DATA / "company_financials.json", {})
    finance_info = load_json(DATA / "financial_source_status.json", {})
    listings = [r for r in rows if re.fullmatch(r"\d{6}", clean(r.get("stock_code")))]
    wanted = sorted({r["stock_code"] for r in listings})
    pending = [c for c in wanted if not clean(profiles.get(c, {}).get("business_summary"))
               and not valid_fresh(profiles.get(c, {}).get("business_updated"), 14)]
    pending = pending[:PROFILE_BATCH]
    print("Profile backlog:", len(pending), "of", len(wanted), flush=True)
    with ThreadPoolExecutor(max_workers=PROFILE_WORKERS) as executor:
        future_to_code = {executor.submit(fetch_business, c): c for c in pending}
        for fut in as_completed(future_to_code):
            code = future_to_code[fut]
            try:
                result = fut.result()
                if result is not None:
                    profiles[code] = result
            except Exception:
                logging.exception("Profile request failed for %s", code)
    save_json(DATA / "company_profiles.json", profiles)
    # Each report period fetched in one bulk request, not one request per stock.
    # Recheck periodically to account for late reports / newly listed companies.
    for period, keys in (("20251231", ("revenue_2025_100m", "net_profit_2025_100m")),
                         ("20260930", ("revenue_2026_9m_100m", "net_profit_2026_9m_100m"))):
        if valid_fresh(finance_info.get(period, ""), 10):
            continue
        try:
            print("Fetching financial period", period, flush=True)
            snapshot = financial_snapshot(period)
            if len(snapshot) < 500:
                raise RuntimeError(f"Unusually small financial dataset: {len(snapshot)}")
            for c, v in snapshot.items():
                entry = finance.setdefault(c, {})
                if v["revenue"] != "":
                    entry[keys[0]] = v["revenue"]
                if v["net_profit"] != "":
                    entry[keys[1]] = v["net_profit"]
                entry["financial_source_url"] = SOURCE_FIN + period[:6] + "/yjbb.html"
                entry["financial_updated"] = ASOF
            finance_info[period] = ASOF
            save_json(DATA / "company_financials.json", finance)
            save_json(DATA / "financial_source_status.json", finance_info)
            print(period, "financial records:", len(snapshot), flush=True)
        except Exception as e:
            logging.exception("Financial snapshot %s failed: %s", period, e)
    covered = 0
    financial_covered = 0
    for row in rows:
        c = clean(row.get("stock_code"))
        p = profiles.get(c, {})
        q = finance.get(c, {})
        for k in BIZ_KEYS:
            row[k] = p.get(k, "")
        for k in FIN_KEYS:
            row[k] = q.get(k, "")
        if row["business_summary"]:
            covered += 1
        if any(row.get(k, "") != "" for k in ("revenue_2025_100m", "revenue_2026_9m_100m")):
            financial_covered += 1
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    payload = load_json(WEB / "ipo_data.json", {"rows": [], "summary": {}})
    payload["rows"] = rows
    payload.setdefault("summary", {})["enrichment"] = {
        "business_covered": covered, "financial_covered": financial_covered,
        "listed_or_issued": len(listings), "description_pending": len(pending),
        "notes": "Listed-company profiles from CNINFO; annual FY2025 and 2026 9M values from Eastmoney earnings reports. IPO application prospectus profiles not yet automated."
    }
    save_json(WEB / "ipo_data.json", payload)
    print("Enrichment done, business:", covered, "financial:", financial_covered,
          "listed or issued:", len(listings), flush=True)

if __name__ == "__main__":
    main()
