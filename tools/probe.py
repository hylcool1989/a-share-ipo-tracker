import akshare as ak
print("AKShare",getattr(ak,"__version__","unknown"))
for k in ["stock_profile_cninfo","stock_zyjs_ths","stock_financial_abstract_ths","stock_yjbb_em"]:
    print(k,"available",hasattr(ak,k),flush=True)
for code in ["301584","688801","920060"]:
    for fname,kwargs in [("stock_profile_cninfo",{"symbol":code}),("stock_financial_abstract_ths",{"symbol":code,"indicator":"按报告期"})]:
        try:
            fn=getattr(ak,fname)
            df=fn(**kwargs)
            print(code,fname,"SHAPE",df.shape,"COLUMNS",list(df.columns),"ROWS",df.head(6).to_json(orient="records",force_ascii=False),flush=True)
        except Exception as e: print(code,fname,"ERROR",repr(e),flush=True)
try:
    df=ak.stock_yjbb_em(date="20251231")
    print("stock_yjbb_em","SHAPE",df.shape,"COLUMNS",list(df.columns),"SAMPLE",df[df["股票代码"].astype(str).str.contains("301584|688801|920060")].to_json(orient="records",force_ascii=False),flush=True)
except Exception as e: print("stock_yjbb_em ERROR",repr(e),flush=True)

# Trigger API probe workflow
