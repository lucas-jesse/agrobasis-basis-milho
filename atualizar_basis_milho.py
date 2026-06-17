import requests
import pandas as pd
import gspread
from io import StringIO
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.service_account import Credentials

CREDENCIAIS = "credenciais.json"

PLANILHA_FISICO = "Preço milho Fisico"
PLANILHA_B3 = "Historico Spread Milho"

ABA_FISICO_ORIGINAL = "Planilha1"
ABA_B3 = "Planilha1"

ABA_FISICO_ATUALIZADO = "fisico_atualizado"
ABA_COMPLETA = "base_milho_completa"
ABA_BASIS = "basis_diario"

URL_CONAB = "https://portaldeinformacoes.conab.gov.br/downloads/arquivos/PrecosSemanalMunicipio.txt"

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

PRECO_MIN = 35
PRECO_MAX = 100

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]


def normalizar_texto(txt):
    return (
        str(txt).upper().strip()
        .replace("Á", "A").replace("À", "A").replace("Â", "A").replace("Ã", "A")
        .replace("É", "E").replace("Ê", "E")
        .replace("Í", "I")
        .replace("Ó", "O").replace("Ô", "O").replace("Õ", "O")
        .replace("Ú", "U")
        .replace("Ç", "C")
    )


def parse_data(valor):
    if pd.isna(valor) or str(valor).strip() == "":
        return pd.NaT

    txt = str(valor).strip()

    formatos = [
        "%d/%m/%Y",
        "%d/%m/%y",
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
    ]

    for fmt in formatos:
        try:
            return pd.to_datetime(datetime.strptime(txt, fmt))
        except Exception:
            pass

    return pd.to_datetime(txt, errors="coerce", dayfirst=True)


def limpar_preco(valor):
    if pd.isna(valor) or str(valor).strip() == "":
        return None

    txt = str(valor).strip().replace("R$", "").replace(" ", "")

    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")

    try:
        numero = float(txt)
    except Exception:
        return None

    if numero > 1000:
        numero = numero / 100
    elif numero > 150:
        numero = numero / 10

    return numero


def contrato_ref(data):
    mes = data.month
    ano = data.year

    if mes in [1, 2]:
        return f"CCMH{str(ano)[-2:]}"
    elif mes in [3, 4]:
        return f"CCMK{str(ano)[-2:]}"
    elif mes in [5, 6]:
        return f"CCMN{str(ano)[-2:]}"
    elif mes in [7, 8]:
        return f"CCMU{str(ano)[-2:]}"
    elif mes in [9, 10]:
        return f"CCMX{str(ano)[-2:]}"
    else:
        return f"CCMF{str(ano + 1)[-2:]}"


def abrir_ou_criar_aba(planilha, nome_aba, rows=1000, cols=30):
    try:
        return planilha.worksheet(nome_aba)
    except Exception:
        return planilha.add_worksheet(title=nome_aba, rows=rows, cols=cols)


def worksheet_para_df(ws):
    valores = ws.get_all_values()
    cabecalho = valores[0]
    dados = valores[1:]
    return pd.DataFrame(dados, columns=cabecalho)


def salvar_dataframe(ws, df):
    df_envio = df.copy()

    if "Data" in df_envio.columns:
        df_envio["Data"] = pd.to_datetime(
            df_envio["Data"],
            errors="coerce"
        ).dt.strftime("%d/%m/%Y")

    df_envio = df_envio.fillna("")

    valores = [df_envio.columns.tolist()] + df_envio.values.tolist()

    ws.clear()
    ws.update(values=valores, range_name="A1")


def baixar_ultima_conab():
    print("Baixando CONAB...")

    r = requests.get(
        URL_CONAB,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=60
    )
    r.raise_for_status()

    texto = r.content.decode("latin1")

    df = pd.read_csv(
        StringIO(texto),
        sep=";",
        decimal=",",
        low_memory=False
    )

    df.columns = df.columns.astype(str).str.strip().str.upper()

    milho = df[
        df["PRODUTO"]
        .astype(str)
        .str.upper()
        .str.contains("MILHO", na=False)
    ].copy()

    milho["UF"] = milho["UF"].astype(str).str.strip().str.upper()

    milho["MUNICIPIO_LIMPO"] = (
        milho["NOM_MUNICIPIO"]
        .astype(str)
        .apply(normalizar_texto)
        .str.replace(r"-[A-Z]{2}$", "", regex=True)
        .str.strip()
    )

    milho["PRECO_KG"] = pd.to_numeric(
        milho["VALOR_PRODUTO_KG"],
        errors="coerce"
    )

    milho["PRECO_SACA"] = milho["PRECO_KG"] * 60

    milho = milho[
        (milho["PRECO_SACA"] >= PRECO_MIN) &
        (milho["PRECO_SACA"] <= PRECO_MAX)
    ]

    linhas = []

    for _, row in milho.iterrows():
        chave = (row["UF"], row["MUNICIPIO_LIMPO"])

        if chave not in MAPA_CIDADES:
            continue

        data_txt = str(row["DATA_INICIAL_FINAL_SEMANA"])
        data_inicio = data_txt.split(" ")[0]
        data = pd.to_datetime(data_inicio, dayfirst=True, errors="coerce")

        if pd.isna(data):
            continue

        linhas.append({
            "Data_CONAB": data,
            "Cidade": MAPA_CIDADES[chave],
            "Preco": row["PRECO_SACA"]
        })

    df_conab = pd.DataFrame(linhas)

    if df_conab.empty:
        print("Nenhum registro CONAB válido encontrado.")
        return pd.DataFrame(columns=["Data_CONAB", "Cidade", "Preco"])

    # Mantém a última cotação disponível por cidade
    df_conab = (
        df_conab
        .sort_values("Data_CONAB")
        .drop_duplicates(["Cidade"], keep="last")
    )

    print("Última CONAB por cidade:")
    print(df_conab)

    return df_conab


