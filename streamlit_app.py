"""
streamlit_app.py

Interface WEB para o motor de análise em `daytrade_smc.py`. Não tem lógica
de análise nenhuma — só importa as funções do motor e desenha por cima.

Quatro modos (seletor no topo do corpo, não na barra lateral — desde 2026-08-06):
    - Análise individual: gráfico de candles com EMAs/VWAP/swings/BOS-CHoCH/
      zonas de FVG, mais os painéis das 6 leituras. Pode auto-atualizar.
    - Scanner: roda a análise em TODOS os ativos da watchlist de uma vez e
      mostra um ranking pelo score geral, com atalho pra abrir qualquer um
      na análise individual.
    - Verificação retroativa: roda a análise numa data passada usando só o
      que se sabia até lá, e confere o desfecho nos candles seguintes.
    - Assertividade: taxa de acerto e expectativa em R do histórico de
      sinais gravado. Depende da API do homelab — é onde o histórico mora.

Rodar:
    streamlit run streamlit_app.py
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
    WINFUT_CONFIRMATION_TIMEFRAMES,
    WINFUT_CONTEXT_TIMEFRAMES,
    WINFUT_SYMBOL,
    SWING_CONTEXT_TIMEFRAMES,
    analyze_symbol_mtf,
    check_signal_as_of,
    delete_profile,
    load_profiles,
    load_symbols,
    overall_agreement,
    overall_direction,
    overall_score,
    params_para_estilo,
    quality,
    rsi_extremes_across_timeframes,
    save_profile,
    save_symbols,
    yahoo_symbol,
)

# Configura a API do homelab (serviço `api`, que serve o TimescaleDB
# alimentado pelo acoes-scraper). O fallback em variável de ambiente existe
# porque o container no k3s não popula st.secrets a menos que monte um
# secrets.toml — sem nenhum dos dois configurado, a fonte "Homelab (API)"
# dá erro claro em vez de travar, e a Yahoo continua utilizável.
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

# O Mini Índice tem estilo próprio, e de propósito NÃO entra no STYLES
# acima: aquele dicionário alimenta o radio "Estilo de operação" da barra
# lateral (`list(STYLES.keys())`), que se aplica à watchlist de AÇÕES.
# Somar o WINFUT ali colocaria um terceiro botão no seletor errado.
WINFUT_STYLE = "Mini Índice (WINFUT)"
_ESTILOS_TODOS = {
    **STYLES,
    WINFUT_STYLE: {
        "confirmation": WINFUT_CONFIRMATION_TIMEFRAMES,
        "context": WINFUT_CONTEXT_TIMEFRAMES,
        "count_label": "Candles fechados (5 e 15 minutos)",
    },
}


def estilo(nome: str) -> dict:
    """Resolve um estilo pelos DOIS conjuntos — os da barra lateral e o do
    Mini Índice. Quem renderiza análise passa por aqui em vez de indexar
    `STYLES` direto, senão o modo WINFUT levanta KeyError."""
    return _ESTILOS_TODOS[nome]

st.set_page_config(page_title="Day Trade SMC", page_icon="📊", layout="wide")

DIRECTION_COLOR = {
    Direction.BUY: "#2ed3a3",
    Direction.SELL: "#ff5470",
    Direction.NEUTRAL: "#8291a1",
}

SOURCE_LABELS = {
    "Homelab (API)": "Homelab (API, quase em tempo real)",
    "Yahoo Finance": "Yahoo Finance (atraso ~15-20min)",
}

# Folga entre ativos no Scanner, cobrada SÓ do Yahoo — é a única fonte com
# rate limit. A API do homelab é uma chamada de rede local; pagar essa pausa
# por ativo ali só somaria segundos à varredura em troca de nada. A regra
# antes estava escrita ao contrário ("todo mundo menos o MT5"), o que fazia
# cada fonte nova nascer pagando o pedágio do Yahoo sem ninguém decidir isso.
_PAUSA_YAHOO = 0.3


# ========================================================================
# Dados / cache / análise
# ========================================================================
@st.cache_data(ttl=60, show_spinner=False)
def _cached_mtf_yahoo(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="Yahoo Finance", params=AnalysisParams.from_items(params_items))


@st.cache_data(ttl=3, show_spinner=False)
def _cached_mtf_api(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, params_items: tuple):
    counts = {tf: count for tf in (*confirmation, *context)}
    return analyze_symbol_mtf(symbol, confirmation=confirmation, context=context, counts=counts, modality=modality, source="Homelab (API)", params=AnalysisParams.from_items(params_items))


def cached_mtf(symbol: str, count: int, confirmation: tuple[str, str], context: tuple[str, ...], modality: str, source: str, params: AnalysisParams = DEFAULT_PARAMS):
    """
    Cacheia o pacote de timeframes. Homelab (API) usa 3s (o scraper alimenta
    o banco a cada poucos segundos, então cache longo só esconde dado que já
    chegou); Yahoo Finance usa 60s, porque tem rate limit e o dado nasce
    ~15-20min atrasado de qualquer jeito. Funções fixas em vez de decoradas
    dinamicamente, pelo mesmo motivo dos fragmentos de auto-refresh: evita o
    bug de identidade de widget no React já corrigido antes neste projeto.

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
    return _cached_mtf_yahoo(symbol, count, confirmation, context, modality, params_items)


@st.cache_data(ttl=30, show_spinner=False)
def _ultima_vela():
    """Vela mais recente que a API tem, pra legenda de frescor da sidebar.

    TTL de 30s em vez dos 3s da análise: isto roda em TODO rerun da página,
    inclusive nos do auto-refresh, e é informação de rodapé — não vale uma
    ida à rede por clique de widget. `fetch_last_candle_time` já engole as
    falhas e devolve None."""
    return daytrade_smc.fetch_last_candle_time()


# Piso de sinais resolvidos, dos dois lados (confirmado e não confirmado),
# pra uma taxa entrar na tela. Abaixo disso é ruído, não medição — comparar
# 40% de 3 sinais contra 42% de 3000 daria peso igual a acaso e a estatística.
_TAXA_MTF_RESOLVIDOS_MINIMO = 30


