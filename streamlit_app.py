"""
streamlit_app.py

Interface WEB para o motor de análise em `daytrade_smc.py`. Não altera
nada do motor — só importa as funções e desenha por cima.

Dois modos (barra lateral):
    - Análise individual: gráfico de candles com EMAs/VWAP/swings/BOS-CHoCH/
      zonas de FVG, mais os painéis das 5 leituras. Pode auto-atualizar.
    - Scanner: roda a análise em TODOS os ativos da watchlist de uma vez e
      mostra um ranking por score de confluência (o "Top N" do requisito
      original), com atalho pra abrir qualquer um na análise individual.

Rodar localmente (se algum dia tiver Python disponível):
    streamlit run streamlit_app.py

Rodar pela internet sem instalar nada: ver README.md.
"""

from __future__ import annotations

import os
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import daytrade_smc
from daytrade_smc import (
    ALL_MODALITIES_OPTION,
    AnalysisParams,
    DATA_SOURCES,
    DAYTRADE_CONFIRMATION_TIMEFRAMES,
    DAYTRADE_CONTEXT_TIMEFRAMES,
    DEFAULT_PARAMS,
    DEFAULT_PROFILE_NAME,
    DEFAULT_SYMBOLS,
    Direction,
    MODALITY_CHOICES,
    Signal,
    SWING_CONFIRMATION_TIMEFRAMES,
    SWING_CONTEXT_TIMEFRAMES,
    analyze_symbol_mtf,
    check_signal_as_of,
    delete_profile,
    fetch_snapshot_timestamp,
    load_profiles,
    load_symbols,
    overall_agreement,
    overall_direction,
    overall_score,
    quality,
    save_profile,
    save_symbols,
    trigger_github_update,
    yahoo_symbol,
)

# Configura a ponte GitHub a partir dos secrets do Streamlit (Settings
# → Secrets no Streamlit Cloud, ou .streamlit/secrets.toml localmente).
# Sem isso configurado, a fonte "GitHub (MT5 de casa)" dá erro claro em
# vez de travar — ver README.
try:
    daytrade_smc.GITHUB_BRIDGE_REPO = st.secrets.get("github_repo")
    daytrade_smc.GITHUB_BRIDGE_TOKEN = st.secrets.get("github_token")
except Exception:
    daytrade_smc.GITHUB_BRIDGE_REPO = None
    daytrade_smc.GITHUB_BRIDGE_TOKEN = None

# Configura a API do homelab (serviço `api`, que serve o TimescaleDB
# alimentado pelo acoes-scraper). O fallback em variável de ambiente existe
# porque o container no k3s não popula st.secrets a menos que monte um
# secrets.toml — sem nenhum dos dois configurado, a fonte "Homelab (API)"
# dá erro claro em vez de travar, mesmo padrão da ponte GitHub.
#
# Note que aqui não há mais DSN nenhum: este processo não fala SQL, só HTTP.
try:
    daytrade_smc.ACOES_API_URL = st.secrets.get("acoes_api_url") or os.environ.get("ACOES_API_URL")
    daytrade_smc.ACOES_API_KEY = st.secrets.get("acoes_api_key") or os.environ.get("ACOES_API_KEY")
except Exception:
    daytrade_smc.ACOES_API_URL = os.environ.get("ACOES_API_URL")
    daytrade_smc.ACOES_API_KEY = os.environ.get("ACOES_API_KEY")

STYLES = {
    "Day Trade": {
        "confirmation": DAYTRADE_CONFIRMATION_TIMEFRAMES,
        "context": DAYTRADE_CONTEXT_TIMEFRAMES,
        "count_label": "Candles fechados (M15 e H1)",
    },
    "Swing Trade": {
        "confirmation": SWING_CONFIRMATION_TIMEFRAMES,
        "context": SWING_CONTEXT_TIMEFRAMES,
        "count_label": "Candles fechados (Diário e Semanal)",
    },
}

st.set_page_config(page_title="Day Trade SMC", page_icon="📊", layout="wide")

DIRECTION_COLOR = {
    Direction.BUY: "#2ed3a3",
    Direction.SELL: "#ff5470",
    Direction.NEUTRAL: "#8291a1",
}

SOURCE_LABELS = {
    "Yahoo Finance": "Yahoo Finance (atraso ~15-20min)",
    "MetaTrader 5": "MT5 (tempo real)",
    "GitHub (MT5 de casa)": "GitHub (MT5 de casa)",
    "Homelab (API)": "Homelab (API, quase em tempo real)",
}


# ========================================================================
# Dados / cache / análise
# ========================================================================
@st.cache_data(ttl=60, show_spinner=False)
def _cached_mtf_yahoo(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="Yahoo Finance", params=AnalysisParams.from_items(params_items))


@st.cache_data(ttl=3, show_spinner=False)
def _cached_mtf_mt5(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="MetaTrader 5", params=AnalysisParams.from_items(params_items))


@st.cache_data(ttl=10, show_spinner=False)
def _cached_mtf_github(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="GitHub (MT5 de casa)", params=AnalysisParams.from_items(params_items))


@st.cache_data(ttl=3, show_spinner=False)
def _cached_mtf_api(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="Homelab (API)", params=AnalysisParams.from_items(params_items))


def cached_mtf(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, source: str, params: AnalysisParams = DEFAULT_PARAMS):
    """
    Cacheia o pacote de timeframes. Yahoo Finance usa 60s de cache (tem
    rate limit); MetaTrader 5 direto e Homelab (API) usam 3s (dado
    quase em tempo real nos dois casos); GitHub (MT5 de casa) usa 10s (só
    muda quando você clica em "Atualizar via MT5", então não precisa ser
    tão curto). Funções fixas em vez de decoradas dinamicamente, pelo
    mesmo motivo dos fragmentos de auto-refresh: evita o bug de identidade
    de widget no React já corrigido antes neste projeto.

    Os parâmetros de análise entram na chave de cache como TUPLA DE PARES
    (`params.to_items()`), nunca como o dataclass e nunca por variável
    global. O hasher do `st.cache_data` garante tuplas de primitivos; um
    dataclass ou levanta `UnhashableParamError` na hora, ou — pior — é
    hasheado por identidade e a falha fica silenciosa: trocar de perfil
    continuaria servindo os scores do perfil anterior pelo TTL inteiro, e
    o Scanner ranquearia por eles sem nenhum sinal de que algo está errado.
    """
    params_items = params.to_items()
    if source == "Homelab (API)":
        return _cached_mtf_api(symbol, count, confirmation, context, modality, params_items)
    if source == "MetaTrader 5":
        return _cached_mtf_mt5(symbol, count, confirmation, context, modality, params_items)
    if source == "GitHub (MT5 de casa)":
        return _cached_mtf_github(symbol, count, confirmation, context, modality, params_items)
    return _cached_mtf_yahoo(symbol, count, confirmation, context, modality, params_items)


def find_fvg_zone(df: pd.DataFrame, max_age: int = DEFAULT_PARAMS.fvg_max_idade) -> dict | None:
    """
    Replica a lógica de `detect_fvg_setup` (do seu motor original), mas
    devolve os PREÇOS do gap em vez de só um texto — usado unicamente
    para desenhar a zona no gráfico. Não influencia nenhum score; a
    decisão de score continua 100% dentro de `daytrade_smc.py`.
    """
    current_price = float(df["close"].iloc[-1])
    tolerance = current_price * 0.0015
    first = max(1, len(df) - max_age)

    for middle in range(len(df) - 2, first - 1, -1):
        candle_1 = df.iloc[middle - 1]
        candle_3 = df.iloc[middle + 1]

        if candle_1["high"] < candle_3["low"]:
            bottom, top = float(candle_1["high"]), float(candle_3["low"])
            filled = bool((df["low"].iloc[middle + 2:] <= bottom).any())
            near = bottom - tolerance <= current_price <= top + tolerance
            if not filled and near:
                return {"kind": "ALTA", "bottom": bottom, "top": top, "start_idx": middle - 1}

        if candle_1["low"] > candle_3["high"]:
            bottom, top = float(candle_3["high"]), float(candle_1["low"])
            filled = bool((df["high"].iloc[middle + 2:] >= top).any())
            near = bottom - tolerance <= current_price <= top + tolerance
            if not filled and near:
                return {"kind": "BAIXA", "bottom": bottom, "top": top, "start_idx": middle - 1}

    return None