# =========================
# EXECUÇÃO
# =========================

print("Conectando ao Google Sheets...")

creds = Credentials.from_service_account_file(CREDENCIAIS, scopes=SCOPES)
client = gspread.authorize(creds)

sh_fisico = client.open(PLANILHA_FISICO)
ws_fisico_original = sh_fisico.worksheet(ABA_FISICO_ORIGINAL)

sh_b3 = client.open(PLANILHA_B3)
ws_b3 = sh_b3.worksheet(ABA_B3)

print("Lendo base física original sem alterar...")
df_fisico = worksheet_para_df(ws_fisico_original)

df_fisico["Data"] = df_fisico["Data"].apply(parse_data)

for col in df_fisico.columns:
    if col != "Data":
        df_fisico[col] = df_fisico[col].apply(limpar_preco)

print("Lendo B3...")
df_b3 = worksheet_para_df(ws_b3)

df_b3["Data"] = df_b3["Data"].apply(parse_data)

for col in df_b3.columns:
    if col != "Data":
        df_b3[col] = df_b3[col].apply(limpar_preco)

print("Linhas físico original:", len(df_fisico))
print("Linhas B3:", len(df_b3))

df_conab = baixar_ultima_conab()

print("Criando base física atualizada em memória...")

df_fisico_atualizado = df_fisico.copy()

data_hoje = pd.to_datetime(
    datetime.now(ZoneInfo("America/Sao_Paulo")).date()
)

existe_hoje = df_fisico_atualizado["Data"].dt.date.eq(data_hoje.date()).any()

if existe_hoje:
    print(f"A data de hoje {data_hoje.strftime('%d/%m/%Y')} já existe. Nenhuma cotação CONAB será inserida.")
else:
    print(f"Adicionando nova linha para {data_hoje.strftime('%d/%m/%Y')} com última CONAB disponível.")

    nova_linha = {col: None for col in df_fisico_atualizado.columns}
    nova_linha["Data"] = data_hoje

    for _, row in df_conab.iterrows():
        cidade = row["Cidade"]
        preco = row["Preco"]

        if cidade in nova_linha:
            nova_linha[cidade] = preco

    df_fisico_atualizado = pd.concat(
        [df_fisico_atualizado, pd.DataFrame([nova_linha])],
        ignore_index=True
    )

df_fisico_atualizado = df_fisico_atualizado.sort_values("Data")

print("Salvando fisico_atualizado...")
ws_fisico_atualizado = abrir_ou_criar_aba(
    sh_fisico,
    ABA_FISICO_ATUALIZADO,
    rows=15000,
    cols=40
)

salvar_dataframe(ws_fisico_atualizado, df_fisico_atualizado)

print("Montando base_milho_completa...")

df_b3 = df_b3.sort_values("Data")

df_base = pd.merge(
    df_fisico_atualizado,
    df_b3,
    on="Data",
    how="left"
)

df_base = df_base.sort_values("Data").ffill()

ws_completa = abrir_ou_criar_aba(
    sh_fisico,
    ABA_COMPLETA,
    rows=15000,
    cols=150
)

salvar_dataframe(ws_completa, df_base)

print("Calculando basis_diario...")

linhas_basis = []
cidades = list(MAPA_CIDADES.values())

for _, row in df_base.iterrows():
    data = row["Data"]

    if pd.isna(data):
        continue

    contrato = contrato_ref(data)

    if contrato not in df_base.columns:
        continue

    preco_b3 = row[contrato]

    if pd.isna(preco_b3) or preco_b3 == 0:
        continue

    for cidade in cidades:
        if cidade not in df_base.columns:
            continue

        preco_fisico = row[cidade]

        if pd.isna(preco_fisico):
            continue

        basis = preco_fisico - preco_b3

        linhas_basis.append({
            "Data": data,
            "Cidade": cidade,
            "Preco_Fisico": round(preco_fisico, 2),
            "Contrato_Ref": contrato,
            "Preco_B3": round(preco_b3, 2),
            "Basis": round(basis, 2),
            "Basis_%": round((basis / preco_b3) * 100, 2)
        })

df_basis = pd.DataFrame(linhas_basis)

ws_basis = abrir_ou_criar_aba(
    sh_fisico,
    ABA_BASIS,
    rows=70000,
    cols=20
)

salvar_dataframe(ws_basis, df_basis)

print("Processo finalizado.")
print(f"Linhas físico original: {len(df_fisico)}")
print(f"Linhas físico atualizado: {len(df_fisico_atualizado)}")
print(f"Linhas basis: {len(df_basis)}")
print("A aba Planilha1 não foi alterada.")
