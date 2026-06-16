import json
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials


st.set_page_config(
    page_title="AgroBasis | Basis do Milho",
    page_icon="🌽",
    layout="wide",
)

PLANILHA = "Preço milho Fisico"
ABA_BASIS = "basis_diario"
ABA_COMPLETA = "base_milho_completa"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CIDADES_COORD = {
    "Santa Rosa RS": (-27.8707, -54.4806),
    "Passo Fundo RS": (-28.2628, -52.4067),
    "Maringá PR": (-23.4205, -51.9331),
    "Ponta Grossa PR": (-25.0994, -50.1583),
    "Sorriso MT": (-12.5425, -55.7211),
    "Rondonópolis MT": (-16.4673, -54.6372),
    "Rio Verde GO": (-17.7923, -50.9192),
    "Itapeva SP": (-23.9822, -48.8756),
    "Vilhena RO": (-12.7414, -60.1386),
}

MESES_CONTRATOS = {"F": 1, "H": 3, "K": 5, "N": 7, "U": 9, "X": 11}

st.markdown(
    """
<style>
.stApp { background-color: #f4f7fb; color: #0f172a; }
.block-container { padding-top: 1.1rem; padding-left: 2rem; padding-right: 2rem; max-width: 1500px; }
.filter-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 18px;
    padding: 16px 18px 6px 18px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.05);
    margin-bottom: 14px;
}
.chart-card {
    background: #ffffff; border: 1px solid #e2e8f0; border-radius: 18px;
    padding: 12px 14px 4px 14px; box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
}
.small-note { color:#64748b; font-size:12px; margin-top:-4px; }
.stTabs [data-baseweb="tab-list"] { gap: 8px; border-bottom: 1px solid #dbe4ee; }
.stTabs [data-baseweb="tab"] {
    background: #ffffff; border: 1px solid #dbe4ee; border-bottom: none;
    border-radius: 12px 12px 0 0; padding: 11px 18px; color: #334155; font-weight: 700;
}
.stTabs [aria-selected="true"] { background: #14532d; color: white; }
.stSelectbox label, .stMultiSelect label, .stSlider label, .stRadio label { color: #334155 !important; font-weight: 700; }
div[data-baseweb="select"] > div { background-color: #ffffff; border-radius: 12px; border-color: #cbd5e1; }
button[kind="secondary"] { border-radius: 12px; }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def conectar_google():
    try:
        credenciais_json = st.secrets.get("GOOGLE_CREDENTIALS", None)
    except Exception:
        credenciais_json = None

    if credenciais_json:
        credenciais = json.loads(credenciais_json)
        creds = Credentials.from_service_account_info(credenciais, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credenciais.json", scopes=SCOPES)

    return gspread.authorize(creds)


def parse_numero(valor):
    if valor is None:
        return pd.NA
    txt = str(valor).strip()
    if txt == "":
        return pd.NA
    txt = txt.replace("R$", "").replace("%", "").replace(" ", "")
    if "," in txt:
        txt = txt.replace(".", "").replace(",", ".")
    try:
        return float(txt)
    except Exception:
        return pd.NA


def ler_aba(nome_aba):
    client = conectar_google()
    ws = client.open(PLANILHA).worksheet(nome_aba)
    valores = ws.get_all_values()
    if not valores or len(valores) < 2:
        return pd.DataFrame()
    return pd.DataFrame(valores[1:], columns=valores[0])


@st.cache_data(ttl=900)
def carregar_basis():
    df = ler_aba(ABA_BASIS)
    if df.empty:
        return df

    df["Data"] = pd.to_datetime(df["Data"], dayfirst=True, errors="coerce")
    for col in ["Preco_Fisico", "Preco_B3", "Basis", "Basis_%"]:
        if col in df.columns:
            df[col] = df[col].apply(parse_numero)
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["Data", "Cidade", "Basis"])
    df["Ano"] = df["Data"].dt.year.astype(int)
    df["DOY"] = df["Data"].dt.dayofyear.astype(int)
    df["Mes"] = df["Data"].dt.month.astype(int)
    df["MesDia"] = df["Data"].dt.strftime("%d/%m")
    return df.sort_values("Data")


@st.cache_data(ttl=900)
def carregar_base_completa():
    df = ler_aba(ABA_COMPLETA)
    if df.empty:
        return df

    df["Data"] = pd.to_datetime(df["Data"], dayfirst=True, errors="coerce")
    for col in df.columns:
        if col != "Data":
            df[col] = df[col].apply(parse_numero)
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["Data"]).sort_values("Data")


def media_5_anos(df, cidade, ano_base, janela=11):
    anos_hist = list(range(ano_base - 5, ano_base))
    hist = df[(df["Cidade"] == cidade) & (df["Ano"].isin(anos_hist))].copy()
    if hist.empty:
        return pd.DataFrame(columns=["DOY", "Media_5a"])

    media = (
        hist.groupby("DOY", as_index=False)["Basis"]
        .mean()
        .rename(columns={"Basis": "Media_5a"})
        .sort_values("DOY")
    )
    media["Media_5a"] = media["Media_5a"].rolling(window=janela, center=True, min_periods=1).mean()
    return media


def serie_ano(df, cidade, ano, suavizar=1):
    d = df[(df["Cidade"] == cidade) & (df["Ano"] == ano)].copy().sort_values("DOY")
    if suavizar and suavizar > 1:
        d["Basis_plot"] = d["Basis"].rolling(window=suavizar, center=True, min_periods=1).mean()
    else:
        d["Basis_plot"] = d["Basis"]
    return d


def grafico_basis(df, cidade, anos, suavizar_atual, suavizar_media):
    ano_base = max(anos)
    fig = go.Figure()
    palette = {
        ano_base: "#14532d",
        ano_base - 1: "#2563eb",
        ano_base - 2: "#f97316",
        ano_base - 3: "#7c3aed",
        ano_base - 4: "#0f766e",
        ano_base - 5: "#64748b",
    }

    for ano in sorted(anos):
        d = serie_ano(df, cidade, ano, suavizar_atual)
        if d.empty:
            continue
        fig.add_trace(go.Scatter(
            x=d["DOY"], y=d["Basis_plot"], mode="lines", name=str(ano),
            line=dict(width=3.5 if ano == ano_base else 2, color=palette.get(ano, None)),
            connectgaps=False,
            hovertemplate="Ano: %{customdata[0]}<br>Data: %{customdata[1]}<br>Basis: R$ %{y:.2f}/sc<extra></extra>",
            customdata=d[["Ano", "MesDia"]].values,
        ))

    media = media_5_anos(df, cidade, ano_base, janela=suavizar_media)
    if not media.empty:
        fig.add_trace(go.Scatter(
            x=media["DOY"], y=media["Media_5a"], mode="lines",
            name=f"Média {ano_base-5} - {ano_base-1}",
            line=dict(width=3, dash="dot", color="#d97706"),
            hovertemplate="Média 5 anos<br>Dia do ano: %{x}<br>Basis: R$ %{y:.2f}/sc<extra></extra>",
        ))

    cortes = [(60, "Mar / K"), (121, "Mai / N"), (182, "Jul / U"), (244, "Set / X"), (305, "Nov / F")]
    for x, label in cortes:
        fig.add_vline(x=x, line_width=1.2, line_dash="dash", line_color="rgba(15, 23, 42, 0.45)")
        fig.add_annotation(x=x, y=1.05, yref="paper", text=label, showarrow=False, font=dict(color="#334155", size=11))

    fig.add_hline(y=0, line_width=1.1, line_color="rgba(15, 23, 42, 0.45)")

    tickvals = [1, 32, 60, 91, 121, 152, 182, 213, 244, 274, 305, 335]
    ticktext = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

    fig.update_layout(
        template="plotly_white",
        title={"text": f"{cidade} × B3", "x": 0.5, "xanchor": "center", "y": 0.97, "yanchor": "top", "font": {"size": 19, "color": "#0f172a"}},
        height=650,
        paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", font=dict(color="#0f172a"),
        legend=dict(
            orientation="h",
            y=1.18,
            x=0.98,
            xanchor="right",
            yanchor="top",
            bgcolor="rgba(255,255,255,0.85)",
            bordercolor="rgba(226,232,240,0.8)",
            borderwidth=1,
        ),
        margin=dict(l=65, r=30, t=145, b=70),
        xaxis=dict(title="", tickmode="array", tickvals=tickvals, ticktext=ticktext, showgrid=False, range=[1, 366]),
        yaxis=dict(title="R$/sc", gridcolor="rgba(148, 163, 184, 0.30)", zeroline=False),
    )

    fig.add_annotation(
        text="AgroBasis", xref="paper", yref="paper", x=0.50, y=0.52,
        showarrow=False, font=dict(size=82, color="rgba(15, 23, 42, 0.055)"), textangle=-18,
    )
    return fig


def resumo_atual(df):
    ultima_data = df["Data"].max()
    atual = df[df["Data"] == ultima_data].copy()
    return atual, ultima_data


def grafico_mapa(df):
    atual, ultima_data = resumo_atual(df)
    mapa = atual.copy()
    mapa["lat"] = mapa["Cidade"].map(lambda x: CIDADES_COORD.get(x, (None, None))[0])
    mapa["lon"] = mapa["Cidade"].map(lambda x: CIDADES_COORD.get(x, (None, None))[1])
    mapa = mapa.dropna(subset=["lat", "lon"])

    fig = go.Figure()
    fig.add_trace(go.Scattergeo(
        lon=mapa["lon"], lat=mapa["lat"], text=mapa["Cidade"],
        customdata=mapa[["Basis", "Preco_Fisico", "Preco_B3", "Contrato_Ref"]],
        mode="markers+text", textposition="top center",
        marker=dict(
            size=18, color=mapa["Basis"], colorscale="RdYlGn",
            colorbar=dict(title="Basis<br>R$/sc"), line=dict(width=1, color="#0f172a"),
        ),
        hovertemplate="<b>%{text}</b><br>Basis: R$ %{customdata[0]:.2f}/sc<br>Físico: R$ %{customdata[1]:.2f}/sc<br>B3: R$ %{customdata[2]:.2f}/sc<br>Contrato: %{customdata[3]}<extra></extra>",
    ))
    fig.update_geos(
        scope="south america", projection_type="mercator", showcountries=True,
        countrycolor="#cbd5e1", showland=True, landcolor="#f8fafc", showocean=True,
        oceancolor="#e0f2fe", lataxis_range=[-35, 6], lonaxis_range=[-75, -32],
    )
    fig.update_layout(
        template="plotly_white",
        title={"text": f"Basis atual por município — {ultima_data.strftime('%d/%m/%Y')}", "x": 0.5, "xanchor": "center", "font": {"size": 18}},
        height=650, margin=dict(l=10, r=10, t=70, b=10), paper_bgcolor="#ffffff",
    )
    return fig


def contrato_para_data(codigo):
    m = re.match(r"CCM([FHKNUX])(\d{2})$", str(codigo))
    if not m:
        return None
    letra = m.group(1)
    ano = 2000 + int(m.group(2))
    mes = MESES_CONTRATOS.get(letra)
    if mes is None:
        return None
    return pd.Timestamp(year=ano, month=mes, day=15)


def curva_b3_interpolada(df_base):
    if df_base.empty:
        return pd.DataFrame()
    contratos = [c for c in df_base.columns if re.match(r"^CCM[FHKNUX]\d{2}$", str(c))]
    if not contratos:
        return pd.DataFrame()

    ultima_data = df_base["Data"].max()
    curvas = []
    for contrato in contratos:
        venc = contrato_para_data(contrato)
        if venc is None:
            continue
        serie = df_base[["Data", contrato]].dropna()
        if serie.empty:
            continue
        preco = serie.sort_values("Data")[contrato].iloc[-1]
        if pd.isna(preco):
            continue
        curvas.append({"Contrato": contrato, "Vencimento": venc, "Preco_B3": float(preco)})

    curva = pd.DataFrame(curvas).sort_values("Vencimento")
    if curva.empty:
        return curva

    data_ref = pd.Timestamp(ultima_data.year, ultima_data.month, 1)
    curva = curva[curva["Vencimento"] >= data_ref].copy()
    if len(curva) < 2:
        return curva

    meses = pd.date_range(start=data_ref, end=curva["Vencimento"].max(), freq="MS")
    x_contratos = curva["Vencimento"].map(pd.Timestamp.toordinal).to_numpy()
    y_contratos = curva["Preco_B3"].to_numpy()
    x_meses = meses.map(pd.Timestamp.toordinal).to_numpy()
    precos_interp = np.interp(x_meses, x_contratos, y_contratos)
    return pd.DataFrame({"Data": meses, "Preco_B3_Interpolado": precos_interp})


def basis_mensal_referencia(df, cidade, ano_ref, tipo_basis):
    anos_hist = list(range(ano_ref - 5, ano_ref))
    hist = df[(df["Cidade"] == cidade) & (df["Ano"].isin(anos_hist))].copy()
    if hist.empty:
        return pd.DataFrame(columns=["Mes", "Basis_Ref"])

    agg_map = {"Basis médio": "mean", "Basis mínimo": "min", "Basis máximo": "max"}
    metodo = agg_map.get(tipo_basis, "mean")
    base = hist.groupby("Mes", as_index=False)["Basis"].agg(metodo).rename(columns={"Basis": "Basis_Ref"}).sort_values("Mes")
    base["Basis_Ref"] = base["Basis_Ref"].rolling(3, center=True, min_periods=1).mean()
    return base


def grafico_preco_futuro(df_basis, df_base, cidade, tipo_basis):
    curva = curva_b3_interpolada(df_base)
    if curva.empty:
        return None

    ano_ref = int(df_basis["Ano"].max())
    ref = basis_mensal_referencia(df_basis, cidade, ano_ref, tipo_basis)
    if ref.empty:
        return None

    curva["Mes"] = curva["Data"].dt.month
    curva = curva.merge(ref, on="Mes", how="left")
    curva["Basis_Ref"] = curva["Basis_Ref"].ffill().bfill()
    curva["Preco_Futuro_Cidade"] = curva["Preco_B3_Interpolado"] + curva["Basis_Ref"]
    curva["Mes_Label"] = curva["Data"].dt.strftime("%b/%y")

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=curva["Mes_Label"],
        y=curva["Preco_Futuro_Cidade"],
        name="Preço teórico",
        marker=dict(
            color=curva["Preco_Futuro_Cidade"],
            colorscale=[
                [0.0, "#dbeafe"],
                [0.45, "#60a5fa"],
                [1.0, "#14532d"],
            ],
            line=dict(color="rgba(15, 23, 42, 0.18)", width=1),
        ),
        text=[f"R$ {v:.2f}" for v in curva["Preco_Futuro_Cidade"]],
        textposition="outside",
        textfont=dict(color="#0f172a", size=12),
        hovertemplate="Mês: %{x}<br>Preço teórico: R$ %{y:.2f}/sc<extra></extra>",
    ))

    fig.add_trace(go.Scatter(
        x=curva["Mes_Label"],
        y=curva["Preco_Futuro_Cidade"],
        mode="lines",
        name="Tendência",
        line=dict(color="#0f172a", width=2, shape="spline"),
        hoverinfo="skip",
        showlegend=False,
    ))
    fig.update_layout(
        template="plotly_white",
        title={
            "text": f"Preço teórico mensal do milho — {cidade}",
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": 20, "color": "#0f172a"},
        },
        height=650, paper_bgcolor="#ffffff", plot_bgcolor="#ffffff", font=dict(color="#0f172a"),
        showlegend=False,
        bargap=0.28,
        margin=dict(l=65, r=30, t=105, b=70),
        yaxis=dict(
            title="R$/sc",
            gridcolor="rgba(148, 163, 184, 0.25)",
            zeroline=False,
        ),
        xaxis=dict(
            title="",
            showgrid=False,
            tickfont=dict(size=12),
        ),
    )
    fig.add_annotation(
        text="AgroBasis", xref="paper", yref="paper", x=0.50, y=0.52,
        showarrow=False, font=dict(size=82, color="rgba(15, 23, 42, 0.055)"), textangle=-18,
    )
    return fig


try:
    df = carregar_basis()
except Exception as e:
    st.error(f"Erro ao carregar dados: {e}")
    st.stop()

if df.empty:
    st.warning("A aba basis_diario está vazia.")
    st.stop()

cidades = sorted(df["Cidade"].dropna().unique())
anos_disponiveis = sorted(df["Ano"].dropna().unique().astype(int).tolist())

st.markdown('<div class="filter-card">', unsafe_allow_html=True)
col1, col2, col3, col4, col5 = st.columns([1.8, 1.6, 1.1, 1.1, 1.1])

with col1:
    cidade_sel = st.selectbox(
        "Localidade",
        cidades,
        index=cidades.index("Santa Rosa RS") if "Santa Rosa RS" in cidades else 0,
    )

with col2:
    default_anos = anos_disponiveis[-2:] if len(anos_disponiveis) >= 2 else anos_disponiveis
    anos_sel = st.multiselect("Anos exibidos", anos_disponiveis, default=default_anos)

with col3:
    suavizar_atual = st.slider("Suavização ano", min_value=1, max_value=10, value=1, step=1)

with col4:
    suavizar_media = st.slider("Suavização média", min_value=3, max_value=31, value=11, step=2)

with col5:
    st.write("")
    st.write("")
    if st.button("Atualizar"):
        st.cache_data.clear()
        st.cache_resource.clear()
        st.rerun()

st.markdown('</div>', unsafe_allow_html=True)

if not anos_sel:
    st.warning("Selecione pelo menos um ano.")
    st.stop()

tab_basis, tab_mapa, tab_futuro = st.tabs(["📈 Basis", "🗺️ Mapa", "🌽 Preço futuro"])

with tab_basis:
    st.markdown('<div class="chart-card">', unsafe_allow_html=True)
    fig = grafico_basis(df=df, cidade=cidade_sel, anos=anos_sel, suavizar_atual=suavizar_atual, suavizar_media=suavizar_media)
    st.plotly_chart(fig, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

with tab_mapa:
    st.markdown('<div class="chart-card">', unsafe_allow_html=True)
    fig_mapa = grafico_mapa(df)
    st.plotly_chart(fig_mapa, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

with tab_futuro:
    try:
        df_base = carregar_base_completa()
    except Exception as e:
        st.error(f"Erro ao carregar a aba base_milho_completa: {e}")
        st.stop()

    tipo_basis = st.radio(
        "Referência de basis para composição do preço futuro",
        ["Basis médio", "Basis mínimo", "Basis máximo"],
        horizontal=True,
    )
    st.markdown(
        "<div class='small-note'>Preço teórico mensal = curva B3 interpolada + referência mensal de basis da localidade selecionada.</div>",
        unsafe_allow_html=True,
    )

    st.markdown('<div class="chart-card">', unsafe_allow_html=True)
    fig_futuro = grafico_preco_futuro(df_basis=df, df_base=df_base, cidade=cidade_sel, tipo_basis=tipo_basis)
    if fig_futuro is None:
        st.warning("Não foi possível montar a curva futura. Verifique se a aba base_milho_completa possui contratos CCM futuros.")
    else:
        st.plotly_chart(fig_futuro, use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)