# ========================================================================
# Gráfico
# ========================================================================
def build_chart(context, active_signal: Signal | None, symbol: str) -> go.Figure:
    df = context.df
    # BUG CORRIGIDO: os candles vêm do Yahoo em UTC (fetch_ohlcv normaliza
    # tudo pra UTC internamente), mas estavam sendo plotados sem converter
    # pro horário de Brasília — isso deixava CADA candle 3 horas adiantado
    # em relação ao gráfico real, um erro sistêmico que afeta todo ativo,
    # mais visível em M15 (equivale a 12 candles de deslocamento).
    x = df.index.tz_convert("America/Sao_Paulo")

    fig = go.Figure()

    fig.add_trace(
        go.Candlestick(
            x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name=symbol, increasing_line_color="#2ed3a3", decreasing_line_color="#ff5470",
            increasing_fillcolor="#2ed3a3", decreasing_fillcolor="#ff5470",
        )
    )

    ema_colors = {"ema_9": "#5ec8ff", "ema_21": "#a78bfa", "ema_50": "#f0b429", "ema_200": "#ff8a3d"}
    for col, color in ema_colors.items():
        fig.add_trace(
            go.Scatter(x=x, y=context.emas[col], mode="lines", name=col.upper().replace("_", " "),
                       line=dict(color=color, width=1.3))
        )

    fig.add_trace(
        go.Scatter(x=x, y=context.vwap_series, mode="lines", name="VWAP",
                   line=dict(color="#2ed3a3", width=1.6, dash="dot"))
    )

    swing_highs = [(x[s.index], s.price) for s in context.swings if s.kind == "HIGH"]
    swing_lows = [(x[s.index], s.price) for s in context.swings if s.kind == "LOW"]
    if swing_highs:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in swing_highs], y=[p[1] for p in swing_highs], mode="markers",
            name="Swing High", marker=dict(symbol="triangle-down", size=7, color="#ff5470"),
        ))
    if swing_lows:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in swing_lows], y=[p[1] for p in swing_lows], mode="markers",
            name="Swing Low", marker=dict(symbol="triangle-up", size=7, color="#2ed3a3"),
        ))

    for kind, symb, color in [("BOS", "diamond", "#f0b429"), ("CHOCH", "star", "#ffffff")]:
        pts = [e for e in context.events if e.kind == kind]
        if not pts:
            continue
        fig.add_trace(go.Scatter(
            x=[x[e.index] for e in pts],
            y=[df["high"].iloc[e.index] * 1.003 if e.direction == Direction.BUY else df["low"].iloc[e.index] * 0.997 for e in pts],
            mode="markers+text", name=kind,
            marker=dict(symbol=symb, size=11, color=color, line=dict(width=1, color="#0a0e13")),
            text=[kind] * len(pts), textposition="top center", textfont=dict(size=9, color=color),
        ))

    # O mesmo parâmetro que o motor usou pra DECIDIR o FVG precisa valer aqui
    # pra DESENHAR — senão o gráfico mostra uma zona que o score não enxerga.
    fvg = find_fvg_zone(df, context.params.fvg_max_idade)
    if fvg is not None:
        color = "#2ed3a3" if fvg["kind"] == "ALTA" else "#ff5470"
        fig.add_shape(
            type="rect", xref="x", yref="y",
            x0=x[fvg["start_idx"]], x1=x[-1],
            y0=fvg["bottom"], y1=fvg["top"],
            fillcolor=color, opacity=0.12, line=dict(width=1, color=color, dash="dot"),
        )
        fig.add_annotation(
            x=x[fvg["start_idx"]], y=fvg["top"], text=f"FVG {fvg['kind']}",
            showarrow=False, font=dict(size=9, color=color), xanchor="left", yanchor="bottom",
        )

    if active_signal is not None and active_signal.risk.entry is not None:
        r = active_signal.risk
        levels = [("Entrada", r.entry, "#e7ecf1"), ("Stop", r.stop, "#ff5470"),
                  ("Alvo 1", r.target_1, "#2ed3a3"), ("Alvo 2", r.target_2, "#2ed3a3")]
        for label, price, color in levels:
            if price is None:
                continue
            fig.add_hline(y=price, line=dict(color=color, width=1.4, dash="solid" if label != "Alvo 2" else "dash"),
                          annotation_text=f"{label}: {price:.2f}", annotation_position="right",
                          annotation=dict(font=dict(size=10, color=color)))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0a0e13", plot_bgcolor="#0a0e13",
        height=560, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        font=dict(family="IBM Plex Mono, monospace", size=11, color="#8291a1"),
    )
    return fig


