"""
pipeline_milho.py — Pipeline de dados do Basis do Milho (AgroBasis)

Fonte única da lógica de dados, usada por:
  • app.py (Streamlit)              → leitura + atualização de contingência
  • atualizar_basis_milho.py (CLI)  → job agendado (GitHub Actions / agendador local)

Fluxo
-----
1. CONAB  → baixa PrecosSemanalMunicipio.txt (streaming, em chunks, filtrando milho
            e praças monitoradas) e faz UPSERT no histórico persistido na aba
            `conab_historico`. Se o download falhar (SSL, timeout, layout), o
            pipeline segue com o histórico já salvo — o app nunca cai por isso.
2. FÍSICO → série manual diária (Planilha1, NUNCA alterada) + Conab semanal a partir
            da última data manual de cada praça (sem sobrepor fontes).
3. B3     → ajustes diários por contrato CCM*, ffill limitado a feriados.
4. BASIS  → observação diária (manual): B3 do mesmo dia.
            observação semanal (Conab = média da semana): média do contrato B3
            na MESMA semana (casamento de janelas, sem look-ahead).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import certifi
import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("agrobasis.milho")
TZ = ZoneInfo("America/Sao_Paulo")

# =============================================================================
# CONFIGURAÇÃO
# =============================================================================
PLANILHA_FISICO = "Preço milho Fisico"
PLANILHA_B3 = "Historico Spread Milho"

ABA_FISICO_ORIGINAL = "Planilha1"       # série manual — somente leitura
ABA_B3 = "Planilha1"
ABA_FISICO_ATUALIZADO = "fisico_atualizado"
ABA_COMPLETA = "base_milho_completa"
ABA_BASIS = "basis_diario"
ABA_CONAB_HIST = "conab_historico"      # NOVO: histórico Conab acumulado (upsert)
ABA_META = "meta_atualizacao"           # NOVO: controle de execução

URL_CONAB = "https://portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalMunicipio.txt"

# Certificado intermediário opcional (cadeia incompleta no servidor gov.br).
# Também pode ser apontado pela variável de ambiente CONAB_CA_BUNDLE.
CERT_EXTRA = Path(__file__).parent / "certs" / "conab_cadeia.pem"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

MAPA_CIDADES = {
    ("RS", "SANTA ROSA"): "Santa Rosa RS",
    ("RS", "PASSO FUNDO"): "Passo Fundo RS",
    ("PR", "MARINGA"): "Maringá PR",
    ("PR", "PONTA GROSSA"): "Ponta Grossa PR",
    ("MT", "SORRISO"): "Sorriso MT",
    ("MT", "RONDONOPOLIS"): "Rondonópolis MT",
    ("GO", "RIO VERDE"): "Rio Verde GO",
    ("SP", "ITAPEVA"): "Itapeva SP",
    ("RO", "VILHENA"): "Vilhena RO",
}
CIDADES = list(MAPA_CIDADES.values())

# Produto: valide no log ("Produtos Conab considerados") na primeira execução.
PRODUTO_INCLUI = "MILHO"
PRODUTO_EXCLUI = ("PIPOCA", "VERDE", "CANJICA", "FUBA", "FARELO")

# Faixa ampla: serve só para pegar erro de unidade. A faixa antiga (35–100)
# descartava milho real de MT/RO, que já negociou abaixo de R$ 35/sc.
PRECO_MIN, PRECO_MAX = 15.0, 150.0
DESVIO_MAX_MEDIANA = 0.35   # descarta ponto >35% fora da mediana móvel (8 semanas) da praça
FFILL_B3_DIAS = 5           # cobre feriados/pontes; preço FÍSICO nunca é propagado

COLUNAS_CONAB = {"PRODUTO", "UF", "NOM_MUNICIPIO", "VALOR_PRODUTO_KG", "DATA_INICIAL_FINAL_SEMANA"}
COLUNAS_HIST = ["Semana_Ini", "Semana_Fim", "Cidade", "Preco", "Coletado_Em"]
RE_CONTRATO = re.compile(r"^CCM[FHKNUX]\d{2}$")
RE_DATA_BR = r"\d{2}/\d{2}/\d{4}"


# =============================================================================
# PARSERS
# =============================================================================
def normalizar_texto(txt) -> str:
    """Remove acentos e padroniza caixa (cobre qualquer diacrítico, não só os listados)."""
    sem_acento = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    return sem_acento.upper().strip()


def parse_data(valor):
    if pd.isna(valor) or str(valor).strip() == "":
        return pd.NaT
    txt = str(valor).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return pd.Timestamp(datetime.strptime(txt, fmt))
        except ValueError:
            continue
    return pd.to_datetime(txt, errors="coerce", dayfirst=True)


def parse_numero(valor) -> float:
    """Número de tabela interna (gravada pelo próprio pipeline). Sem heurística de escala."""
    if valor is None or (isinstance(valor, float) and np.isnan(valor)):
        return np.nan
    txt = str(valor).strip().replace("R$", "").replace("%", "").replace(" ", "")
    if not txt:
        return np.nan
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except ValueError:
        return np.nan


def limpar_preco(valor) -> float:
    """
    Preço das planilhas LEGADAS (Planilha1 física e B3), digitadas/coladas à mão.
    Mantém a heurística original de escala (vírgula perdida → 4550 vira 45,50).
    Atenção: só é segura porque milho em R$/sc nunca passa de 150.
    """
    numero = parse_numero(valor)
    if np.isnan(numero):
        return np.nan
    if numero > 1000:
        return numero / 100
    if numero > 150:
        return numero / 10
    return numero


def contrato_ref(data: pd.Timestamp) -> str:
    """Contrato B3 de referência do basis para a data (vencimento mais próximo à frente)."""
    mes, ano = data.month, data.year
    if mes in (1, 2):
        return f"CCMH{ano % 100:02d}"
    if mes in (3, 4):
        return f"CCMK{ano % 100:02d}"
    if mes in (5, 6):
        return f"CCMN{ano % 100:02d}"
    if mes in (7, 8):
        return f"CCMU{ano % 100:02d}"
    if mes in (9, 10):
        return f"CCMX{ano % 100:02d}"
    return f"CCMF{(ano + 1) % 100:02d}"


# =============================================================================
# GOOGLE SHEETS
# =============================================================================
def conectar_google(credenciais: dict | str | None = None):
    """
    Credenciais, em ordem de prioridade:
      1. argumento (dict ou JSON string — ex.: st.secrets no app)
      2. variável de ambiente GOOGLE_CREDENTIALS (GitHub Actions)
      3. arquivo credenciais.json (execução local)
    """
    import gspread
    from google.oauth2.service_account import Credentials

    if credenciais is None:
        credenciais = os.environ.get("GOOGLE_CREDENTIALS")
    if isinstance(credenciais, str):
        credenciais = json.loads(credenciais)
    if credenciais:
        creds = Credentials.from_service_account_info(dict(credenciais), scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credenciais.json", scopes=SCOPES)
    return gspread.authorize(creds)


def ler_df(sh, aba: str) -> pd.DataFrame:
    """Lê uma aba como DataFrame de strings. Aba inexistente → DataFrame vazio."""
    import gspread

    try:
        valores = sh.worksheet(aba).get_all_values()
    except gspread.WorksheetNotFound:
        return pd.DataFrame()
    if not valores:
        return pd.DataFrame()
    return pd.DataFrame(valores[1:], columns=valores[0])


def salvar_df(sh, aba: str, df: pd.DataFrame) -> None:
    """Sobrescreve a aba com o DataFrame, redimensionando a grade (evita estouro de linhas)."""
    import gspread

    envio = df.copy()
    for col in envio.columns:
        if pd.api.types.is_datetime64_any_dtype(envio[col]):
            envio[col] = envio[col].dt.strftime("%d/%m/%Y")
    envio = envio.astype(object).where(envio.notna(), "")

    n_rows, n_cols = max(len(envio) + 1, 2), max(len(envio.columns), 1)
    try:
        ws = sh.worksheet(aba)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=aba, rows=n_rows, cols=n_cols)

    ws.clear()
    ws.resize(rows=n_rows, cols=n_cols)
    ws.update(values=[envio.columns.tolist()] + envio.values.tolist(), range_name="A1")


# =============================================================================
# CONAB — DOWNLOAD (SSL robusto) E PARSE
# =============================================================================
def _ca_bundle() -> str:
    """certifi + certificado intermediário da Conab, se fornecido."""
    extra = os.environ.get("CONAB_CA_BUNDLE") or (str(CERT_EXTRA) if CERT_EXTRA.exists() else None)
    if not extra:
        return certifi.where()
    destino = Path(tempfile.gettempdir()) / "agrobasis_ca_bundle.pem"
    if not destino.exists():
        destino.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + Path(extra).read_bytes())
    return str(destino)


def _sessao_http() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (compatible; AgroBasis/1.0; +https://agrobasis.com.br)"
    s.verify = _ca_bundle()  # verificação SSL SEMPRE ligada
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def baixar_arquivo_conab(timeout=(15, 180)) -> str:
    """Baixa o arquivo da Conab em streaming para um arquivo temporário. Retorna o caminho."""
    with _sessao_http() as s, s.get(URL_CONAB, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            for bloco in r.iter_content(chunk_size=1 << 20):
                tmp.write(bloco)
            return tmp.name


def parse_arquivo_conab(caminho: str) -> pd.DataFrame:
    """
    Lê o arquivo completo da Conab (todo o histórico semanal) em chunks e devolve
    as observações de milho das praças monitoradas, em formato longo:
    Semana_Ini | Semana_Fim | Cidade | Preco (R$/sc 60 kg)
    """
    partes = []
    leitor = pd.read_csv(
        caminho,
        sep=";",
        encoding="latin1",
        dtype=str,
        usecols=lambda c: c.strip().upper() in COLUNAS_CONAB,
        chunksize=250_000,
    )
    for chunk in leitor:
        chunk.columns = chunk.columns.str.strip().str.upper()
        faltando = COLUNAS_CONAB - set(chunk.columns)
        if faltando:
            raise ValueError(f"Layout da Conab mudou — colunas ausentes: {sorted(faltando)}")
        prod = chunk["PRODUTO"].str.upper()
        mask = prod.str.contains(PRODUTO_INCLUI, na=False) & ~prod.str.contains(
            "|".join(PRODUTO_EXCLUI), na=False
        )
        if mask.any():
            partes.append(chunk.loc[mask])

    if not partes:
        raise ValueError("Arquivo da Conab sem registros de milho.")

    df = pd.concat(partes, ignore_index=True)
    log.info("Produtos Conab considerados: %s", sorted(df["PRODUTO"].dropna().unique()))

    # Praça
    uf = df["UF"].str.strip().str.upper()
    mun = df["NOM_MUNICIPIO"].map(normalizar_texto).str.replace(r"-[A-Z]{2}$", "", regex=True).str.strip()
    df["Cidade"] = [MAPA_CIDADES.get(k) for k in zip(uf, mun)]
    df = df.dropna(subset=["Cidade"])

    # Semana: extrai todas as datas da string, independente do separador
    datas = df["DATA_INICIAL_FINAL_SEMANA"].astype(str).str.findall(RE_DATA_BR)
    df["Semana_Ini"] = pd.to_datetime(datas.str[0], format="%d/%m/%Y", errors="coerce")
    df["Semana_Fim"] = pd.to_datetime(datas.str[-1], format="%d/%m/%Y", errors="coerce")
    if df["Semana_Fim"].isna().all():
        raise ValueError("Formato de DATA_INICIAL_FINAL_SEMANA não reconhecido.")

    preco_kg = pd.to_numeric(df["VALOR_PRODUTO_KG"].str.replace(",", ".", regex=False), errors="coerce")
    df["Preco"] = preco_kg * 60
    df = df.dropna(subset=["Semana_Ini", "Semana_Fim", "Preco"])

    # Mais de um registro de milho na mesma praça-semana → média
    out = df.groupby(["Semana_Ini", "Semana_Fim", "Cidade"], as_index=False)["Preco"].mean()
    out["Preco"] = out["Preco"].round(2)
    out["Coletado_Em"] = pd.Timestamp(datetime.now(TZ).date())
    return filtrar_outliers(out)


def filtrar_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """Faixa absoluta + desvio contra a mediana móvel centrada de 8 semanas da praça."""
    df = df[df["Preco"].between(PRECO_MIN, PRECO_MAX)].sort_values(["Cidade", "Semana_Fim"])
    mediana = df.groupby("Cidade")["Preco"].transform(
        lambda s: s.rolling(8, min_periods=3, center=True).median()
    )
    desvio = (df["Preco"] / mediana - 1).abs()
    ok = desvio.isna() | (desvio <= DESVIO_MAX_MEDIANA)
    if (~ok).any():
        log.warning("Conab: %d outliers descartados:\n%s", (~ok).sum(), df.loc[~ok].to_string())
    return df.loc[ok].reset_index(drop=True)


def baixar_conab_milho() -> pd.DataFrame:
    caminho = baixar_arquivo_conab()
    try:
        return parse_arquivo_conab(caminho)
    finally:
        os.unlink(caminho)


# =============================================================================
# HISTÓRICO CONAB (persistido)
# =============================================================================
def ler_historico_conab(sh) -> pd.DataFrame:
    df = ler_df(sh, ABA_CONAB_HIST)
    if df.empty or not set(COLUNAS_HIST).issubset(df.columns):
        return pd.DataFrame(columns=COLUNAS_HIST)
    for col in ("Semana_Ini", "Semana_Fim", "Coletado_Em"):
        df[col] = pd.to_datetime(df[col], format="%d/%m/%Y", errors="coerce")
    df["Preco"] = pd.to_numeric(df["Preco"].map(parse_numero), errors="coerce")
    return df.dropna(subset=["Semana_Fim", "Cidade", "Preco"])[COLUNAS_HIST]


def mesclar_historico(hist_salvo: pd.DataFrame, novo: pd.DataFrame) -> pd.DataFrame:
    """
    UPSERT por (Semana_Fim, Cidade). O dado mais recente vence (a Conab revisa
    semanas). Semanas que saírem do arquivo da Conab CONTINUAM no histórico.
    """
    base = pd.concat([hist_salvo, novo], ignore_index=True)
    base = base.sort_values("Coletado_Em", kind="stable")
    base = base.drop_duplicates(["Semana_Fim", "Cidade"], keep="last")
    return base.sort_values(["Cidade", "Semana_Fim"]).reset_index(drop=True)[COLUNAS_HIST]


# =============================================================================
# FÍSICO E B3
# =============================================================================
def ler_fisico_manual(sh) -> pd.DataFrame:
    """Planilha1 (larga, diária) → longa: Data | Cidade | Preco | Semana_Ini | Semana_Fim | Fonte."""
    df = ler_df(sh, ABA_FISICO_ORIGINAL)
    if df.empty or "Data" not in df.columns:
        return pd.DataFrame(columns=["Data", "Cidade", "Preco", "Semana_Ini", "Semana_Fim", "Fonte"])
    df["Data"] = df["Data"].map(parse_data)
    longo = df.melt(id_vars="Data", var_name="Cidade", value_name="Preco")
    longo["Preco"] = pd.to_numeric(longo["Preco"].map(limpar_preco), errors="coerce")
    longo = longo.dropna(subset=["Data", "Preco"])
    longo["Semana_Ini"] = longo["Data"]
    longo["Semana_Fim"] = longo["Data"]
    longo["Fonte"] = "MANUAL"
    return longo


def montar_fisico(manual: pd.DataFrame, hist_conab: pd.DataFrame) -> pd.DataFrame:
    """
    Série física combinada. Regra de precedência por praça:
      manual (diário) até a última data manual; Conab (semanal) só DEPOIS dela.
    Evita misturar duas fontes no mesmo período (degrau artificial no basis).
    Praça sem nenhum dado manual usa todo o histórico Conab.
    """
    ultimo_manual = manual.groupby("Cidade")["Data"].max()
    conab = hist_conab.copy()
    conab["Data"] = conab["Semana_Fim"]   # média semanal só é conhecida no fim da semana
    conab["Fonte"] = "CONAB"
    corte = conab["Cidade"].map(ultimo_manual)
    conab = conab[corte.isna() | (conab["Semana_Ini"] > corte)]

    cols = ["Data", "Cidade", "Preco", "Semana_Ini", "Semana_Fim", "Fonte"]
    return (
        pd.concat([manual[cols], conab[cols]], ignore_index=True)
        .sort_values(["Cidade", "Data"])
        .reset_index(drop=True)
    )


def ler_b3(client) -> pd.DataFrame:
    df = ler_df(client.open(PLANILHA_B3), ABA_B3)
    df["Data"] = df["Data"].map(parse_data)
    for col in df.columns.drop("Data"):
        df[col] = pd.to_numeric(df[col].map(limpar_preco), errors="coerce")
    return df.dropna(subset=["Data"]).drop_duplicates("Data", keep="last").sort_values("Data")


def montar_base_completa(fisico: pd.DataFrame, b3: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Retorna (base_completa, fisico_largo).
    Outer join: mantém todos os pregões da B3 (curva sempre com o ajuste mais recente).
    ffill só nas colunas B3 e limitado; físico nunca é propagado.
    """
    largo = fisico.pivot_table(index="Data", columns="Cidade", values="Preco", aggfunc="mean").reset_index()
    largo.columns.name = None
    base = largo.merge(b3, on="Data", how="outer").sort_values("Data").reset_index(drop=True)
    cols_b3 = [c for c in b3.columns if c != "Data"]
    base[cols_b3] = base[cols_b3].ffill(limit=FFILL_B3_DIAS)
    return base, largo