@st.cache_data(ttl=3600, show_spinner=False)
def _taxa_mtf(modalidade: str, timeframe: str, dias: int = 365) -> dict[bool, dict] | None:
    """Taxa de acerto medida, confirmado vs não confirmado, pra uma
    leitura+timeframe. Devolve {True: linha, False: linha} — os dois lados
    do recorte `por_mtf` de `/signals/stats`, filtrado por timeframe (o
    filtro recorta a CTE base ANTES do GROUP BY, então uma chamada só já
    cobre os dois lados).

    None quando a API não está configurada, quando falta um dos dois lados,
    quando algum deles não bate `_TAXA_MTF_RESOLVIDOS_MINIMO`, ou em
    qualquer falha de rede — isto alimenta uma caption do veredito, não
    pode derrubar a tela por causa disso.

    TTL de 1h: é histórico medido, não dado ao vivo — não vale uma chamada
    de rede por rerun da página, muito menos por candidato do Scanner.
    """
    if not daytrade_smc.ACOES_API_URL:
        return None
    try:
        stats = daytrade_smc.fetch_signal_stats(timeframe=timeframe, dias=dias)
    except Exception:
        return None

    linhas = {
        linha["recorte"] == "true": linha
        for linha in stats.get("por_mtf") or []
        if linha["modalidade"] == modalidade
    }
    if True not in linhas or False not in linhas:
        return None
    if (linhas[True]["resolvidos"] < _TAXA_MTF_RESOLVIDOS_MINIMO
            or linhas[False]["resolvidos"] < _TAXA_MTF_RESOLVIDOS_MINIMO):
        return None
    return linhas


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
def _salvar_sinal(signal: Signal, symbol: str, timeframe: str, context, mtf, params: AnalysisParams,
                  perfil: str, style: str) -> None:
    """Grava um sinal manual e dá o feedback (sucesso/duplicado/erro).

    Fatorado em 2026-08-06 pra ser chamado tanto pelo botão em destaque no
    veredito quanto pelo botão dentro do expander de detalhe — sem duplicar
    a lógica de confirmação+gravação entre os dois. É o mecanismo PRINCIPAL
    de geração de sinal agora (o worker automático foi reduzido a propósito,
    ver docs/homelab-pipeline.md).

    A confirmação gravada é a DESTA leitura (`signal.name`), não
    necessariamente a modalidade escolhida na sidebar — reaproveitar a
    confirmação de outra leitura falsearia o recorte de assertividade."""
    confirmado, direcao_mtf = (False, Direction.NEUTRAL)
    if mtf is not None:
        confirmado, direcao_mtf = daytrade_smc.mtf_confirmation(
            {tf: r.signals for tf, r in mtf.results.items()},
            estilo(style)["confirmation"],
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
            _salvar_sinal(signal, symbol, timeframe, context, mtf, params, perfil,
                         st.session_state.style_select)


TIMEFRAME_LABELS = {
    "M2": "2 minutos",
    "M5": "5 minutos",
    "M15": "15 minutos",
    "H1": "60 minutos",
    "H4": "240 minutos",
    "D1": "Diário",
    "W1": "Semanal",
}


def leitura_ativa(signals: list[Signal], modality: str) -> Signal | None:
    """O sinal que a Modalidade escolhida na sidebar designa.

    A tela inteira passou a girar em torno desta função. Antes a Modalidade
    decidia só a confirmação e a ordenação do Scanner: a Análise individual
    ignorava a escolha e desenhava as CINCO leituras, em abas dentro de abas
    (4 timeframes × 6 leituras = 24 painéis por ativo). Pedir uma decisão ao
    usuário e depois não usá-la é a origem da queixa de "tela confusa".

    Com "Todas as modalidades" não existe um Signal só, e devolver um
    sintético seria mentira: o plano de risco de uma média de seis leituras
    não existe no motor. Nesse caso devolve None e quem chama usa a
    Confluência como portadora do plano — que é a regra que o Scanner já
    seguia, agora explicitada num lugar só em vez de repetida."""
    if modality == ALL_MODALITIES_OPTION:
        return None
    return next((s for s in signals if s.name == modality), None)


def portadora_do_plano(signals: list[Signal], modality: str) -> Signal | None:
    """Qual leitura carrega entrada/stop/alvo quando a modalidade é a média.

    A Confluência, e só quando ela concorda com a direção geral — senão não
    há um plano coerente pra mostrar, só uma votação. Mesma regra do
    `run_scanner`."""
    ativa = leitura_ativa(signals, modality)
    if ativa is not None:
        return ativa
    confluencia = next((s for s in signals if s.name == "Confluência"), None)
    if confluencia is None or confluencia.direction != overall_direction(signals):
        return None
    return confluencia


def render_veredito(mtf, confirmation: tuple[str, str], symbol: str, risk_budget: float | None,
                    params: AnalysisParams = DEFAULT_PARAMS) -> Signal | None:
    """O bloco de resposta, no topo da tela. Devolve a leitura que carrega o
    plano de risco (ou None), pra quem chama desenhar o gráfico e oferecer
    "salvar sinal" com ela.

    Uma tela, uma resposta. O que existia antes era um selo de confirmação
    seguido de vinte painéis — o usuário tinha que montar o veredito sozinho
    lendo abas. Aqui ou tem operação (com preço, stop e alvo na mesma frase)
    ou tem o motivo de não ter, escrito por extenso.

    2026-08-06: a concordância entre os dois timeframes obrigatórios DEIXOU
    DE SER GATE. Medimos 81 mil sinais resolvidos e a confirmação
    multi-timeframe rendeu PIOR que a ausência dela (37,0% vs 42,1% de
    acerto, z=-10,7, nas leituras sem exceção) — travar a tela nisso
    escondia justamente o subconjunto que media melhor. Agora `mtf.confirmed`
    vira só mais um fato na caption, ao lado da taxa medida quando existir
    (`_taxa_mtf`), e quem decide se o plano aparece é só ele ter entrada
    válida — não mais concordância entre timeframes."""
    tf_a, tf_b = confirmation
    result_a, result_b = mtf.results[tf_a], mtf.results[tf_b]

    if result_a.error or result_b.error:
        st.error(
            f"Não dá pra concluir nada sobre {symbol}: falha ao buscar "
            f"{TIMEFRAME_LABELS[tf_a]} e/ou {TIMEFRAME_LABELS[tf_b]}. "
            f"{result_a.error or ''} {result_b.error or ''}".strip()
        )
        return None

    plano = portadora_do_plano(result_a.signals, mtf.modality)
    risk = plano.risk if plano else None

    # Concordância entre os dois timeframes — informativo em qualquer
    # caminho agora, não decide mais se o plano aparece.
    if mtf.modality == ALL_MODALITIES_OPTION:
        dir_a, dir_b = overall_direction(result_a.signals), overall_direction(result_b.signals)
        ag_a, tot_a = overall_agreement(result_a.signals)
        ag_b, tot_b = overall_agreement(result_b.signals)
        detalhe = (f"{TIMEFRAME_LABELS[tf_a]}: **{dir_a.value}** ({ag_a} de {tot_a} leituras) · "
                   f"{TIMEFRAME_LABELS[tf_b]}: **{dir_b.value}** ({ag_b} de {tot_b})")
    else:
        dir_a = next(s.direction for s in result_a.signals if s.name == mtf.modality)
        dir_b = next(s.direction for s in result_b.signals if s.name == mtf.modality)
        detalhe = (f"{TIMEFRAME_LABELS[tf_a]}: **{dir_a.value}** · "
                   f"{TIMEFRAME_LABELS[tf_b]}: **{dir_b.value}**")

    # ---- caminho 1: sem leitura operável no timeframe de entrada ----
    if plano is None or risk is None or risk.entry is None:
        st.markdown(
            f'<div style="border-left:4px solid #8291a1; border-radius:4px; padding:14px 18px; '
            f'background:#8291a114; margin-bottom:16px;">'
            f'<div style="font-size:20px; font-weight:600; color:#8291a1;">SEM OPERAÇÃO EM {symbol}</div>'
            f'<div style="margin-top:6px; opacity:.85;">Sem sinal operável em {mtf.modality} '
            f"em {TIMEFRAME_LABELS[tf_a]} — entrada, stop e alvo foram bloqueados.</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"{detalhe} · leitura: {mtf.modality}")
        return plano

    # ---- caminho 2: operação. Mostra sempre que houver plano válido —
    # concordância entre timeframes virou dado na caption, não condição. ----
    cor = DIRECTION_COLOR[plano.direction]
    acao = "COMPRAR" if plano.direction == Direction.BUY else "VENDER"
    st.markdown(
        f'<div style="border-left:4px solid {cor}; border-radius:4px; padding:14px 18px; '
        f'background:{cor}14; margin-bottom:16px;">'
        f'<div style="font-size:20px; font-weight:600; color:{cor};">'
        f'{acao} {symbol} · R$ {risk.entry:.2f}</div>'
        f'<div style="margin-top:6px; opacity:.9;">'
        f"stop <b>R$ {risk.stop:.2f}</b> · alvo <b>R$ {risk.target_1:.2f}</b> · "
        f"R/R <b>1:{risk.rr:.2f}</b> · score {plano.score:.0f}/100</div>"
        f"</div>",
        unsafe_allow_html=True,
    )

    concordam = "concordam" if mtf.confirmed else "discordam"
    linha = f"{TIMEFRAME_LABELS[tf_a]} e {TIMEFRAME_LABELS[tf_b]} {concordam}"
    taxa = _taxa_mtf(plano.name, tf_a)
    if taxa is not None:
        t_sim, t_nao = taxa[True], taxa[False]
        linha += (
            f" · medição de {plano.name} em {TIMEFRAME_LABELS[tf_a]}: "
            f"{t_sim['taxa_acerto'] * 100:.0f}% quando concorda (n={t_sim['resolvidos']}) "
            f"vs {t_nao['taxa_acerto'] * 100:.0f}% quando discorda (n={t_nao['resolvidos']})"
        )
    else:
        linha += " · sem dado histórico suficiente pra comparar ainda"
    linha += f" · leitura: {mtf.modality} · {plano.setup}"
    if risk_budget:
        risco_acao = abs(risk.entry - risk.stop)
        if risco_acao > 0:
            qtd = int(risk_budget // risco_acao)
            linha += (f" · com R$ {risk_budget:.0f} de risco: **{qtd} ações** "
                      f"(R$ {qtd * risk.entry:.2f} no total)")
    st.caption(linha)
    return plano


OUTCOME_LABELS = {
    "ALVO_1": ("✅ Bateu o Alvo 1", "#2ed3a3"),
    "ALVO_2": ("✅ Bateu o Alvo 2", "#2ed3a3"),
    "STOP": ("❌ Bateu o Stop", "#ff5470"),
    "EM_ABERTO": ("⏳ Ainda em aberto", "#f0b429"),
    "SEM_SINAL": ("— Sem sinal operável nesta data", "#8291a1"),
    # Não é acerto nem erro: a vela seguinte abriu além do stop, então a
    # operação não chegou a existir. Contar isso como stop seria inventar uma
    # perda que ninguém teve; contar como acerto, o oposto. Fica fora da conta.
    "SEM_ENTRADA": ("— Gap abriu além do stop; sem operação", "#8291a1"),
    "SEM_DADO_FUTURO": ("⏳ Sem candles seguintes disponíveis ainda", "#8291a1"),
}


def render_retro_check(symbol: str, style: str, modality: str, source: str, count: int, params: AnalysisParams = DEFAULT_PARAMS) -> None:
    st.caption(
        "Roda a análise usando SÓ os dados que existiam até a data escolhida (sem espiar o "
        "futuro), depois confere o que aconteceu de verdade nos candles seguintes — se bateu "
        "entrada, alvo ou stop."
    )

    all_tfs = list(dict.fromkeys([*estilo(style)["confirmation"], *estilo(style)["context"]]))
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
        # Sem selo de "oportunidade excepcional" aqui — a faixa de score 80+
        # mediu como a SEGUNDA PIOR em expectativa nos 81 mil sinais
        # analisados em 2026-08-06 (ver render_veredito). Destacar
        # visualmente o que a medição contradiz seria o mesmo erro de novo.
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

        if check.r_realizado is not None:
            st.caption(f"R realizado: {check.r_realizado:+.2f} (fill em R$ {check.preco_fill:.2f})")

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
        "Taxa de acerto e expectativa dos sinais efetivamente gravados: os que o worker "
        "do homelab varreu automaticamente (`worker`), os que você salvou à mão "
        "(`manual`) e os reconstruídos a partir das velas já guardadas no banco "
        "(`backfill`). Só entram na conta os que já tiveram desfecho (bateu alvo ou "
        "stop); os em aberto aparecem no contador, mas não na taxa. O período é contado "
        "pela data da **vela**, não pela data em que o sinal foi gravado."
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
        # 'backfill' segue como opção de filtro (a reconstrução em massa não
        # é mais a fonte principal desde 2026-08-06, mas o comando ainda
        # existe pra casos pontuais — ex: bootstrapar histórico de um ativo
        # novo). 'manual' é o caminho principal agora: o botão "💾 Salvar
        # este sinal" no veredito de cada análise.
        f_origem = st.selectbox("Origem", ["Todas", "worker", "manual", "backfill"],
                                key="assert_origem")
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
            "Nenhum sinal com desfecho neste recorte ainda. A partir de 2026-08-06 a geração "
            "de sinal deixou de ser em massa — use o botão \"💾 Salvar este sinal\" na Análise "
            "individual pra registrar o que você realmente decidiu operar, ou aguarde o worker "
            "reduzido acumular histórico (ele só grava leitura operável, não mais toda "
            "varredura). O desfecho aparece depois que o preço bate o alvo ou o stop — em D1 "
            "isso leva dias."
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

    # `modelo_atual` conta quantos dos resolvidos já foram avaliados pelo
    # modelo de execução corrigido (fill na abertura seguinte, R líquido de
    # custo — ver evaluate_signal_outcome). Abaixo de `total_res`, parte da
    # expectativa acima ainda fala o modelo antigo, que inflava o resultado
    # justamente nos dias de gap — e isso precisa ficar visível, não
    # silenciosamente misturado numa média só.
    total_modelo_atual = sum(linha.get("modelo_atual", 0) for linha in stats["geral"])
    if total_res and total_modelo_atual < total_res:
        st.warning(
            f"⚠️ {total_res - total_modelo_atual} de {total_res} sinais resolvidos ainda "
            "estão com o modelo de execução ANTIGO (assumia fill no fechamento da vela do "
            "sinal, sem custo) — a expectativa acima mistura os dois modelos. Rode "
            "`analyzer.py --reavaliar-tudo` pra recalcular tudo com o modelo atual."
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


def _linha_leitura(s: Signal) -> dict:
    """Uma leitura virada linha de tabela. É aqui que as leituras sem entrada
    param de virar painel: elas viram uma linha dizendo que não têm nada, em
    vez de meia tela de métricas zeradas e um aviso azul."""
    return {
        "Leitura": s.name,
        "Direção": s.direction.value,
        "Score": round(s.score, 1),
        "Entrada": round(s.risk.entry, 2) if s.risk.entry else None,
        "Stop": round(s.risk.stop, 2) if s.risk.stop else None,
        "Alvo 1": round(s.risk.target_1, 2) if s.risk.target_1 else None,
        "Setup": s.setup,
    }


def render_rsi_multi_tf(mtf, params: AnalysisParams) -> None:
    """Consolida os extremos de IFR de todos os timeframes analisados.

    Exaustão simultânea em 2+ timeframes é rara e é a leitura de maior
    convicção que o motor produz — vale mais que qualquer leitura
    isolada com score alto. Por isso fica no topo, não num expander.
    """
    consolidado = rsi_extremes_across_timeframes(mtf, params)
    por_tf = consolidado["por_tf"]
    if not por_tf:
        return

    alinhamento = consolidado["alinhamento"]
    direcao = consolidado["direcao"]

    if alinhamento >= 2:
        cor = DIRECTION_COLOR[Direction(direcao)]
        titulo = f'<b style="color:{cor}">🎯 Exaustão em {alinhamento} timeframes — {direcao}</b>'
    elif alinhamento == 1:
        cor = DIRECTION_COLOR[Direction(direcao)]
        titulo = f'<b style="color:{cor}">Exaustão em 1 timeframe — {direcao}</b>'
    else:
        cor = DIRECTION_COLOR[Direction.NEUTRAL]
        titulo = '<span style="color:#8291a1">Nenhum timeframe em exaustão</span>'

    partes = []
    for tf, (valor, zona) in por_tf.items():
        rotulo = TIMEFRAME_LABELS.get(tf, tf)
        if zona is None:
            partes.append(f'{rotulo} {valor:.0f}')
        else:
            partes.append(f'<b style="color:{DIRECTION_COLOR[Direction(zona)]}">{rotulo} {valor:.0f}</b>')

    st.markdown(
        f'<div style="border:1px solid {cor}; border-radius:8px; padding:8px 14px; '
        f'background:{cor}18; margin:6px 0 14px 0;">{titulo}<br>'
        f'<span style="font-size:0.92em">IFR({params.rsi_periodo}) — {" · ".join(partes)}'
        f' · exaustão em ≤{params.rsi_sobrevenda:.0f} ou ≥{params.rsi_sobrecompra:.0f}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def render_rsi_badge(context, params: AnalysisParams) -> None:
    """Faixa visual do IFR do timeframe atual, com o Diário ao lado quando disponível."""
    rsi = context.rsi
    sobrevenda, sobrecompra = params.rsi_sobrevenda, params.rsi_sobrecompra

    if rsi <= sobrevenda:
        zona, cor = f"EXAUSTÃO VENDEDORA (≤{sobrevenda:.0f}) → COMPRA", DIRECTION_COLOR[Direction.BUY]
    elif rsi >= sobrecompra:
        zona, cor = f"EXAUSTÃO COMPRADORA (≥{sobrecompra:.0f}) → VENDA", DIRECTION_COLOR[Direction.SELL]
    else:
        zona, cor = "Sem exaustão", DIRECTION_COLOR[Direction.NEUTRAL]

    diario = ""
    if context.higher_rsi is not None:
        d = context.higher_rsi
        d_zona = "exaurido ↓" if d <= sobrevenda else "exaurido ↑" if d >= sobrecompra else "sem exaustão"
        diario = f" · <b>Diário:</b> {d:.1f} ({d_zona})"

    st.markdown(
        f'<div style="border:1px solid {cor}; border-radius:8px; padding:10px 14px; '
        f'background:{cor}18; margin:6px 0 14px 0;">'
        f'<b>IFR ({params.rsi_periodo}):</b> <b style="color:{cor}">{rsi:.1f} — {zona}</b>{diario}'
        f'<div style="background:#8291a133; height:8px; border-radius:4px; margin-top:8px; position:relative;">'
        f'<div style="position:absolute; left:{sobrevenda:.0f}%; top:0; bottom:0; width:1px; background:#8291a1;"></div>'
        f'<div style="position:absolute; left:{sobrecompra:.0f}%; top:0; bottom:0; width:1px; background:#8291a1;"></div>'
        f'<div style="position:absolute; left:calc({min(max(rsi, 0), 100):.0f}% - 4px); top:-2px; '
        f'width:8px; height:12px; border-radius:2px; background:{cor};"></div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def render_individual_analysis(symbol: str, style: str, modality: str, source: str, count: int, risk_budget: float | None, params: AnalysisParams = DEFAULT_PARAMS, perfil: str = DEFAULT_PROFILE_NAME) -> None:
    """Uma tela, uma resposta.

    A ordem é deliberada e é o oposto da anterior: veredito primeiro, prova
    depois, detalhe só a pedido. A tela antiga abria com quatro abas de
    timeframe, cada uma com cinco abas de leitura, e deixava a conclusão pro
    usuário montar. Aqui o gráfico é UM (o timeframe de entrada), as outras
    leituras são linhas de tabela, e os timeframes de contexto são uma linha
    cada — não abas."""
    confirmation = estilo(style)["confirmation"]
    context_tfs = estilo(style)["context"]
    tf_entrada = confirmation[0]

    fonte_label = SOURCE_LABELS.get(source, source)
    with st.spinner(f"Analisando {symbol} via {fonte_label}..."):
        mtf = cached_mtf(symbol, count, confirmation, context_tfs, modality, source, params)

    plano = render_veredito(mtf, confirmation, symbol, risk_budget, params)
    render_rsi_multi_tf(mtf, params)

    if plano is not None and plano.risk.entry is not None and daytrade_smc.ACOES_API_URL:
        # Caminho PRINCIPAL de geração de sinal agora — o worker automático
        # foi reduzido de propósito em 2026-08-06 (ver
        # docs/homelab-pipeline.md): a maioria do que ele gravava era leitura
        # sem sinal, e a confirmação multi-timeframe que a tela usava como
        # filtro de qualidade mediu PIOR que a ausência dela. Fica logo
        # abaixo do veredito, não enterrado num expander de detalhe — antes
        # disso o botão de salvar nunca tinha sido usado nem uma vez.
        if st.button("💾 Salvar este sinal", type="primary",
                     key=f"salvar_veredito_{symbol}_{tf_entrada}_{plano.name}"):
            _salvar_sinal(plano, symbol, tf_entrada, mtf.results[tf_entrada].context, mtf,
                         params, perfil, style)

    resultado_entrada = mtf.results[tf_entrada]
    if resultado_entrada.error:
        return

    contexto = resultado_entrada.context
    sinais = resultado_entrada.signals

    st.caption(
        f"{symbol} ({yahoo_symbol(symbol)}) · gráfico em {TIMEFRAME_LABELS[tf_entrada]} · "
        f"último candle {contexto.df.index[-1].tz_convert('America/Sao_Paulo'):%d/%m %H:%M} · "
        f"ATR {contexto.atr:.2f} ({contexto.atr_pct:.2f}%) · RVOL {contexto.rvol:.2f}x · "
        f"volatilidade {contexto.volatility} · atualizado {pd.Timestamp.now(tz='America/Sao_Paulo'):%H:%M:%S}"
    )
    render_rsi_badge(contexto, params)
    st.plotly_chart(
        build_chart(contexto, plano, symbol),
        use_container_width=True, key=f"chart_{symbol}_{tf_entrada}",
    )

    # ---- a prova: as seis leituras nos dois timeframes que decidem ----
    st.markdown("#### O que sustenta (ou derruba) o veredito")
    for tf in confirmation:
        resultado = mtf.results[tf]
        if resultado.error:
            st.warning(f"{TIMEFRAME_LABELS[tf]}: {resultado.error}")
            continue
        st.caption(f"**{TIMEFRAME_LABELS[tf]}**")
        st.dataframe(
            pd.DataFrame([_linha_leitura(s) for s in resultado.signals]),
            hide_index=True, use_container_width=True,
        )

    # ---- contexto: uma linha por timeframe, não uma aba ----
    linhas_contexto = []
    for tf in context_tfs:
        resultado = mtf.results.get(tf)
        if resultado is None or resultado.error:
            continue
        s = leitura_ativa(resultado.signals, modality)
        direcao = s.direction if s else overall_direction(resultado.signals)
        score = s.score if s else overall_score(resultado.signals)
        linhas_contexto.append({
            "Timeframe": TIMEFRAME_LABELS[tf],
            "Direção": direcao.value,
            "Score": round(score, 1),
        })
    if linhas_contexto:
        st.caption("**Contexto** — tendência mais ampla. Não confirma nem bloqueia a recomendação.")
        st.dataframe(pd.DataFrame(linhas_contexto), hide_index=True, use_container_width=True)

    # ---- detalhe: só a pedido, e só da leitura que decide ----
    nome_detalhe = plano.name if plano else "Confluência"
    with st.expander(f"Detalhe da leitura {nome_detalhe} em {TIMEFRAME_LABELS[tf_entrada]}"):
        alvo = next((s for s in sinais if s.name == nome_detalhe), None)
        if alvo is not None:
            render_signal_panel(alvo, symbol, risk_budget, tf_entrada, contexto, mtf, params, perfil)

    with st.expander("Ver todas as leituras em detalhe"):
        outras = [s for s in sinais if s.name != nome_detalhe]
        abas = st.tabs([s.name for s in outras])
        for aba, s in zip(abas, outras):
            with aba:
                render_signal_panel(s, symbol, risk_budget, tf_entrada, contexto, mtf, params, perfil)


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
    """
    2026-08-06: parou de ordenar por "Confirmado" e de destacar score 80+.
    Os 81 mil sinais medidos mostraram os dois como PIORES que a ausência
    deles (confirmação: 37,0% vs 42,1% de acerto; faixa 80+: segunda pior
    expectativa) — usá-los como critério de ranking apontaria justamente
    pro pior subconjunto primeiro. "Confirmado" continua na tabela como
    dado; "Score Geral" continua ordenando por não termos, ainda, um
    critério validado melhor — mas sem pretender que é garantia de nada.
    """
    rows = []
    progress = st.progress(0.0, text="Iniciando scanner...")
    confirmation = estilo(style)["confirmation"]
    context_tfs = estilo(style)["context"]
    tf_a, tf_b = confirmation
    col_a, col_b = f"Score {tf_a}", f"Score {tf_b}"

    # Não depende do símbolo — uma leitura só (cacheada por _taxa_mtf) serve
    # a tabela inteira, não é uma chamada de rede por ativo escaneado.
    modalidade_taxa = modality if modality != ALL_MODALITIES_OPTION else "Confluência"
    taxa_hist = _taxa_mtf(modalidade_taxa, tf_a)
    taxa_col = (
        f"{taxa_hist[True]['taxa_acerto'] * 100:.0f}%/{taxa_hist[False]['taxa_acerto'] * 100:.0f}%"
        if taxa_hist is not None else "—"
    )

    for i, symbol in enumerate(symbols):
        progress.progress((i + 1) / len(symbols), text=f"Analisando {symbol} ({i+1}/{len(symbols)})...")
        mtf = cached_mtf(symbol, count, confirmation, context_tfs, modality, source, params)
        result_a = mtf.results[tf_a]
        result_b = mtf.results[tf_b]

        if result_a.error or result_b.error:
            err = (result_a.error or result_b.error or "")[:60]
            rows.append({"Ativo": symbol, "Confirmado": "ERRO", "Direção": "ERRO", col_a: None,
                        col_b: None, "Score Geral": None, "Exaustão": "",
                        "Taxa hist. (conf./não)": taxa_col, "Setup": err,
                        "Entrada": None, "Stop": None, "Alvo 1": None, "Quantidade": None, "Total (R$)": None})
            if source == "Yahoo Finance":
                time.sleep(_PAUSA_YAHOO)
            continue

        # mesma leitura que decide o veredito da Análise individual — sem
        # exigir mtf.confirmed pra mostrar o plano (ver render_veredito)
        plano = portadora_do_plano(result_a.signals, modality)
        risk = plano.risk if plano else None

        if modality == ALL_MODALITIES_OPTION:
            score_a = round(overall_score(result_a.signals), 1)
            score_b = round(overall_score(result_b.signals), 1)
            confluence_a = next(s for s in result_a.signals if s.name == "Confluência")
            setup_text = f"Score geral (média de 5 leituras) — {confluence_a.setup}"
            direction_label = overall_direction(result_a.signals).value
        else:
            conf_a = next(s for s in result_a.signals if s.name == modality)
            conf_b = next(s for s in result_b.signals if s.name == modality)
            score_a = round(conf_a.score, 1)
            score_b = round(conf_b.score, 1)
            setup_text = conf_a.setup
            direction_label = conf_a.direction.value

        qty = None
        total = None
        if risk_budget and risk and risk.entry is not None and risk.stop is not None:
            risk_per_share = abs(risk.entry - risk.stop)
            if risk_per_share > 0:
                qty = int(risk_budget // risk_per_share)
                total = round(qty * risk.entry, 2) if qty > 0 else 0.0

        score_geral = round((score_a + score_b) / 2, 1)

        # Exaustão de IFR alinhada em vários timeframes é o filtro mais
        # seletivo da tabela: é raro, e quando aparece diz mais que um
        # score alto isolado. Fica como COLUNA, não como ordenação — o
        # ranking segue no Score Geral pelo motivo do docstring acima.
        exaustao_tf = rsi_extremes_across_timeframes(mtf, params)
        n_extremos = exaustao_tf["alinhamento"]
        if n_extremos >= 2:
            seta = "↑" if exaustao_tf["direcao"] == Direction.BUY.value else "↓"
            exaustao = f"🎯 {n_extremos} TFs {seta}"
        elif n_extremos == 1:
            seta = "↑" if exaustao_tf["direcao"] == Direction.BUY.value else "↓"
            exaustao = f"1 TF {seta}"
        else:
            exaustao = ""

        rows.append({
            "Ativo": symbol,
            "Confirmado": "✅" if mtf.confirmed else "❌",
            "Direção": direction_label,
            col_a: score_a,
            col_b: score_b,
            "Score Geral": score_geral,
            "Exaustão": exaustao,
            "Taxa hist. (conf./não)": taxa_col,
            "Setup": setup_text,
            "Entrada": round(risk.entry, 2) if risk and risk.entry else None,
            "Stop": round(risk.stop, 2) if risk and risk.stop else None,
            "Alvo 1": round(risk.target_1, 2) if risk and risk.target_1 else None,
            "Quantidade": qty,
            "Total (R$)": total,
        })
        if source == "Yahoo Finance":
            time.sleep(_PAUSA_YAHOO)

    progress.empty()
    result = pd.DataFrame(rows)
    if "Score Geral" in result.columns:
        result = result.sort_values("Score Geral", ascending=False, na_position="last")
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

# Fonte padrão conforme o ambiente: com a API do homelab configurada ela é o
# caminho recomendado; sem ela, cair no Homelab só produziria uma tela de
# erro na primeira visita. Fixado aqui, e não via `index=` no `st.radio`,
# porque o widget tem `key="source_select"` — com key, o Streamlit ignora o
# `index` a partir do segundo rerun e passa a mandar o session_state.
if "source_select" not in st.session_state:
    st.session_state.source_select = (
        "Homelab (API)" if daytrade_smc.ACOES_API_URL else "Yahoo Finance"
    )

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
        ("rsi_periodo", "Período do IFR", 2, 200, 1),
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
    "IFR": [
        ("rsi_sobrevenda", "Exaustão vendedora — IFR abaixo de", 0.0, 50.0, 1.0),
        ("rsi_sobrecompra", "Exaustão compradora — IFR acima de", 50.0, 100.0, 1.0),
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
    "multiplicador_concordancia": "Multiplicador do score da confluência por quantas das 4 categorias estruturais concordam (0 a 4). O IFR não entra nessa conta.",
    "rsi_periodo": "Período do IFR. 14 é a convenção de Wilder, a mesma do MT5 e do TradingView.",
    "rsi_sobrevenda": "Abaixo disso o IFR marca exaustão vendedora e aponta COMPRA. O padrão 10 é extremo de propósito: dispara pouco, mas quando dispara vale.",
    "rsi_sobrecompra": "Acima disso o IFR marca exaustão compradora e aponta VENDA. Ver a observação do limiar de sobrevenda.",
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
# Modo — no corpo, não na sidebar (2026-08-06: a navegação entre os 4 modos
# sentia fragmentada com o seletor escondido lá embaixo, entre outras
# configurações). Calculado ANTES do bloco da sidebar porque os widgets lá
# dentro (ex: qual seletor de ativo aparece) dependem de `mode` já estar
# definido — a ordem de EXECUÇÃO no script decide a ordem dentro de cada
# container, não a posição visual entre corpo e sidebar, então isto roda
# aqui e mesmo assim aparece no topo do corpo, acima do título.
#
# `st.segmented_control`, não `st.tabs`: tabs executam o corpo de TODAS as
# abas a cada rerun — é só disposição visual, não controle de fluxo. Trocar
# o if/elif atual (mais abaixo) por tabs faria Scanner/Análise
# individual/Retroativa rodarem seus fetches (alguns custosos — o Yahoo tem
# rate limit documentado) toda vez que qualquer widget mudasse, mesmo com
# outra aba visível. segmented_control preserva o if/elif: só o modo
# escolhido executa, exatamente como o st.radio que ele substitui.
# Deep linking — aplica ANTES do mode widget nascer pra não conflitar.

def _render_breadcrumb(current: str) -> None:
    """Navegação tipo breadcrumb — mostra onde o usuário está."""
    breadcrumbs = {
        "Dashboard": "📊 Dashboard",
        "Scanner": "🔍 Scanner",
        "Análise individual": "📈 Análise",
        "Verificação retroativa": "🕵️ Retroativa",
        "Assertividade": "📉 Assertividade",
    }
    items = ["Dashboard"] if current == "Dashboard" else ["Dashboard", current]
    st.caption(" > ".join(breadcrumbs.get(i, i) for i in items))


def _render_share_button() -> None:
    """Botão de compartilhar — copia URL com params da view atual."""
    # Monta URL com params do estado atual
    params_dict = {}
    if "mode_select" in st.session_state:
        params_dict["mode"] = st.session_state.mode_select
    if "symbol_select" in st.session_state:
        params_dict["symbol"] = st.session_state.symbol_select
    if "perfil_select" in st.session_state:
        params_dict["perfil"] = st.session_state.perfil_select
    if "modality_select" in st.session_state:
        params_dict["modality"] = st.session_state.modality_select

    # Atualiza query params na URL
    if params_dict:
        st.query_params.update(params_dict)
        url = f"https://acoes.dondon.services/?{'&'.join(f'{k}={v}' for k, v in params_dict.items())}"
    else:
        url = "https://acoes.dondon.services/"

    # Botão de compartilhar (copia URL)
    if st.button("🔗 Copiar link", key="share_btn", help="Copia link para esta view"):
        st.code(url, language=None)
        st.success("Link copiado! Cole onde quiser compartilhar.")
        st.balloons()


def _apply_deep_link() -> None:
    """Aplica deep linking dos query params pro session_state.
    Roda ANTES dos widgets nascerem pra não dar conflito."""
    qp = st.query_params

    # Modo
    if qp.get("mode") and qp["mode"] in [
        "Dashboard", "Scanner", "Análise individual", "Verificação retroativa", "Assertividade",
    ]:
        st.session_state.mode_select = qp["mode"]

    # Símbolo
    if qp.get("symbol") and qp["symbol"] in st.session_state.get("watchlist", []):
        st.session_state.symbol_select = qp["symbol"]

    # Perfil
    if qp.get("perfil") and qp["perfil"] in st.session_state.get("perfis", {}):
        st.session_state.perfil_select = qp["perfil"]

    # Modalidade
    if qp.get("modality") and qp["modality"] in [
        "Confluência", "SMC", "Price Action", "Médias Móveis", "VWAP", "IFR",
    ]:
        st.session_state.modality_select = qp["modality"]



_apply_deep_link()

# ========================================================================
mode = st.segmented_control(
    "Modo",
    ["Dashboard", "Scanner", "Análise individual", "Verificação retroativa", "Assertividade",
     "Mini Índice (WINFUT)"],
    key="mode_select", default="Dashboard", required=True,
    # `required=True` é obrigatório aqui, não estético: sem ele,
    # segmented_control deixa clicar no pill já selecionado pra DESMARCAR e
    # devolver None — e o if/elif abaixo termina num `else` que assume
    # Assertividade. Sem o required, um duplo-clique acidental trocaria de
    # modo em silêncio pro usuário achar que ainda está na tela anterior.
)

def render_dashboard(source: str, count: int, risk_budget: float | None, params: AnalysisParams,
                      perfis: list[str], style: str) -> None:
    """Tela principal: top oportunidades de relance, organizadas por perfil.

    Cada card é uma decisão — entrada, stop, alvo, score, perfil. Sem
    parameter tweaking: isso aqui é pra operar, não pra calibrar. Quem quer
    calibrar desce pro Scanner ou pra Análise individual."""
    confirmation = estilo(style)["confirmation"]
    context_tfs = estilo(style)["context"]
    tf_entrada = confirmation[0]

    # Breadcrumb + Share
    _render_breadcrumb("Dashboard")
    _render_share_button()

    st.markdown("### 🎯 Melhores oportunidades agora")
    st.caption(f"Scanner sobre {len(st.session_state.watchlist)} ativos · {style} · "
               f"perfil ativo: **{st.session_state.perfil_select}** · "
               f"fonte: {SOURCE_LABELS.get(source, source)}")

    # Perfis que o worker está rodando — mostra aba de cada um
    if len(perfis) > 1:
        perfil_tabs = st.tabs(["Todos"] + perfis)
    else:
        perfil_tabs = [st.container()]

    for idx, tab in enumerate(perfil_tabs):
        with tab:
            perfil_filtro = None if idx == 0 else perfis[idx - 1]

            # Rodar o scanner (cacheado)
            with st.spinner("Analisando oportunidades..."):
                df = run_scanner(
                    st.session_state.watchlist, style, "Confluência",
                    source, count, risk_budget,
                    params if perfil_filtro is None else st.session_state.perfis.get(perfil_filtro, params),
                )

            # Filtrar só operáveis (entrada válida)
            operáveis = df[df["Entrada"].notna()].copy() if "Entrada" in df.columns else df.head(0)

            if operáveis.empty:
                st.info("Nenhum sinal operável neste momento. Tente outro perfil ou aguarde o próximo ciclo.")
                continue

            # Top 5 por score
            top = operáveis.head(5)

            for _, row in top.iterrows():
                _render_oportunidade_card(row, symbol=row["Ativo"], style=style,
                                          source=source, count=count, risk_budget=risk_budget,
                                          params=params, perfil=perfil_filtro or st.session_state.perfil_select)


def _render_oportunidade_card(row: pd.Series, symbol: str, style: str, source: str,
                               count: int, risk_budget: float | None, params: AnalysisParams,
                               perfil: str) -> None:
    """Um card de oportunidade — tudo que pra decidir está na cara.

    Layout: badge de direção + ativo à esquerda, níveis de preço no centro,
    ações à direita. Visual limpo, decisão em 3 segundos."""
    direcao = row.get("Direção", "NEUTRO")
    if direcao == "COMPRA":
        cor = "#2ed3a3"
        icone = "🟢"
        acao = "COMPRAR"
        seta = "▲"
    else:
        cor = "#ff5470"
        icone = "🔴"
        acao = "VENDER"
        seta = "▼"

    score = row.get("Score Geral", 0) or 0
    entrada = row.get("Entrada")
    stop = row.get("Stop")
    alvo = row.get("Alvo 1")
    qty = row.get("Quantidade")
    total = row.get("Total (R$)")
    rr = (alvo - entrada) / (entrada - stop) if entrada and stop and alvo and (entrada - stop) != 0 else 0

    # Qualidade visual do score
    if score >= 80:
        qualidade = "🌟 Excepcional"
        q_cor = "#f0b429"
    elif score >= 60:
        qualidade = "✅ Boa"
        q_cor = "#2ed3a3"
    else:
        qualidade = "⚡ Regular"
        q_cor = "#8291a1"

    # Card container
    st.markdown(
        f'<div style="border:1px solid {cor}33; border-left:4px solid {cor}; '
        f'border-radius:8px; padding:12px 16px; background:{cor}08; margin-bottom:8px;">',
        unsafe_allow_html=True,
    )

    # Linha 1: direção + ativo + score + setup
    c1, c2, c3, c4 = st.columns([1.2, 2, 1.5, 1.5])
    with c1:
        st.markdown(f'<span style="font-size:20px">{icone}</span> **{acao}**', unsafe_allow_html=True)
    with c2:
        st.markdown(f"### {symbol}")
        st.caption(f"{row.get('Setup', '')[:50]}")
    with c3:
        st.metric("Score", f"{score:.0f}", qualidade, label_visibility="visible")
    with c4:
        st.caption(f"RR: **1:{rr:.1f}**" if rr else "")
        st.caption(f"perfil: `{perfil}`")

    # Linha 2: níveis de preço
    if entrada and stop and alvo:
        cols = st.columns(5)
        cols[0].metric("Entrada", f"R$ {entrada:.2f}")
        cols[1].metric("Stop", f"R$ {stop:.2f}", f"{(stop-entrada)/entrada*100:+.2f}%",
                       delta_color="inverse")
        cols[2].metric("Alvo", f"R$ {alvo:.2f}", f"{(alvo-entrada)/entrada*100:+.2f}%")
        if qty and qty > 0:
            cols[3].metric("Qtd", f"{int(qty)}")
            cols[4].metric("Total", f"R$ {total:.0f}" if total else "—")
        elif risk_budget:
            risk_per_share = abs(entrada - stop)
            if risk_per_share > 0:
                qtd_calc = int(risk_budget // risk_per_share)
                cols[3].metric("Qtd", f"{qtd_calc}")
                cols[4].metric("Risco", f"R$ {qtd_calc * risk_per_share:.0f}")

    # Linha 3: ações
    c_esq, c_dir = st.columns([1, 4])
    with c_esq:
        if st.button("📊 Ver análise", key=f"dash_ver_{symbol}_{perfil}_{row.name}",
                     use_container_width=True):
            st.session_state.jump_to_symbol = symbol
            st.rerun()
    with c_dir:
        # Link compartilhável direto pro ativo
        link = f"https://acoes.dondon.services/?mode=Análise+individual&symbol={symbol}&perfil={perfil}"
        if st.button(f"🔗 Copiar link de {symbol}", key=f"dash_share_{symbol}_{perfil}_{row.name}",
                     use_container_width=True):
            st.code(link, language=None)
            st.caption("Link para esta oportunidade — cole onde quiser.")

    st.markdown('</div>', unsafe_allow_html=True)


# ========================================================================
# Sidebar
# ========================================================================
with st.sidebar:
    st.markdown("## 📊 Day Trade SMC")
    st.caption("SMC · Price Action · Médias Móveis · VWAP · IFR")

    st.markdown("### Fonte de dados")
    source = st.radio(
        "Fonte", DATA_SOURCES, key="source_select", horizontal=True,
        help="\"Homelab (API)\" lê candles pela API do homelab, alimentada por um scraper "
             "MT5 que roda continuamente numa VM — dado real, poucos segundos de atraso. "
             "É o caminho recomendado. \"Yahoo Finance\" funciona em qualquer lugar, sem "
             "depender de nada seu estar no ar, mas o dado nasce ~15-20min atrasado.",
    )
    if source == "Homelab (API)":
        if not daytrade_smc.ACOES_API_URL:
            st.caption(
                "⚠️ ACOES_API_URL não está configurada (st.secrets ou variável de "
                "ambiente) — esta fonte vai dar erro. Use Yahoo Finance enquanto isso."
            )
        else:
            ultima = _ultima_vela()
            if ultima is None:
                st.caption("🏠 API do homelab · não consegui ler o `/status` agora.")
            else:
                st.caption(
                    "🏠 Última vela: "
                    f"{ultima.tz_convert('America/Sao_Paulo').strftime('%d/%m/%Y %H:%M')} · "
                    "congela com o mercado fechado, isso é o esperado."
                )

    st.markdown("### Estilo de operação")
    style = st.radio(
        "Estilo", list(STYLES.keys()), key="style_select", horizontal=True,
        help="Day Trade confirma em M15+H1 (posições no mesmo dia). "
             "Swing Trade confirma em Diário+Semanal (posições de dias a semanas), com H4 como contexto de timing de entrada.",
    )
    conf_a, conf_b = estilo(style)["confirmation"]

    st.markdown("### Modalidade")
    modality = st.selectbox(
        "Qual leitura usar como base da recomendação", MODALITY_CHOICES, key="modality_select",
        help="Confluência combina as 4 categorias estruturais (SMC, Price Action, Médias Móveis, "
             "VWAP). SMC/Price Action/Médias Móveis/VWAP/IFR usam só a leitura isolada daquela "
             "categoria. O IFR é leitura de EXAUSTÃO, contrária por natureza: só aponta direção "
             "em ≤10 ou ≥90, fica NEUTRO quase sempre (de propósito) e por isso NÃO entra nem na "
             "Confluência nem no Score Geral. \"Todas as modalidades\" calcula um SCORE GERAL "
             "(média das 5 leituras agregáveis) e usa ele — não uma única leitura — pra decidir "
             "a confirmação e ordenar o Scanner.",
    )

    # "Ativo para análise" fica SEMPRE visível — é o que se mexe todo dia.
    # Gerenciar a watchlist é configuração de uma vez só, então desce pro
    # expander. Separação sugerida pela auditoria do projeto original.
    if mode in ("Análise individual", "Verificação retroativa", "Dashboard"):
        st.markdown("### Ativo")
        st.selectbox("Ativo para análise", st.session_state.watchlist, key="symbol_select")

    with st.expander("Gerenciar watchlist", expanded=False):
        st.caption(f"{len(st.session_state.watchlist)} ativo(s) monitorado(s)")
        new_symbol = st.text_input("Adicionar ativo (ex: VALE3)", key="new_symbol_input")
        if st.button("Adicionar", use_container_width=True) and new_symbol.strip():
            value = new_symbol.strip().upper().replace(" ", "")
            if value not in st.session_state.watchlist:
                st.session_state.watchlist.append(value)
                _persist_watchlist()
            st.rerun()

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
    # O estilo pode trocar os limiares do IFR (Swing opera 20/80, Day
    # Trade 10/90) quando o perfil não escolheu os seus. A comparação
    # do "alterado, não salvo" aplica o MESMO ajuste no perfil salvo,
    # senão todo perfil apareceria como alterado em Swing sem ninguém
    # ter mexido em nada.
    params = params_para_estilo(_params_da_sessao(), style)
    _salvo = st.session_state.perfis.get(perfil)
    _alterado = _salvo is None or params != params_para_estilo(_salvo, style)
    st.caption(f"hash `{params.params_hash()[:8]}`" + (" · **alterado, não salvo**" if _alterado else ""))
    if (params.rsi_sobrevenda, params.rsi_sobrecompra) != (
        _params_da_sessao().rsi_sobrevenda, _params_da_sessao().rsi_sobrecompra
    ):
        st.caption(
            f"IFR ajustado pro estilo: exaustão em "
            f"{params.rsi_sobrevenda:.0f}/{params.rsi_sobrecompra:.0f}. "
            "Salve o perfil com outro par pra fixar."
        )

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
              f"{', '.join(TIMEFRAME_LABELS[tf] for tf in estilo(style)['context'])} aparece como contexto adicional.")
    count = st.slider(estilo(style)["count_label"], min_value=50, max_value=400, value=250, step=10)
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
    elif mode == "Scanner":
        run_scanner_clicked = st.button("🔍 Rodar scanner", type="primary", use_container_width=True)
    # Dashboard não precisa de controles extra — roda automático

    # Footer da sidebar — ajuda rápida
    with st.expander("ℹ️ Como usar", expanded=False):
        st.markdown("""
        **Dashboard**: melhores oportunidades agora, organizadas por perfil.
        **Scanner**: tabela com todos os ativos da watchlist.
        **Análise individual**: gráfico + 6 leituras de um ativo.
        **Retroativa**: veja como um sinal teria se saído no passado.
        **Assertividade**: taxa de acerto medida dos sinais gravados.

        💡 **Dica**: use 'Copiar link' pra compartilhar qualquer view.
        """)


# ========================================================================
# Corpo principal
# ========================================================================
# Título contextual por modo (mais limpo que título fixo)
if mode == "Dashboard":
    st.title("📊 Oportunidades agora")
elif mode == "Scanner":
    st.title("🔍 Scanner de mercado")
elif mode == "Análise individual":
    st.title("📈 Análise técnica")
elif mode == "Verificação retroativa":
    st.title("🕵️ Verificação retroativa")
elif mode == "Assertividade":
    st.title("📉 Assertividade medida")
else:
    st.title("📊 Day Trade SMC")

# O aviso ACOMPANHA a fonte. Fixo no texto do Yahoo, ele mentia toda vez que
# a fonte era o homelab: anunciava 20 minutos de atraso num dado de poucos
# segundos, e um aviso que mente é pior que nenhum — o usuário aprende a
# ignorar a faixa amarela inteira.
if source == "Homelab (API)":
    st.warning(
        "⚠️ **Dado real do MT5, com poucos segundos de atraso** — mas é **candle "
        "fechado, não book.** Use para **viés e estrutura** (tendência, níveis, força "
        "relativa entre ativos). Antes de entrar numa operação, confirme o preço e a "
        "liquidez na tela da sua corretora.",
        icon="🏠",
    )
else:
    st.warning(
        "⚠️ **Dados do Yahoo Finance com atraso de ~15-20 minutos.** Use esta ferramenta para "
        "**viés e estrutura** (tendência, níveis, força relativa entre ativos) — **nunca para o "
        "preço/timing exato de execução.** Antes de entrar numa operação, confirme o preço real "
        "no ProfitChart ou na tela da sua corretora.",
        icon="⏱️",
    )

if mode == "Dashboard":
    render_dashboard(source, count, risk_budget, params,
                     perfis=[p for p in sorted(st.session_state.perfis)
                             if p != DEFAULT_PROFILE_NAME][:4],  # top 4 perfis custom
                     style=style)

elif mode == "Scanner":
    _render_breadcrumb("Scanner")
    _render_share_button()
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

        def _color_exaustao(val):
            if not val:
                return "color: #8291a1"
            cor = "#2ed3a3" if "↑" in str(val) else "#ff5470"
            return f"color: {cor}; font-weight: 600"

        vista = result_df
        if "Exaustão" in result_df.columns:
            n_exaustao = int((result_df["Exaustão"] != "").sum())
            if n_exaustao:
                st.caption(
                    f"🎯 {n_exaustao} ativo(s) com exaustão de IFR. Exaustão simultânea em "
                    "2+ timeframes é rara e vale mais que score alto isolado."
                )
                if st.checkbox("Só com exaustão de IFR", value=False, key="scan_f_exaustao"):
                    vista = result_df[result_df["Exaustão"] != ""]

        st.dataframe(
            vista.style.map(_color_direction, subset=["Direção"])
                       .map(_color_exaustao, subset=["Exaustão"]),
            hide_index=True, use_container_width=True, height=min(450, 45 + 35 * len(vista)),
            column_config={
                "Exaustão": st.column_config.TextColumn(
                    "Exaustão IFR", width="small",
                    help="Timeframes em exaustão simultânea na mesma direção",
                ),
            },
        )

        st.markdown("#### Abrir análise completa de um ativo")
        pick = st.selectbox("Ativo", result_df["Ativo"].tolist(), key="scanner_pick_select")
        if st.button("Ver gráfico e as 6 leituras completas"):
            st.session_state.jump_to_symbol = pick
            st.rerun()
    else:
        st.info("Clique em **Rodar scanner** na barra lateral para analisar todos os ativos da watchlist.")

elif mode == "Análise individual":
    symbol = st.session_state.symbol_select

    if auto_refresh:
        st.caption(f"🔄 Atualizando automaticamente a cada {refresh_interval}s")
        _AUTO_REFRESH_FRAGMENTS[refresh_interval](symbol, style, modality, source, count, risk_budget, params, perfil)
    else:
        _render_breadcrumb("Análise individual")
        _render_share_button()
        render_individual_analysis(symbol, style, modality, source, count, risk_budget, params, perfil)

elif mode == "Verificação retroativa":
    _render_breadcrumb("Verificação retroativa")
    symbol = st.session_state.symbol_select
    render_retro_check(symbol, style, modality, source, count, params)

elif mode == "Assertividade":
    _render_breadcrumb("Assertividade")
    render_assertividade(sorted(st.session_state.perfis))

else:  # Mini Índice (WINFUT)
    # O mini índice NÃO existe no Yahoo: "WINFUT" atravessa `yahoo_symbol`
    # intacto e o Yahoo devolve "símbolo não encontrado", que pareceria bug
    # da ferramenta. Bloqueia antes de tentar, dizendo o que fazer.
    if source != "Homelab (API)":
        st.warning(
            "O Mini Índice só existe pela **Homelab (API)** — ele vem do MetaTrader 5 "
            "pelo scraper, e o Yahoo Finance não tem esse contrato. Troque a fonte na "
            "barra lateral.",
            icon="🏠",
        )
    elif WINFUT_SYMBOL not in st.session_state.watchlist:
        st.warning(
            f"**{WINFUT_SYMBOL}** ainda não está na watchlist, então o scraper não está "
            "coletando as velas dele. Adicione em *Gerenciar watchlist* na barra lateral "
            "e confira no scraper da VM se `SCRAPER_SYMBOL_MT5` aponta pro nome do "
            "contrato no seu MT5 (contínuo `WIN$` ou o vencimento vigente).",
            icon="📋",
        )
    else:
        conf_win = ", ".join(TIMEFRAME_LABELS[tf] for tf in WINFUT_CONFIRMATION_TIMEFRAMES)
        ctx_win = ", ".join(TIMEFRAME_LABELS[tf] for tf in WINFUT_CONTEXT_TIMEFRAMES)
        st.caption(
            f"📈 Contrato futuro do mini índice · leitura: {modality} · perfil: {perfil} · "
            f"{count} candles · recomendação exige {conf_win} concordando · contexto: {ctx_win}"
        )
        st.info(
            "**Contrato futuro, não é ação.** Alavancagem e horário de negociação são "
            "diferentes, e o contrato vira de vencimento periodicamente — o histórico é "
            "contínuo aqui porque o scraper traduz o nome, não porque o papel é o mesmo.",
            icon="⚠️",
        )
        render_individual_analysis(
            WINFUT_SYMBOL, WINFUT_STYLE, modality, source, count, risk_budget,
            params_para_estilo(params, WINFUT_STYLE), perfil,
        )