# ========================================================================
# Painéis
# ========================================================================
def render_signal_panel(signal: Signal, symbol: str, risk_budget: float | None, timeframe: str = "", context=None, mtf=None, params: AnalysisParams = DEFAULT_PARAMS, perfil: str = DEFAULT_PROFILE_NAME) -> None:
    color = DIRECTION_COLOR[signal.direction]
    q = quality(signal.score, params)

    if q == "OPORTUNIDADE EXCEPCIONAL" and signal.direction != Direction.NEUTRAL:
        action = "COMPRA" if signal.direction == Direction.BUY else "VENDA"
        st.markdown(
            f'<div style="border:2px solid #f0b429; border-radius:8px; padding:10px 16px; '
            f'background:#f0b42922; margin-bottom:12px; text-align:center;">'
            f'<span style="font-size:18px;">🌟 <b style="color:#f0b429;">OPORTUNIDADE EXCEPCIONAL</b> · '
            f'{action} · score {signal.score:.1f}/100</span>'
            f'</div>',
            unsafe_allow_html=True,
        )

    c1, c2, c3 = st.columns([2, 1, 1])
    c1.markdown(f"### {signal.setup}")
    c2.metric("Direção", signal.direction.value)
    c3.metric("Score", f"{signal.score:.1f}/100", q)

    risk = signal.risk
    if signal.direction == Direction.NEUTRAL or risk.entry is None:
        st.info("Sem sinal operável nesta leitura — entrada, stop e alvos foram bloqueados.")
    else:
        action = "COMPRAR" if signal.direction == Direction.BUY else "VENDER"
        st.markdown(
            f"> **{action} {symbol}** perto de **R$ {risk.entry:.2f}**, stop em "
            f"**R$ {risk.stop:.2f}**, alvo em **R$ {risk.target_1:.2f}** (R/R 1:{risk.rr:.2f})."
        )

        cols = st.columns(4)
        cols[0].metric("Entrada", f"R$ {risk.entry:.2f}")
        cols[1].metric("Stop", f"R$ {risk.stop:.2f}", f"{(risk.stop-risk.entry)/risk.entry*100:+.2f}%")
        cols[2].metric("Alvo 1", f"R$ {risk.target_1:.2f}", f"{(risk.target_1-risk.entry)/risk.entry*100:+.2f}%")
        cols[3].metric("Alvo 2", f"R$ {risk.target_2:.2f}", f"{(risk.target_2-risk.entry)/risk.entry*100:+.2f}%")

        st.caption(f"Risco por ação: R$ {abs(risk.entry-risk.stop):.2f} · Base do stop: {risk.stop_basis}")

        if risk_budget:
            risk_per_share = abs(risk.entry - risk.stop)
            if risk_per_share > 0:
                qty = int(risk_budget // risk_per_share)
                st.caption(f"Com risco de R$ {risk_budget:.2f}: **{qty} ações** · "
                          f"risco real R$ {qty*risk_per_share:.2f} · total R$ {qty*risk.entry:.2f}")

        if risk.alternatives:
            st.markdown("**Possibilidades de saída:**")
            st.dataframe(
                [{"Método": t["method"], "Preço": round(t["price"], 2), "R/R": round(t["rr"], 2),
                  "Status": "Viável" if t["viable"] else "Fraco"} for t in risk.alternatives],
                hide_index=True, use_container_width=True,
            )

    with st.expander("Motivos e alertas"):
        for reason in signal.reasons:
            st.write(f"- {reason}")
        for alert in dict.fromkeys(signal.alerts):
            st.warning(alert)

    if context is not None and daytrade_smc.ACOES_API_URL:
        # O auto-refresh NÃO dispara este botão: `st.button` só devolve True
        # no rerun do clique, então o fragmento de 30s renderiza o botão sem
        # nunca ativá-lo. Não "conserte" isso pra uma chamada incondicional —
        # viraria uma linha nova a cada 30 segundos.
        #
        # A garantia contra clique duplo, essa sim, é o índice único no
        # banco: (symbol, timeframe, modalidade, candle_time, perfil, origem).
        if st.button("💾 Salvar sinal", key=f"salvar_{symbol}_{timeframe}_{signal.name}"):
            # A confirmação gravada é a DESTA leitura, não a da modalidade
            # selecionada na sidebar: o painel mostra as cinco, e reaproveitar
            # a confirmação de outra falsearia o recorte de assertividade.
            confirmado, direcao_mtf = (False, Direction.NEUTRAL)
            if mtf is not None:
                confirmado, direcao_mtf = daytrade_smc.mtf_confirmation(
                    {tf: r.signals for tf, r in mtf.results.items()},
                    STYLES[st.session_state.style_select]["confirmation"],
                    signal.name,
                )
            try:
                resultado = daytrade_smc.save_signal(
                    daytrade_smc.signal_payload(
                        symbol, timeframe, signal, context, perfil, params, "manual",
                        confirmado, direcao_mtf,
                    )
                )
            except Exception as exc:
                st.error(f"Não foi possível salvar: {exc}")
            else:
                if resultado.get("duplicado"):
                    st.info("Este sinal já estava salvo (mesma vela, mesma leitura).")
                else:
                    st.success(f"Sinal salvo — {signal.name} de {symbol} em {timeframe}.")


TIMEFRAME_LABELS = {
    "M15": "15 minutos",
    "H1": "60 minutos",
    "H4": "240 minutos",
    "D1": "Diário",
    "W1": "Semanal",
}


def render_confirmation_badge(mtf, confirmation: tuple[str, str], params: AnalysisParams = DEFAULT_PARAMS) -> None:
    tf_a, tf_b = confirmation
    result_a = mtf.results[tf_a]
    result_b = mtf.results[tf_b]

    if result_a.error or result_b.error:
        st.warning(
            f"⚠️ Não foi possível confirmar — falha ao buscar {tf_a} e/ou {tf_b}. "
            f"{result_a.error or ''} {result_b.error or ''}".strip()
        )
        return

    if mtf.modality == ALL_MODALITIES_OPTION:
        dir_a = overall_direction(result_a.signals)
        dir_b = overall_direction(result_b.signals)
        agree_a, total_a = overall_agreement(result_a.signals)
        agree_b, total_b = overall_agreement(result_b.signals)
        agreement_note = f" ({tf_a}: {agree_a}/{total_a} leituras concordam · {tf_b}: {agree_b}/{total_b})"
    else:
        dir_a = next(s.direction for s in result_a.signals if s.name == mtf.modality)
        dir_b = next(s.direction for s in result_b.signals if s.name == mtf.modality)
        agreement_note = ""

    if mtf.confirmed:
        color = DIRECTION_COLOR[mtf.confirmed_direction]
        if mtf.modality == ALL_MODALITIES_OPTION:
            score_for_badge = overall_score(result_a.signals)
        else:
            score_for_badge = next(s.score for s in result_a.signals if s.name == mtf.modality)
        star = "🌟 " if quality(score_for_badge, params) == "OPORTUNIDADE EXCEPCIONAL" else ""
        st.markdown(
            f'<div style="border:1px solid {color}; border-radius:8px; padding:12px 16px; '
            f'background:{color}18; margin-bottom:14px;">'
            f'{star}✅ <b style="color:{color}">CONFIRMADO: {mtf.confirmed_direction.value}</b> — '
            f"{tf_a} e {tf_b} concordam na mesma direção, segundo a leitura <b>{mtf.modality}</b>."
            f"{agreement_note}"
            f"</div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f'<div style="border:1px solid #8291a1; border-radius:8px; padding:12px 16px; '
            f'background:#8291a118; margin-bottom:14px;">'
            f"❌ <b>NÃO CONFIRMADO</b> (leitura: <b>{mtf.modality}</b>) — {tf_a} diz <b>{dir_a.value}</b>, "
            f"{tf_b} diz <b>{dir_b.value}</b>. Só é recomendação operável quando os dois concordam."
            f"</div>",
            unsafe_allow_html=True,
        )


OUTCOME_LABELS = {
    "ALVO_1": ("✅ Bateu o Alvo 1", "#2ed3a3"),
    "ALVO_2": ("✅ Bateu o Alvo 2", "#2ed3a3"),
    "STOP": ("❌ Bateu o Stop", "#ff5470"),
    "EM_ABERTO": ("⏳ Ainda em aberto", "#f0b429"),
    "SEM_SINAL": ("— Sem sinal operável nesta data", "#8291a1"),
    "SEM_DADO_FUTURO": ("⏳ Sem candles seguintes disponíveis ainda", "#8291a1"),
}


def render_retro_check(symbol: str, style: str, modality: str, source: str, count: int, params: AnalysisParams = DEFAULT_PARAMS) -> None:
    st.caption(
        "Roda a análise usando SÓ os dados que existiam até a data escolhida (sem espiar o "
        "futuro), depois confere o que aconteceu de verdade nos candles seguintes — se bateu "
        "entrada, alvo ou stop."
    )

    all_tfs = list(dict.fromkeys([*STYLES[style]["confirmation"], *STYLES[style]["context"]]))
    col1, col2 = st.columns(2)
    with col1:
        check_tf = st.selectbox("Timeframe a verificar", all_tfs, format_func=lambda tf: TIMEFRAME_LABELS[tf])
    with col2:
        default_date = pd.Timestamp.now(tz="America/Sao_Paulo").date() - pd.Timedelta(days=1)
        as_of_date = st.date_input("Data (fechamento até esse dia)", value=default_date)

    if st.button("🔍 Verificar", type="primary"):
        as_of_ts = pd.Timestamp(as_of_date).tz_localize("America/Sao_Paulo") + pd.Timedelta(hours=23, minutes=59)
        try:
            check = check_signal_as_of(symbol, check_tf, as_of_ts, count=count, modality=modality, source=source, params=params)
        except Exception as exc:
            st.error(f"Não foi possível verificar: {exc}")
            return

        st.markdown(f"**{symbol}** em **{TIMEFRAME_LABELS[check_tf]}** (leitura: **{modality}**), com dados até "
                   f"**{check.as_of.strftime('%d/%m/%Y %H:%M')}**")

        if check.direction == Direction.NEUTRAL or check.risk.entry is None:
            st.info(f"Não havia sinal operável nesta data ({modality} estava NEUTRO).")
            return

        color = DIRECTION_COLOR[check.direction]
        action = "COMPRAR" if check.direction == Direction.BUY else "VENDER"
        if quality(check.score, params) == "OPORTUNIDADE EXCEPCIONAL":
            st.markdown("🌟 **OPORTUNIDADE EXCEPCIONAL** nesta data")
        st.markdown(
            f'> **{action} {symbol}** perto de **R$ {check.risk.entry:.2f}**, stop em '
            f'**R$ {check.risk.stop:.2f}**, alvo em **R$ {check.risk.target_1:.2f}** '
            f'(setup: {check.setup}, score {check.score:.1f}/100)'
        )

        outcome_label, outcome_color = OUTCOME_LABELS[check.outcome]
        st.markdown(
            f'<div style="border:1px solid {outcome_color}; border-radius:8px; padding:12px 16px; '
            f'background:{outcome_color}18; margin:10px 0;">'
            f'<b style="color:{outcome_color}">{outcome_label}</b><br>{check.outcome_detail}'
            f'</div>',
            unsafe_allow_html=True,
        )

        if check.outcome in ("EM_ABERTO", "STOP", "ALVO_1", "ALVO_2"):
            st.caption(f"Candles disponíveis após a data escolhida: {check.candles_futuros_disponiveis}")


# ========================================================================
# Assertividade
# ========================================================================
RECORTE_LABELS = {
    "por_timeframe": "Timeframe",
    "por_symbol": "Ativo",
    "por_direcao": "Direção",
    "por_faixa_score": "Faixa de score",
    "por_mtf": "Confirmado no multi-timeframe",
}


def _tabela_assertividade(linhas: list[dict], rotulo: str | None) -> pd.DataFrame:
    """Monta a tabela de um recorte.

    `n` e `resolvidos` ficam SEMPRE visíveis: a taxa e a expectativa
    ignoram os sinais em aberto (o SQL usa avg(), que pula NULL), então
    esconder o denominador transformaria "2 de 3" em "66,7% de 28".
    """
    registros = []
    for linha in linhas:
        registro = {"Leitura": linha["modalidade"]}
        if rotulo:
            valor = linha["recorte"]
            if rotulo == "Confirmado no multi-timeframe":
                valor = {"true": "Sim", "false": "Não"}.get(valor, valor)
            registro[rotulo] = valor
        registro.update({
            "Sinais": linha["n"],
            "Resolvidos": linha["resolvidos"],
            "Em aberto": linha["em_aberto"],
            "Acertos": linha["acertos"],
            "Taxa de acerto": None if linha["taxa_acerto"] is None else round(linha["taxa_acerto"] * 100, 1),
            "Expectativa (R)": None if linha["expectativa_r"] is None else round(linha["expectativa_r"], 2),
            "Alvo 1": linha["alvo_1"],
            "Alvo 2": linha["alvo_2"],
            "Stop": linha["stop"],
        })
        registros.append(registro)
    return pd.DataFrame(registros)


def render_assertividade(perfis: list[str]) -> None:
    st.caption(
        "Taxa de acerto e expectativa dos sinais efetivamente gravados — os que o worker "
        "do homelab varreu automaticamente, mais os que você salvou à mão. Só entram na "
        "conta os que já tiveram desfecho (bateu alvo ou stop); os em aberto aparecem no "
        "contador, mas não na taxa."
    )

    if not daytrade_smc.ACOES_API_URL:
        st.info(
            "Este modo depende da API do homelab (`ACOES_API_URL`). Sem ela não há onde "
            "guardar o histórico de sinais — ver `docs/homelab-pipeline.md`."
        )
        return

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        f_perfil = st.selectbox("Perfil", ["Todos"] + perfis, key="assert_perfil")
    with col2:
        f_origem = st.selectbox("Origem", ["Todas", "worker", "manual"], key="assert_origem")
    with col3:
        f_symbol = st.selectbox("Ativo", ["Todos"] + st.session_state.watchlist, key="assert_symbol")
    with col4:
        f_dias = st.select_slider("Período", options=[7, 30, 90, 180, 365, 1095], value=90,
                                  format_func=lambda d: f"{d} dias", key="assert_dias")

    filtros = {
        "perfil": None if f_perfil == "Todos" else f_perfil,
        "origem": None if f_origem == "Todas" else f_origem,
        "symbol": None if f_symbol == "Todos" else f_symbol,
        "dias": f_dias,
    }

    try:
        stats = daytrade_smc.fetch_signal_stats(**filtros)
    except Exception as exc:
        st.error(f"Não foi possível carregar as estatísticas: {exc}")
        return

    if not stats["geral"]:
        st.info(
            "Nenhum sinal com desfecho neste recorte ainda. O worker grava os sinais assim "
            "que a vela fecha, mas o desfecho só aparece depois que o preço bate o alvo ou "
            "o stop — em D1 isso leva dias."
        )
        return

    total_n = sum(linha["n"] for linha in stats["geral"])
    total_res = sum(linha["resolvidos"] for linha in stats["geral"])
    total_acertos = sum(linha["acertos"] for linha in stats["geral"])
    total_aberto = sum(linha["em_aberto"] for linha in stats["geral"])

    cols = st.columns(4)
    cols[0].metric("Sinais no período", total_n)
    cols[1].metric("Já resolvidos", total_res, f"{total_aberto} em aberto")
    cols[2].metric(
        "Taxa de acerto",
        "—" if not total_res else f"{total_acertos / total_res * 100:.1f}%",
        help="Acertos ÷ resolvidos. Os sinais em aberto ficam de fora até baterem alvo ou stop.",
    )
    expectativas = [(l["expectativa_r"], l["resolvidos"]) for l in stats["geral"] if l["expectativa_r"] is not None]
    peso_total = sum(peso for _, peso in expectativas)
    cols[3].metric(
        "Expectativa (R)",
        "—" if not peso_total else f"{sum(v * p for v, p in expectativas) / peso_total:+.2f}",
        help="Retorno médio em múltiplos de risco, usando o R que cada sinal realmente tinha "
             "(alvo 1 e alvo 2 são parâmetros do perfil, não valores fixos).",
    )

    st.markdown("### Por tipo de análise")
    st.dataframe(_tabela_assertividade(stats["geral"], None), hide_index=True, use_container_width=True)

    for chave, rotulo in RECORTE_LABELS.items():
        linhas = stats.get(chave) or []
        if not linhas:
            continue
        with st.expander(f"Por {rotulo.lower()}"):
            st.dataframe(_tabela_assertividade(linhas, rotulo), hide_index=True, use_container_width=True)

    st.markdown("### Últimos sinais gravados")
    try:
        historico = daytrade_smc.fetch_signals(limite=100, **filtros)
    except Exception as exc:
        st.warning(f"Não foi possível carregar o histórico: {exc}")
        return

    if not historico["signals"]:
        st.caption("Nenhum sinal gravado neste recorte.")
        return

    st.caption(f"{historico['total']} sinal(is) no recorte · mostrando os {len(historico['signals'])} mais recentes")
    st.dataframe(
        [{
            "Vela": pd.Timestamp(s["candle_time"]).tz_convert("America/Sao_Paulo").strftime("%d/%m %H:%M"),
            "Ativo": s["symbol"],
            "TF": s["timeframe"],
            "Leitura": s["modalidade"],
            "Direção": s["direcao"],
            "Score": round(s["score"], 1),
            "MTF": "Sim" if s["mtf_confirmado"] else "Não",
            "Entrada": s["entrada"],
            "Stop": s["stop"],
            "Alvo 1": s["alvo_1"],
            "Desfecho": OUTCOME_LABELS.get(s["resultado"], ("Aguardando", ""))[0] if s["resultado"] else "Aguardando",
            "Perfil": s["perfil"],
            "Origem": s["origem"],
        } for s in historico["signals"]],
        hide_index=True, use_container_width=True,
    )


def render_individual_analysis(symbol: str, style: str, modality: str, source: str, count: int, risk_budget: float | None, params: AnalysisParams = DEFAULT_PARAMS, perfil: str = DEFAULT_PROFILE_NAME) -> None:
    confirmation = STYLES[style]["confirmation"]
    context_tfs = STYLES[style]["context"]
    all_tfs = list(confirmation) + [tf for tf in context_tfs if tf not in confirmation]

    fonte_label = SOURCE_LABELS.get(source, source)
    with st.spinner(f"Buscando {', '.join(TIMEFRAME_LABELS[tf] for tf in all_tfs)} de {symbol} via {fonte_label}..."):
        mtf = cached_mtf(symbol, count, confirmation, context_tfs, modality, source, params)

    render_confirmation_badge(mtf, confirmation, params)

    tf_tabs = st.tabs([TIMEFRAME_LABELS[tf] + (" (contexto)" if tf not in confirmation else "") for tf in all_tfs])
    for tab, tf in zip(tf_tabs, all_tfs):
        with tab:
            result = mtf.results[tf]
            if result.error:
                st.error(f"Não foi possível analisar {symbol} em {tf}: {result.error}")
                continue
            render_timeframe_panel(symbol, tf, result.context, result.signals, risk_budget, mtf, params, perfil)


def render_timeframe_panel(symbol: str, timeframe: str, context, signals, risk_budget: float | None, mtf=None, params: AnalysisParams = DEFAULT_PARAMS, perfil: str = DEFAULT_PROFILE_NAME) -> None:
    by_name = {s.name: s for s in signals}
    last_open = context.df.index[-1].tz_convert("America/Sao_Paulo")
    st.caption(f"{symbol} ({yahoo_symbol(symbol)}) · {timeframe} · último candle: {last_open} · "
              f"ATR {context.atr:.2f} ({context.atr_pct:.2f}%) · RVOL {context.rvol:.2f}x · "
              f"Volatilidade {context.volatility} · atualizado às "
              f"{pd.Timestamp.now(tz='America/Sao_Paulo').strftime('%H:%M:%S')}")

    chart_choice = st.selectbox(
        "Ver entrada/stop/alvo de qual leitura no gráfico:",
        [s.name for s in signals], index=0, key=f"chart_choice_{timeframe}",
    )
    st.plotly_chart(build_chart(context, by_name[chart_choice], symbol), use_container_width=True, key=f"chart_{timeframe}_{chart_choice}")

    tabs = st.tabs([s.name for s in signals])
    for tab, s in zip(tabs, signals):
        with tab:
            render_signal_panel(s, symbol, risk_budget, timeframe, context, mtf, params, perfil)

    st.markdown("### Resumo — as 5 leituras lado a lado")
    st.dataframe(
        [{
            "Análise": s.name, "Direção": s.direction.value, "Score": round(s.score, 1),
            "Entrada": round(s.risk.entry, 2) if s.risk.entry else None,
            "Stop": round(s.risk.stop, 2) if s.risk.stop else None,
            "Alvo 1": round(s.risk.target_1, 2) if s.risk.target_1 else None,
        } for s in signals],
        hide_index=True, use_container_width=True,
    )


# ========================================================================
# Fragmentos de auto-atualização — IMPORTANTE: precisam ser definidos
# UMA ÚNICA VEZ, em nível de módulo. Criar um `st.fragment(...)` novo
# a cada rerun do script (como dentro de um if/else no corpo principal)
# faz o Streamlit perder a referência de qual pedaço da tela pertence a
# qual fragmento entre uma atualização e outra — e o React trava tentando
# remover um nó do DOM que ele já não reconhece mais (o erro
# "removeChild ... not a child of this node"). Por isso, um fragmento
# fixo por intervalo, nunca criado dinamicamente.
# ========================================================================
@st.fragment(run_every=30)
def _auto_refresh_30(symbol, style, modality, source, count, risk_budget, params, perfil):
    render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)


