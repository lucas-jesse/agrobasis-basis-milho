import io, requests, pandas as pd
B="https://portaldeinformacoes.conab.gov.br/downloads/arquivos/"
def ler(n):
    r=requests.get(B+n,timeout=600,headers={"User-Agent":"Mozilla/5.0 AgroBasis"}); r.raise_for_status()
    d=pd.read_csv(io.BytesIO(r.content),sep=";",dtype=str,encoding="latin-1",on_bad_lines="skip")
    d.columns=[c.strip().lower() for c in d.columns]
    for c in d.columns: d[c]=d[c].str.strip()
    return d
pd.set_option("display.width",250)
for nome,sem in [("PrecosSemanalMunicipio.txt",True),("PrecosMensalMunicipio.txt",False)]:
    d=ler(nome); print("=====",nome,len(d))
    print("niveis:",d.dsc_nivel_comercializacao.value_counts().to_dict())
    print("anos:",d.ano.min(),d.ano.max())
    p=d.produto.str.upper()
    x=d[p.str.match(r"^(SOJA|MILHO)\b",na=False)].copy()
    print(x.groupby(["produto","classificao_produto","dsc_nivel_comercializacao"]).size().to_string())
    x["per"]=x.ano.astype(int)*100+x.mes.astype(int)
    pp=x[x.dsc_nivel_comercializacao.str.contains("PRODUTOR",na=False)]
    print("munic. preço produtor por ano e produto:")
    print(pp.assign(pr=pp.produto.str.split().str[0]).groupby(["ano","pr"]).nom_municipio.nunique().unstack().to_string())
    rec=pp[pp.per>=202511]
    print("últimos 12m, municípios por UF:")
    print(rec.assign(pr=rec.produto.str.split().str[0]).groupby(["uf","pr"]).nom_municipio.nunique().unstack().to_string())
    if sem:
        rec=rec.assign(pr=rec.produto.str.split().str[0])
        k=rec.groupby(["pr","uf","nom_municipio"]).data_inicial_final_semana.nunique()
        print(k.groupby(level=0).describe().to_string())
        print(k.sort_values(ascending=False).groupby(level=0).head(30).to_string())
    else:
        k=pp.assign(pr=pp.produto.str.split().str[0]).groupby(["pr","uf","nom_municipio"]).agg(ini=("per","min"),fim=("per","max"),n=("per","nunique"))
        print(k[k.n>=120].groupby(level=0).size().to_string(), "municípios com 10+ anos de meses")
        print(k.sort_values("n",ascending=False).groupby(level=0).head(25).to_string())
