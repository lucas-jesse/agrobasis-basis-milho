import io, requests, pandas as pd, re
URL="https://portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalMunicipio.txt"
r=requests.get(URL,timeout=600,headers={"User-Agent":"Mozilla/5.0 AgroBasis"}); r.raise_for_status()
print("MB",len(r.content)/1e6)
df=pd.read_csv(io.BytesIO(r.content),sep=";",dtype=str,encoding="latin-1" if b"\xe7" in r.content[:200000] and not r.content[:200000].decode("utf-8","ignore") else "utf-8",on_bad_lines="skip")
print(df.columns.tolist()); print(df.head(3).to_string())
p=df["PRODUTO"].str.upper()
for col in df.columns:
    if df[col].nunique()<40: print(col, df[col].value_counts().head(15).to_dict())
d=df[p.str.contains("SOJA|MILHO",na=False)].copy()
d["dt"]=pd.to_datetime(d["DATA_INICIAL_FINAL_SEMANA"].str.replace("-","/").str.findall(r"\d{2}/\d{2}/\d{4}").str[-1],dayfirst=True,errors="coerce")
nivel=[c for c in d.columns if "NIVEL" in c.upper() or "COMERC" in c.upper()]
grp=["PRODUTO"]+nivel
print(d.groupby(grp).agg(linhas=("dt","size"),ini=("dt","min"),fim=("dt","max"),munis=("NOM_MUNICIPIO","nunique")).to_string())
d["ano"]=d.dt.dt.year
print(d.groupby(grp+["ano"]).NOM_MUNICIPIO.nunique().unstack(0 if not nivel else list(range(len(grp)))).tail(12).to_string())
rec=d[d.dt>=pd.Timestamp("2025-11-01")]
print(rec.groupby(grp+["UF"]).NOM_MUNICIPIO.nunique().to_string())
for prod in ["SOJA","MILHO"]:
    x=rec[rec.PRODUTO.str.upper().str.contains(prod)]
    print(prod, "semanas por município (últimos 12m):", x.groupby(["UF","NOM_MUNICIPIO"]).dt.nunique().describe().to_dict())
    print(x.groupby(["UF","NOM_MUNICIPIO"]).dt.nunique().sort_values(ascending=False).head(40).to_string())