@st.fragment(run_every=60)
def _auto_refresh_60(symbol, style, modality, source, count, risk_budget, params, perfil):
    render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)


@st.fragment(run_every=120)
def _auto_refresh_120(symbol, style, modality, source, count, risk_budget, params, perfil):
    render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)


@st.fragment(run_every=300)
def _auto_refresh_300(symbol, style, modality, source, count, risk_budget, params, perfil):
    render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)


_AUTO_REFRESH_FRAGMENTS = {
    30: _auto_refresh_30,
    60: _auto_refresh_60,
    120: _auto_refresh_120,
    300: _auto_refresh_300,
}


def run_scanner(symbols: list[str], style: str, modality: str, source: str, count: int, risk_budget: float | None, params: AnalysisParams = DEFAULT_PARAMS) -> pd.DataFrame:
    rows = []
    progress = st.progress(0.0, text="Iniciando scanner...")
    confirmation = STYLES[style]["confirmation"]
    context_tfs = STYLES[style]["context"]
    tf_a, tf_b = confirmation
    col_a, col_b = f"Score {tf_a}", f"Score {tf_b}"

    for i, symbol in enumerate(symbols):
        progress.progress((i + 1) / len(symbols), text=f"Analisando {symbol} ({i+1}/{len(symbols)})...")
        mtf = cached_mtf(symbol, count, confirmation, context_tfs, modality, source, params)
        result_a = mtf.results[tf_a]
        result_b = mtf.results[tf_b]

        if result_a.error or result_b.error:
            err = (result_a.error or result_b.error or "")[:60]
            rows.append({"Ativo": symbol, "Confirmado": "ERRO", "Destaque": "", "Direção": "ERRO", col_a: None,
                        col_b: None, "Score Geral": None, "Setup": err, "Entrada": None, "Stop": None,
                        "Alvo 1": None, "Quantidade": None, "Total (R$)": None})
            if source != "MetaTrader 5":
                time.sleep(0.3)
            continue

        if modality == ALL_MODALITIES_OPTION:
            score_a = round(overall_score(result_a.signals), 1)
            score_b = round(overall_score(result_b.signals), 1)
            confluence_a = next(s for s in result_a.signals if s.name == "Confluência")
            setup_text = (
                f"Score geral (média de 5 leituras) — {confluence_a.setup}" if mtf.confirmed
                else f"Score geral (média de 5 leituras) — sem confirmação entre {tf_a}/{tf_b}"
            )
            risk = confluence_a.risk if (mtf.confirmed and confluence_a.direction == mtf.confirmed_direction) else None
        else:
            conf_a = next(s for s in result_a.signals if s.name == modality)
            conf_b = next(s for s in result_b.signals if s.name == modality)
            score_a = round(conf_a.score, 1)
            score_b = round(conf_b.score, 1)
            setup_text = conf_a.setup if mtf.confirmed else f"{tf_a}={conf_a.direction.value} / {tf_b}={conf_b.direction.value}"
            risk = conf_a.risk if mtf.confirmed else None

        direction_label = mtf.confirmed_direction.value if mtf.confirmed else "NEUTRO"

        qty = None
        total = None
        if mtf.confirmed and risk_budget and risk and risk.entry is not None and risk.stop is not None:
            risk_per_share = abs(risk.entry - risk.stop)
            if risk_per_share > 0:
                qty = int(risk_budget // risk_per_share)
                total = round(qty * risk.entry, 2) if qty > 0 else 0.0

        score_geral = round((score_a + score_b) / 2, 1)
        destaque = "🌟 Excepcional" if (mtf.confirmed and quality(score_geral, params) == "OPORTUNIDADE EXCEPCIONAL") else ""

        rows.append({
            "Ativo": symbol,
            "Confirmado": "✅" if mtf.confirmed else "❌",
            "Destaque": destaque,
            "Direção": direction_label,
            col_a: score_a,
            col_b: score_b,
            "Score Geral": score_geral,
            "Setup": setup_text,
            "Entrada": round(risk.entry, 2) if risk and risk.entry else None,
            "Stop": round(risk.stop, 2) if risk and risk.stop else None,
            "Alvo 1": round(risk.target_1, 2) if risk and risk.target_1 else None,
            "Quantidade": qty,
            "Total (R$)": total,
        })
        if source != "MetaTrader 5":
            time.sleep(0.3)  # folga entre chamadas — reduz risco de rate limit do Yahoo (MT5 é chamada local, sem esse limite)

    progress.empty()
    result = pd.DataFrame(rows)
    if "Score Geral" in result.columns:
        # As recomendações são ordenadas pelo Score Geral (média das leituras em ambos os
        # timeframes de confirmação) — confirmadas primeiro, da maior pontuação pra menor.
        result = result.sort_values(["Confirmado", "Score Geral"], ascending=[True, False], na_position="last")
        result = result.reset_index(drop=True)
        # Posição: 1 = melhor colocado, numeração crescente conforme desce no ranking
        result.insert(0, "Posição", range(1, len(result) + 1))
    return result


# ========================================================================
# Estado inicial (ANTES da sidebar, pra widgets com `key` já nascerem
# com o valor certo — evita o bug clássico do Streamlit de "mudei o
# session_state depois que o widget já foi criado")
# ========================================================================
if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_symbols()

if "symbol_select" not in st.session_state:
    st.session_state.symbol_select = st.session_state.watchlist[0]

# Se o Scanner pediu pra "pular" pra um ativo, aplica ANTES do selectbox nascer
if st.session_state.get("jump_to_symbol"):
    target = st.session_state.pop("jump_to_symbol")
    if target in st.session_state.watchlist:
        st.session_state.symbol_select = target
    st.session_state.mode_select = "Análise individual"
    # limpa o estado do seletor "Ativo" do Scanner — ele não vai mais ser
    # renderizado nesta tela, e deixar a chave órfã pode confundir o
    # controle de estado de widgets em alguns cenários
    st.session_state.pop("scanner_pick_select", None)


# Perfis de análise. A troca de perfil precisa acontecer AQUI, antes da
# sidebar, pelo mesmo motivo do `jump_to_symbol` logo acima: os widgets do
# expander de parâmetros são presos às chaves `param_*`, e o Streamlit não
# deixa mexer numa chave depois que o widget dela já existe no mesmo run.
# Campos-tupla ganham um widget por posição (`param_<campo>_<i>`); os
# escalares têm um widget só (`param_<campo>`). A distinção existe porque
# não dá pra editar uma tupla num number_input, e reconstruí-la a partir
# das posições é mais simples que parsear texto.
PARAM_TUPLAS = {
    nome: len(valor)
    for nome, valor in DEFAULT_PARAMS.to_items()
    if isinstance(valor, tuple)
}
PARAM_ESCALARES = [nome for nome, valor in DEFAULT_PARAMS.to_items() if not isinstance(valor, tuple)]

# Como cada parâmetro aparece na sidebar. A ordem dos grupos segue os
# estágios do motor, na mesma sequência de `analyze()`: primeiro o que
# monta o contexto, depois as leituras, depois o filtro, depois o risco.
# (campo, rótulo, mínimo, máximo, passo) — os tipos de mínimo/máximo/passo
# decidem se o number_input é inteiro ou decimal, então precisam bater com
# o tipo do campo no AnalysisParams.
PARAM_UI = {
    "Contexto": [
        ("atr_periodo", "Período do ATR", 2, 200, 1),
        ("swing_esquerda", "Swing — velas à esquerda", 1, 20, 1),
        ("swing_direita", "Swing — velas à direita", 1, 20, 1),
        ("vol_baixa_max_pct", "Volatilidade BAIXA abaixo de (ATR %)", 0.0, 5.0, 0.05),
        ("vol_excessiva_min_pct", "Volatilidade EXCESSIVA acima de (ATR %)", 0.5, 30.0, 0.5),
    ],
    "Estrutura (BOS/CHoCH e FVG)": [
        ("estrutura_volume_min", "Volume mínimo do rompimento (× média)", 0.5, 5.0, 0.1),
        ("estrutura_range_min", "Amplitude mínima do rompimento (× ATR)", 0.1, 5.0, 0.1),
        ("evento_max_idade", "Idade máxima do BOS/CHoCH (velas)", 1, 200, 1),
        ("fvg_max_idade", "Idade máxima do FVG (velas)", 1, 200, 1),
    ],
    "Price Action": [
        ("rompimento_lookback", "Janela do rompimento (velas)", 3, 200, 1),
        ("rompimento_tolerancia_pct", "Tolerância do reteste (%)", 0.0, 5.0, 0.05),
    ],
    "VWAP": [
        ("vwap_distancia_min_pct", "Distância mínima pra contar (%)", 0.0, 2.0, 0.01),
        ("vwap_distancia_max_pct", "Distância que bloqueia a entrada (%)", 0.1, 10.0, 0.1),
    ],
    "Confluência": [
        ("peso_smc", "Peso — SMC", 0.0, 100.0, 1.0),
        ("peso_price_action", "Peso — Price Action", 0.0, 100.0, 1.0),
        ("peso_medias", "Peso — Médias Móveis", 0.0, 100.0, 1.0),
        ("peso_vwap", "Peso — VWAP", 0.0, 100.0, 1.0),
        ("normalizacao_score", "Divisor de normalização", 1.0, 200.0, 1.0),
        ("confluencia_banda_empate", "Banda de empate (pontos)", 0.0, 50.0, 0.5),
    ],
    "Filtro de mercado": [
        ("filtro_isolada_score_max", "Teto de score — leitura isolada", 0.0, 100.0, 1.0),
        ("filtro_isolada_confianca_max", "Teto de confiança — leitura isolada", 0.0, 100.0, 1.0),
        ("filtro_bloqueio_score_max", "Teto de score — entrada bloqueada", 0.0, 100.0, 1.0),
        ("filtro_bloqueio_confianca_max", "Teto de confiança — entrada bloqueada", 0.0, 100.0, 1.0),
        ("filtro_excessiva_score_max", "Teto de score — volatilidade excessiva", 0.0, 100.0, 1.0),
        ("filtro_excessiva_confianca_max", "Teto de confiança — volatilidade excessiva", 0.0, 100.0, 1.0),
        ("score_minimo_operavel", "Score mínimo operável", 0.0, 100.0, 1.0),
    ],
    "Risco": [
        ("rr_alvo_1", "Risco/retorno do alvo 1", 0.1, 20.0, 0.1),
        ("rr_alvo_2", "Risco/retorno do alvo 2", 0.1, 20.0, 0.1),
        ("stop_minimo_atr", "Distância mínima do stop (× ATR)", 0.0, 5.0, 0.05),
    ],
}

# campo -> (rótulos por posição, passo, formato)
PARAM_UI_TUPLAS = {
    "Confluência": {
        "multiplicador_concordancia": (["0", "1", "2", "3", "4"], 0.05, "%.2f"),
    },
    "Filtro de mercado": {
        "bandas_qualidade": (["Evitar", "Baixa", "Monitorar", "Boa", "Forte"], 1.0, "%.0f"),
    },
}

PARAM_AJUDA = {
    "vol_baixa_max_pct": "Abaixo disso a entrada é bloqueada — o ativo não anda o bastante pra pagar o risco.",
    "vol_excessiva_min_pct": "Acima disso o score é capado: o ativo está volátil demais pra o stop fazer sentido.",
    "evento_max_idade": "Um BOS/CHoCH mais velho que isso deixa de contar como sinal recente.",
    "normalizacao_score": "Divide o score de cada leitura antes de aplicar o peso. Acoplado ao teto da leitura isolada.",
    "confluencia_banda_empate": "Diferença mínima entre compra e venda pra a confluência sair de NEUTRO.",
    "multiplicador_concordancia": "Multiplicador do score da confluência por quantas das 4 leituras concordam (0 a 4)",
    "bandas_qualidade": "Score a partir do qual cada rótulo de qualidade começa",
    "score_minimo_operavel": "Abaixo disso a direção vira NEUTRO, qualquer que seja a leitura.",
    "stop_minimo_atr": "Piso da distância entrada→stop, pra um stop estrutural colado demais não virar risco irreal.",
    "rompimento_tolerancia_pct": "Quão perto do nível rompido o preço precisa voltar pra contar como reteste.",
}

if "perfis" not in st.session_state:
    st.session_state.perfis = load_profiles()
if "perfil_aplicado" not in st.session_state:
    st.session_state.perfil_aplicado = None

# Precisa nascer explícito, igual ao `symbol_select` acima. Sem isso o
# selectbox assume a PRIMEIRA opção da lista ordenada — que é o primeiro
# perfil em ordem alfabética, não o padrão. E o estrago não é só cosmético:
# o primeiro run aplicaria os parâmetros do padrão, o widget gravaria outro
# nome em `perfil_select`, e o run seguinte veria "o perfil mudou" e
# reaplicaria por cima de qualquer ajuste que o usuário tivesse feito.
if "perfil_select" not in st.session_state:
    st.session_state.perfil_select = DEFAULT_PROFILE_NAME

# Salvar/remover perfil precisa mudar qual está selecionado, e o botão que
# faz isso roda DEPOIS que o selectbox já nasceu — mexer na chave dele ali
# levanta StreamlitAPIException. Mesma solução do `jump_to_symbol` acima:
# o botão deixa o pedido aqui e o rerun aplica antes do widget existir.
if st.session_state.get("perfil_pendente"):
    _pedido = st.session_state.pop("perfil_pendente")
    st.session_state.perfis = load_profiles()
    if _pedido in st.session_state.perfis:
        st.session_state.perfil_select = _pedido
    else:
        st.session_state.perfil_select = DEFAULT_PROFILE_NAME
    st.session_state.perfil_aplicado = None  # força reaplicar as chaves param_*

_perfil_alvo = st.session_state.get("perfil_select") or DEFAULT_PROFILE_NAME
if _perfil_alvo not in st.session_state.perfis:
    _perfil_alvo = DEFAULT_PROFILE_NAME
if _perfil_alvo != st.session_state.perfil_aplicado:
    for _campo, _valor in st.session_state.perfis[_perfil_alvo].to_items():
        if isinstance(_valor, tuple):
            for _i, _item in enumerate(_valor):
                st.session_state[f"param_{_campo}_{_i}"] = _item
        else:
            st.session_state[f"param_{_campo}"] = _valor
    st.session_state.perfil_aplicado = _perfil_alvo


def _params_da_sessao() -> AnalysisParams:
    """Monta o AnalysisParams a partir das chaves `param_*` da sessão."""
    dados = {
        campo: st.session_state[f"param_{campo}"]
        for campo in PARAM_ESCALARES
        if f"param_{campo}" in st.session_state
    }
    for campo, tamanho in PARAM_TUPLAS.items():
        chaves = [f"param_{campo}_{i}" for i in range(tamanho)]
        if all(chave in st.session_state for chave in chaves):
            dados[campo] = [st.session_state[chave] for chave in chaves]
    return AnalysisParams.from_dict(dados)


def _persist_watchlist() -> None:
    try:
        save_symbols(st.session_state.watchlist)
    except OSError:
        pass  # ambiente somente-leitura — a lista continua funcionando na sessão


# ========================================================================
# Sidebar
# ========================================================================
with st.sidebar:
    st.markdown("## 📊 Day Trade SMC")
    st.caption("SMC · Price Action · Médias Móveis · VWAP")

    mode = st.radio(
        "Modo",
        ["Análise individual", "Scanner (todos os ativos)", "Verificação retroativa", "Assertividade"],
        key="mode_select",
    )

    st.markdown("### Fonte de dados")
    source = st.radio(
        "Fonte", DATA_SOURCES, key="source_select", horizontal=True,
        help="Yahoo Finance funciona em qualquer lugar, com atraso de ~15-20min. MetaTrader 5 "
             "direto é tempo real, mas só funciona rodando este app na máquina com o MT5 aberto. "
             "\"GitHub (MT5 de casa)\" funciona de qualquer lugar (inclusive do trabalho) e busca "
             "dado real do MT5, mas só atualiza quando você clicar em \"Atualizar via MT5\". "
             "\"Homelab (API)\" lê candles pela API do homelab, alimentada por um scraper "
             "MT5 rodando continuamente numa VM — dado real, quase em tempo real.",
    )
    if source == "MetaTrader 5":
        st.caption(
            "⚠️ Só funciona rodando localmente, na máquina com o MT5 aberto. Se você estiver "
            "vendo isso no Streamlit Cloud, vai dar erro de conexão — o servidor da nuvem não "
            "tem o MT5 instalado. Veja o README pra rodar local e acessar remoto."
        )
    elif source == "Homelab (API)":
        st.caption(
            "🏠 Lê candles pela API do homelab, alimentada por um scraper MT5 contínuo "
            "numa VM. Requer ACOES_API_URL configurada (st.secrets ou variável de ambiente) "
            "e o scraper rodando — veja `scraper/README.md`."
        )
    elif source == "GitHub (MT5 de casa)":
        last_update = fetch_snapshot_timestamp()
        if last_update:
            st.caption(f"📅 Última atualização: {pd.Timestamp(last_update).tz_convert('America/Sao_Paulo').strftime('%d/%m/%Y %H:%M:%S')}")
        else:
            st.caption("Nenhuma atualização publicada ainda — clique no botão abaixo.")

        if st.button("🔄 Atualizar via MT5 (casa)", use_container_width=True):
            ok, msg = trigger_github_update()
            if not ok:
                st.error(msg)
            else:
                st.cache_data.clear()
                with st.spinner("Aguardando seu computador em casa processar (isso leva alguns segundos)..."):
                    trigger_time = pd.Timestamp.now(tz="UTC")
                    updated = False
                    for _ in range(30):  # até ~90s de espera (30 x 3s)
                        time.sleep(3)
                        ts = fetch_snapshot_timestamp()
                        if ts and pd.Timestamp(ts) > trigger_time:
                            updated = True
                            break
                if updated:
                    st.cache_data.clear()
                    st.success("Dados atualizados!")
                    st.rerun()
                else:
                    st.warning(
                        "Não detectei a atualização em 90s. Confirme se o PC de casa está "
                        "ligado, o MT5 aberto e logado, e o runner do GitHub Actions rodando. "
                        "Pode levar mais tempo em alguns casos — tenta de novo em instantes."
                    )

    st.markdown("### Estilo de operação")
    style = st.radio(
        "Estilo", list(STYLES.keys()), key="style_select", horizontal=True,
        help="Day Trade confirma em M15+H1 (posições no mesmo dia). "
             "Swing Trade confirma em Diário+Semanal (posições de dias a semanas), com H4 como contexto de timing de entrada.",
    )
    conf_a, conf_b = STYLES[style]["confirmation"]

    st.markdown("### Modalidade")
    modality = st.selectbox(
        "Qual leitura usar como base da recomendação", MODALITY_CHOICES, key="modality_select",
        help="Confluência combina as 4 categorias. SMC/Price Action/Médias Móveis/VWAP usam só a "
             "leitura isolada daquela categoria. \"Todas as modalidades\" calcula um SCORE GERAL "
             "(média das 5 leituras) e usa ele — não uma única leitura — pra decidir a confirmação "
             "e ordenar o Scanner.",
    )

    st.markdown("### Ativos monitorados")
    new_symbol = st.text_input("Adicionar ativo (ex: VALE3)", key="new_symbol_input")
    if st.button("Adicionar", use_container_width=True) and new_symbol.strip():
        value = new_symbol.strip().upper().replace(" ", "")
        if value not in st.session_state.watchlist:
            st.session_state.watchlist.append(value)
            _persist_watchlist()
        st.rerun()

    if mode in ("Análise individual", "Verificação retroativa"):
        st.selectbox("Ativo para análise", st.session_state.watchlist, key="symbol_select")

    remove_symbol = st.selectbox("Remover ativo", ["—"] + st.session_state.watchlist, key="remove_symbol_select")
    if st.button("Remover", use_container_width=True) and remove_symbol != "—":
        st.session_state.watchlist = [s for s in st.session_state.watchlist if s != remove_symbol]
        _persist_watchlist()
        st.rerun()

    if st.button("Restaurar lista padrão", use_container_width=True):
        st.session_state.watchlist = DEFAULT_SYMBOLS.copy()
        _persist_watchlist()
        st.rerun()

    st.markdown("### Perfil de análise")
    perfil = st.selectbox(
        "Calibragem do motor", sorted(st.session_state.perfis), key="perfil_select",
        help="Um perfil é um conjunto nomeado de parâmetros do motor. Cada sinal salvo "
             "guarda o perfil que o gerou, então dá pra comparar a assertividade de uma "
             "calibragem contra a outra no modo Assertividade.",
    )
    params = _params_da_sessao()
    st.caption(f"hash `{params.params_hash()[:8]}`" + ("" if params == st.session_state.perfis.get(perfil) else " · **alterado, não salvo**"))

    if params.normalizacao_score != params.filtro_isolada_score_max:
        st.warning(
            "O divisor de normalização e o teto de score da leitura isolada estão "
            "diferentes. Os dois são acoplados por construção: uma leitura isolada é "
            "capada pelo teto, e a confluência divide por esse mesmo número pra ela "
            "normalizar em 1.0. Separados, a escala da confluência sai do lugar."
        )

    with st.expander("Ajustar parâmetros", expanded=False):
        for grupo, campos in PARAM_UI.items():
            st.markdown(f"**{grupo}**")
            for campo, rotulo, minimo, maximo, passo in campos:
                st.number_input(
                    rotulo, min_value=minimo, max_value=maximo, step=passo,
                    key=f"param_{campo}", help=PARAM_AJUDA.get(campo),
                )
            for campo, (rotulos, passo, formato) in PARAM_UI_TUPLAS.get(grupo, {}).items():
                st.caption(PARAM_AJUDA.get(campo, campo))
                colunas = st.columns(len(rotulos))
                for i, (coluna, rotulo) in enumerate(zip(colunas, rotulos)):
                    with coluna:
                        st.number_input(rotulo, step=passo, key=f"param_{campo}_{i}", format=formato)

        st.divider()
        novo_perfil = st.text_input("Salvar como perfil", value=perfil, key="novo_perfil_input")
        col_salvar, col_remover = st.columns(2)
        with col_salvar:
            if st.button("Salvar", use_container_width=True) and novo_perfil.strip():
                # o try cobre SÓ a gravação: englobar o st.rerun/session_state
                # transformaria um erro de API do Streamlit em "não foi possível
                # salvar" logo depois de a gravação ter dado certo
                try:
                    save_profile(novo_perfil.strip(), params)
                except Exception as exc:
                    st.error(f"Não foi possível salvar: {exc}")
                else:
                    st.session_state.perfil_pendente = novo_perfil.strip()
                    st.rerun()
        with col_remover:
            if st.button("Remover perfil", use_container_width=True, disabled=perfil == DEFAULT_PROFILE_NAME):
                try:
                    delete_profile(perfil)
                except Exception as exc:
                    st.error(f"Não foi possível remover: {exc}")
                else:
                    st.session_state.perfil_pendente = DEFAULT_PROFILE_NAME
                    st.rerun()

        if st.button("Restaurar valores do perfil", use_container_width=True):
            st.session_state.perfil_aplicado = None
            st.rerun()

    st.markdown("### Parâmetros")
    st.caption(f"A recomendação exige **{TIMEFRAME_LABELS[conf_a]}** e **{TIMEFRAME_LABELS[conf_b]}** concordando "
              f"(ver \"Filtro multi-timeframe\" no rodapé). "
              f"{', '.join(TIMEFRAME_LABELS[tf] for tf in STYLES[style]['context'])} aparece como contexto adicional.")
    count = st.slider(STYLES[style]["count_label"], min_value=50, max_value=400, value=250, step=10)
    risk_budget = st.number_input("Risco máximo (R$) — opcional", min_value=0.0, value=0.0, step=50.0)
    risk_budget = risk_budget if risk_budget > 0 else None

    # Inicializados INCONDICIONALMENTE, antes da cadeia de modos: eles são
    # lidos lá embaixo no corpo principal sem guarda nenhuma, e até aqui só
    # funcionava porque o if/elif do corpo espelhava exatamente o daqui. Com
    # mais um modo, esse acoplamento vira NameError na primeira divergência.
    auto_refresh = False
    refresh_interval = 60
    run_scanner_clicked = False

    if mode == "Análise individual":
        st.markdown("### Atualização")
        auto_refresh = st.checkbox("Atualizar automaticamente")
        refresh_interval = st.select_slider(
            "Intervalo", options=[30, 60, 120, 300], value=60, format_func=lambda s: f"{s}s",
            disabled=not auto_refresh,
        )
    elif mode == "Scanner (todos os ativos)":
        run_scanner_clicked = st.button("🔍 Rodar scanner", type="primary", use_container_width=True)


# ========================================================================
# Corpo principal
# ========================================================================
st.title("📊 Day Trade SMC — Análise Técnica")
st.warning(
    "⚠️ **Dados do Yahoo Finance com atraso de ~15-20 minutos.** Use esta ferramenta para "
    "**viés e estrutura** (tendência, níveis, força relativa entre ativos) — **nunca para o "
    "preço/timing exato de execução.** Antes de entrar numa operação, confirme o preço real "
    "no ProfitChart ou na tela da sua corretora.",
    icon="⏱️",
)

if mode == "Análise individual":
    symbol = st.session_state.symbol_select

    if auto_refresh:
        st.caption(f"🔄 Atualizando automaticamente a cada {refresh_interval}s")
        _AUTO_REFRESH_FRAGMENTS[refresh_interval](symbol, style, modality, source, count, risk_budget, params, perfil)
    else:
        render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)