# =============================================================================
# BASIS
# =============================================================================
def _media_janela(idx: pd.Series, contrato: str, ini, fim) -> float:
    try:
        serie = idx.loc[contrato]
    except KeyError:
        return np.nan
    serie = serie.loc[ini:fim]
    return float(serie.mean()) if len(serie) else np.nan


def calcular_basis(fisico: pd.DataFrame, b3: pd.DataFrame) -> pd.DataFrame:
    contratos = [c for c in b3.columns if RE_CONTRATO.match(str(c))]
    b3_long = (
        b3[["Data"] + contratos]
        .melt(id_vars="Data", var_name="Contrato_Ref", value_name="Preco_B3")
        .replace({"Preco_B3": {0: np.nan}})
        .dropna(subset=["Preco_B3"])
        .sort_values("Data")
    )

    f = fisico[fisico["Cidade"].isin(CIDADES)].copy()
    f["Contrato_Ref"] = f["Data"].map(contrato_ref)

    # 1) Observações diárias: ajuste do dia (ou último pregão até FFILL_B3_DIAS antes)
    diario = pd.merge_asof(
        f[f["Fonte"] == "MANUAL"].sort_values("Data"),
        b3_long,
        on="Data",
        by="Contrato_Ref",
        direction="backward",
        tolerance=pd.Timedelta(days=FFILL_B3_DIAS),
    )

    # 2) Observações semanais Conab: média do contrato B3 na mesma semana
    semanal = f[f["Fonte"] == "CONAB"].copy()
    if not semanal.empty:
        idx = b3_long.set_index(["Contrato_Ref", "Data"])["Preco_B3"].sort_index()
        janelas = semanal[["Contrato_Ref", "Semana_Ini", "Semana_Fim"]].drop_duplicates()
        janelas["Preco_B3"] = [
            _media_janela(idx, c, ini, fim) for c, ini, fim in janelas.itertuples(index=False)
        ]
        semanal = semanal.merge(janelas, on=["Contrato_Ref", "Semana_Ini", "Semana_Fim"], how="left")
    else:
        semanal["Preco_B3"] = pd.Series(dtype=float)

    out = pd.concat([diario, semanal], ignore_index=True).dropna(subset=["Preco_B3"])
    out = out.rename(columns={"Preco": "Preco_Fisico"})
    out["Basis"] = (out["Preco_Fisico"] - out["Preco_B3"]).round(2)
    out["Basis_%"] = (out["Basis"] / out["Preco_B3"] * 100).round(2)
    out["Preco_Fisico"] = out["Preco_Fisico"].round(2)
    out["Preco_B3"] = out["Preco_B3"].round(2)

    cols = ["Data", "Cidade", "Preco_Fisico", "Contrato_Ref", "Preco_B3", "Basis", "Basis_%", "Fonte"]
    return out[cols].sort_values(["Data", "Cidade"]).reset_index(drop=True)


