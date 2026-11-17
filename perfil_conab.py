import io, requests, pandas as pd
B="https://portaldeinformacoes.conab.gov.br/downloads/arquivos/"
H={"User-Agent":"Mozilla/5.0 AgroBasis"}
for n in ["PrecosSemanalUF.txt","PrecosMensalUF.txt","PrecosAnualUF.txt","PrecosAnualMunicipio.txt","PrecosSemanalMunicipio.txt","PrecosMensalMunicipio.txt"]:
    try:
        r=requests.get(B+n,timeout=600,headers=H); print("##",n,r.status_code,round(len(r.content)/1e6,1),"MB")
        if not r.ok: continue
        d=pd.read_csv(io.BytesIO(r.content),sep=";",dtype=str,encoding="latin-1",on_bad_lines="skip")
        d.columns=[c.strip().lower() for c in d.columns]
        for c in d.columns: d[c]=d[c].str.strip()
        x=d[d.produto.str.upper().str.match(r"^(SOJA|MILHO)$",na=False) & d.dsc_nivel_comercializacao.str.contains("RECEBIDO",na=False)]
        x=x.assign(per=x.ano.astype(int)*100+(x.mes.astype(int) if "mes" in x else 0))
        geo="nom_municipio" if "nom_municipio" in x else "uf"
        print(x.groupby("produto").agg(ini=("per","min"),fim=("per","max"),locais=(geo,"nunique"),linhas=("per","size")).to_string())
        print(x.groupby(["produto","ano"])[geo].nunique().unstack(0).to_string()[:1500])
    except Exception as e: print("erro",n,e)