elif mode == "Scanner (todos os ativos)":
    st.caption(f"{len(st.session_state.watchlist)} ativo(s) na watchlist · {style} · leitura: {modality} · "
              f"perfil: {perfil} · {count} candles · recomendação exige {conf_a}+{conf_b} concordando")

    if run_scanner_clicked:
        st.session_state.scanner_result = run_scanner(st.session_state.watchlist, style, modality, source, count, risk_budget, params)
        st.session_state.scanner_risk_budget = risk_budget

    if "scanner_result" in st.session_state:
        result_df = st.session_state.scanner_result

        if not st.session_state.get("scanner_risk_budget"):
            st.info("Defina o **Risco máximo (R$)** na barra lateral e rode o scanner de novo pra ver a "
                    "quantidade sugerida de ações em cada ativo.")

        def _color_direction(val):
            if val == "COMPRA":
                return "color: #2ed3a3; font-weight: 600"
            if val == "VENDA":
                return "color: #ff5470; font-weight: 600"
            return "color: #8291a1"

        st.dataframe(
            result_df.style.map(_color_direction, subset=["Direção"]),
            hide_index=True, use_container_width=True, height=min(450, 45 + 35 * len(result_df)),
        )

        st.markdown("#### Abrir análise completa de um ativo")
        pick = st.selectbox("Ativo", result_df["Ativo"].tolist(), key="scanner_pick_select")
        if st.button("Ver gráfico e as 5 leituras completas"):
            st.session_state.jump_to_symbol = pick
            st.rerun()
    else:
        st.info("Clique em **Rodar scanner** na barra lateral para analisar todos os ativos da watchlist.")

elif mode == "Verificação retroativa":
    symbol = st.session_state.symbol_select
    render_retro_check(symbol, style, modality, source, count, params)

else:  # Assertividade
    render_assertividade(sorted(st.session_state.perfis))