# =============================================================================
# CONTROLE DE EXECUÇÃO
# =============================================================================
def ler_meta(sh) -> dict:
    df = ler_df(sh, ABA_META)
    if df.empty or not {"chave", "valor"}.issubset(df.columns):
        return {}
    return dict(zip(df["chave"], df["valor"]))


def salvar_meta(sh, meta: dict) -> None:
    salvar_df(sh, ABA_META, pd.DataFrame({"chave": list(meta), "valor": [str(v) for v in meta.values()]}))


def precisa_atualizar(sh, horas_retry: float = 3) -> bool:
    """
    Atualiza uma vez por dia. Se a Conab falhou na última execução, tenta de novo
    após `horas_retry` horas — sem martelar o servidor a cada visita ao app.
    """
    meta = ler_meta(sh)
    try:
        ultima = datetime.fromisoformat(meta["ultima_execucao"])
    except (KeyError, ValueError):
        return True
    agora = datetime.now(TZ)
    if ultima.date() < agora.date():
        return True
    return meta.get("status_conab") != "ok" and (agora - ultima).total_seconds() > horas_retry * 3600


# =============================================================================
# ORQUESTRAÇÃO
# =============================================================================
@dataclass
class ResultadoAtualizacao:
    conab_ok: bool
    erro_conab: str | None
    semanas_conab_historico: int
    ultima_semana_conab: pd.Timestamp | None
    linhas_basis: int


def atualizar_base_milho(client) -> ResultadoAtualizacao:
    sh = client.open(PLANILHA_FISICO)

    # 1) Conab: upsert no histórico; falha não interrompe o pipeline
    hist = ler_historico_conab(sh)
    try:
        hist = mesclar_historico(hist, baixar_conab_milho())
        salvar_df(sh, ABA_CONAB_HIST, hist)
        conab_ok, erro = True, None
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        log.exception("Falha ao atualizar a Conab — seguindo com o histórico salvo.")
        conab_ok, erro = False, f"{type(exc).__name__}: {exc}"

    # 2) Físico + B3 → bases derivadas
    fisico = montar_fisico(ler_fisico_manual(sh), hist)
    b3 = ler_b3(client)
    base, largo = montar_base_completa(fisico, b3)
    basis = calcular_basis(fisico, b3)

    salvar_df(sh, ABA_FISICO_ATUALIZADO, largo)
    salvar_df(sh, ABA_COMPLETA, base)
    salvar_df(sh, ABA_BASIS, basis)

    ultima_semana = hist["Semana_Fim"].max() if not hist.empty else None
    salvar_meta(sh, {
        "ultima_execucao": datetime.now(TZ).isoformat(timespec="seconds"),
        "status_conab": "ok" if conab_ok else "falha",
        "erro_conab": erro or "",
        "ultima_semana_conab": ultima_semana.strftime("%d/%m/%Y") if ultima_semana is not None else "",
        "linhas_basis": len(basis),
    })

    return ResultadoAtualizacao(conab_ok, erro, len(hist), ultima_semana, len(basis))
