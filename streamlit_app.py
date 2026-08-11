"""
streamlit_app.py

Interface WEB para o motor de análise em `daytrade_smc.py`. Não tem lógica
de análise nenhuma — só importa as funções do motor e desenha por cima.

Seis rotas, agrupadas em quatro pills no topo do corpo. Cada uma tem URL
própria (`st.navigation` + `st.Page`), então recarregar, favoritar e o
voltar/avançar do navegador funcionam — ver o bloco "Rotas" mais abaixo.

    🎯 Oportunidades  /               os melhores sinais operáveis agora
    🔍 Scanner        /scanner        a watchlist inteira, ranqueada
    📈 Ativo          /ativo          gráfico + as 6 leituras de um ativo
                      /retroativa     como o sinal teria se saído numa data
    📋 Sinais         /acompanhar     triagem do que o worker gravou
                      /assertividade  taxa de acerto medida do histórico

O Mini Índice não tem rota própria: `WINFUT` é uma opção do seletor de ativo,
e `/ativo` troca sozinho para os timeframes dele.

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

# Paleta única do app. Antes destes nomes, os mesmos quatro hexes estavam
# copiados como literal em nove lugares (cards, styler do Scanner, veredito,
# badge de IFR, caixa de desfecho da retroativa, linha de feedback...), então
# mexer numa cor exigia caçar todas as cópias e sempre sobrava uma.
# Os mesmos valores estão em `.streamlit/config.toml`, que declara o tema
# escuro que `build_chart` já assumia — mudar aqui pede mudar lá.
PALETA = {
    "compra": "#2ed3a3",
    "venda": "#ff5470",
    "neutro": "#8291a1",
    "alerta": "#f0b429",
    "fundo": "#0a0e13",
    "texto": "#e7ecf1",
    # séries do gráfico — sem leitura semântica, só precisam ser distinguíveis
    "ema_9": "#5ec8ff",
    "ema_21": "#a78bfa",
    "ema_200": "#ff8a3d",
}

DIRECTION_COLOR = {
    Direction.BUY: PALETA["compra"],
    Direction.SELL: PALETA["venda"],
    Direction.NEUTRAL: PALETA["neutro"],
}

SOURCE_LABELS = {
    "Homelab (API)": "Homelab (API, quase em tempo real)",
    "Yahoo Finance": "Yahoo Finance (atraso ~15-20min)",
}


def _chips(fatos: list[tuple[str, str]]) -> None:
    """Os fatos da tela como etiquetas, num bloco só.

    Existe porque cada view abria com uma `st.caption` de seis a oito fatos
    concatenados por "·" — uma frase longa em fonte pequena, que é o formato
    em que ninguém lê o quarto item. Como rótulo curto + valor em negrito,
    o olho acha o que procura sem ler o resto."""
    marcacao = "".join(
        f'<span style="display:inline-block; margin:0 6px 8px 0; padding:2px 9px; '
        f'border-radius:10px; background:{PALETA["neutro"]}22; font-size:12px; '
        f'white-space:nowrap;"><span style="opacity:.65">{rotulo}</span> '
        f"<b>{valor}</b></span>"
        for rotulo, valor in fatos if valor not in (None, "")
    )
    st.markdown(marcacao, unsafe_allow_html=True)

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
            name=symbol, increasing_line_color=PALETA["compra"], decreasing_line_color=PALETA["venda"],
            increasing_fillcolor=PALETA["compra"], decreasing_fillcolor=PALETA["venda"],
        )
    )

    ema_colors = {"ema_9": PALETA["ema_9"], "ema_21": PALETA["ema_21"],
                  "ema_50": PALETA["alerta"], "ema_200": PALETA["ema_200"]}
    for col, color in ema_colors.items():
        fig.add_trace(
            go.Scatter(x=x, y=context.emas[col], mode="lines", name=col.upper().replace("_", " "),
                       line=dict(color=color, width=1.3))
        )

    fig.add_trace(
        go.Scatter(x=x, y=context.vwap_series, mode="lines", name="VWAP",
                   line=dict(color=PALETA["compra"], width=1.6, dash="dot"))
    )

    swing_highs = [(x[s.index], s.price) for s in context.swings if s.kind == "HIGH"]
    swing_lows = [(x[s.index], s.price) for s in context.swings if s.kind == "LOW"]
    if swing_highs:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in swing_highs], y=[p[1] for p in swing_highs], mode="markers",
            name="Swing High", marker=dict(symbol="triangle-down", size=7, color=PALETA["venda"]),
        ))
    if swing_lows:
        fig.add_trace(go.Scatter(
            x=[p[0] for p in swing_lows], y=[p[1] for p in swing_lows], mode="markers",
            name="Swing Low", marker=dict(symbol="triangle-up", size=7, color=PALETA["compra"]),
        ))

    for kind, symb, color in [("BOS", "diamond", PALETA["alerta"]), ("CHOCH", "star", "#ffffff")]:
        pts = [e for e in context.events if e.kind == kind]
        if not pts:
            continue
        fig.add_trace(go.Scatter(
            x=[x[e.index] for e in pts],
            y=[df["high"].iloc[e.index] * 1.003 if e.direction == Direction.BUY else df["low"].iloc[e.index] * 0.997 for e in pts],
            mode="markers+text", name=kind,
            marker=dict(symbol=symb, size=11, color=color, line=dict(width=1, color=PALETA["fundo"])),
            text=[kind] * len(pts), textposition="top center", textfont=dict(size=9, color=color),
        ))

    # O mesmo parâmetro que o motor usou pra DECIDIR o FVG precisa valer aqui
    # pra DESENHAR — senão o gráfico mostra uma zona que o score não enxerga.
    fvg = find_fvg_zone(df, context.params.fvg_max_idade)
    if fvg is not None:
        color = PALETA["compra"] if fvg["kind"] == "ALTA" else PALETA["venda"]
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
        levels = [("Entrada", r.entry, PALETA["texto"]), ("Stop", r.stop, PALETA["venda"]),
                  ("Alvo 1", r.target_1, PALETA["compra"]), ("Alvo 2", r.target_2, PALETA["compra"])]
        for label, price, color in levels:
            if price is None:
                continue
            fig.add_hline(y=price, line=dict(color=color, width=1.4, dash="solid" if label != "Alvo 2" else "dash"),
                          annotation_text=f"{label}: {price:.2f}", annotation_position="right",
                          annotation=dict(font=dict(size=10, color=color)))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=PALETA["fundo"], plot_bgcolor=PALETA["fundo"],
        height=560, margin=dict(l=10, r=10, t=30, b=10),
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0),
        font=dict(family="IBM Plex Mono, monospace", size=11, color=PALETA["neutro"]),
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
    q = quality(signal.score, params)

    if q == "OPORTUNIDADE EXCEPCIONAL" and signal.direction != Direction.NEUTRAL:
        action = "COMPRA" if signal.direction == Direction.BUY else "VENDA"
        st.markdown(
            f'<div style="border:2px solid {PALETA["alerta"]}; border-radius:8px; padding:10px 16px; '
            f'background:{PALETA["alerta"]}22; margin-bottom:12px; text-align:center;">'
            f'<span style="font-size:18px;">🌟 <b style="color:{PALETA["alerta"]};">OPORTUNIDADE EXCEPCIONAL</b> · '
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
            f'<div style="border-left:4px solid {PALETA["neutro"]}; border-radius:4px; padding:14px 18px; '
            f'background:{PALETA["neutro"]}14; margin-bottom:16px;">'
            f'<div style="font-size:20px; font-weight:600; color:{PALETA["neutro"]};">SEM OPERAÇÃO EM {symbol}</div>'
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
    "ALVO_1": ("✅ Bateu o Alvo 1", PALETA["compra"]),
    "ALVO_2": ("✅ Bateu o Alvo 2", PALETA["compra"]),
    "STOP": ("❌ Bateu o Stop", PALETA["venda"]),
    "EM_ABERTO": ("⏳ Ainda em aberto", PALETA["alerta"]),
    "SEM_SINAL": ("— Sem sinal operável nesta data", PALETA["neutro"]),
    # Não é acerto nem erro: a vela seguinte abriu além do stop, então a
    # operação não chegou a existir. Contar isso como stop seria inventar uma
    # perda que ninguém teve; contar como acerto, o oposto. Fica fora da conta.
    "SEM_ENTRADA": ("— Gap abriu além do stop; sem operação", PALETA["neutro"]),
    "SEM_DADO_FUTURO": ("⏳ Sem candles seguintes disponíveis ainda", PALETA["neutro"]),
}


def render_retro_check(symbol: str, style: str, modality: str, source: str, count: int, params: AnalysisParams = DEFAULT_PARAMS) -> None:
    # A explicação de como funciona virou `help=` do botão: era uma caption
    # de três linhas no topo, relida a cada visita por quem já sabia.
    all_tfs = list(dict.fromkeys([*estilo(style)["confirmation"], *estilo(style)["context"]]))
    col1, col2, col3 = st.columns([2, 2, 1], vertical_alignment="bottom")
    with col1:
        check_tf = st.selectbox("Timeframe a verificar", all_tfs, format_func=lambda tf: TIMEFRAME_LABELS[tf])
    with col2:
        default_date = pd.Timestamp.now(tz="America/Sao_Paulo").date() - pd.Timedelta(days=1)
        as_of_date = st.date_input("Data (fechamento até esse dia)", value=default_date)
    with col3:
        verificar = st.button(
            "🔍 Verificar", type="primary", use_container_width=True,
            help="Roda a análise usando SÓ os dados que existiam até a data escolhida "
                 "(sem espiar o futuro), depois confere o que aconteceu de verdade nos "
                 "candles seguintes — se bateu entrada, alvo ou stop.",
        )

    if verificar:
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

    # Perfil e Ativo ficam à vista — são os dois recortes que se troca o
    # tempo todo. Origem e Período foram pro popover: mexe-se neles uma vez
    # por sessão, e ocupavam metade de uma barra de filtros de quatro
    # colunas acima de tudo o que importa nesta tela.
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        f_perfil = st.selectbox("Perfil", ["Todos"] + perfis, key="assert_perfil")
    with col2:
        f_symbol = st.selectbox("Ativo", ["Todos"] + st.session_state.watchlist, key="assert_symbol")
    with col3:
        with st.popover("⚙️ Mais filtros", use_container_width=True):
            # 'backfill' segue como opção de filtro (a reconstrução em massa
            # não é mais a fonte principal desde 2026-08-06, mas o comando
            # ainda existe pra casos pontuais — ex: bootstrapar histórico de
            # um ativo novo). 'manual' é o caminho principal agora: o botão
            # "💾 Salvar este sinal" no veredito de cada análise.
            f_origem = st.selectbox("Origem", ["Todas", "worker", "manual", "backfill"],
                                    key="assert_origem")
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

    # Um recorte por vez, escolhido num seletor. Eram cinco expanders
    # empilhados abaixo da tabela geral, todos fechados: sete tabelas na
    # mesma página, e a comparação entre dois recortes exigia abrir os dois
    # e rolar. Sendo todas a mesma tabela com outro agrupamento, o seletor
    # troca o conteúdo no lugar.
    disponiveis = {rotulo: chave for chave, rotulo in RECORTE_LABELS.items()
                   if stats.get(chave)}
    st.markdown("### Assertividade por recorte")
    recorte = st.segmented_control(
        "Recorte", ["Tipo de análise"] + list(disponiveis), key="assert_recorte",
        default="Tipo de análise", required=True, label_visibility="collapsed",
    ) or "Tipo de análise"

    if recorte == "Tipo de análise":
        st.dataframe(_tabela_assertividade(stats["geral"], None),
                     hide_index=True, use_container_width=True)
    else:
        st.dataframe(_tabela_assertividade(stats[disponiveis[recorte]], recorte),
                     hide_index=True, use_container_width=True)
    st.caption(
        "**Resolvidos** é o denominador da taxa de acerto e da expectativa — sinal em "
        "aberto não conta como acerto nem como erro."
    )

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
    linhas_hist = [{
        "Vela": pd.Timestamp(s["candle_time"]).tz_convert("America/Sao_Paulo").strftime("%d/%m %H:%M"),
        "Ativo": s["symbol"],
        "TF": s["timeframe"],
        "Leitura": s["modalidade"],
        "Direção": s["direcao"],
        "Score": round(s["score"], 1),
        "Desfecho": OUTCOME_LABELS.get(s["resultado"], ("Aguardando", ""))[0] if s["resultado"] else "Aguardando",
        "MTF": "Sim" if s["mtf_confirmado"] else "Não",
        "Entrada": s["entrada"],
        "Stop": s["stop"],
        "Alvo 1": s["alvo_1"],
        "Perfil": s["perfil"],
        "Origem": s["origem"],
    } for s in historico["signals"]]

    # Treze colunas não cabem sem rolagem horizontal, e as sete primeiras já
    # respondem "o que era e no que deu". O resto fica atrás do toggle.
    _ESSENCIAIS = ["Vela", "Ativo", "TF", "Leitura", "Direção", "Score", "Desfecho"]
    tudo = st.toggle("Ver todas as colunas", key="assert_hist_colunas")
    tabela_hist = pd.DataFrame(linhas_hist)
    st.dataframe(tabela_hist if tudo else tabela_hist[_ESSENCIAIS],
                 hide_index=True, use_container_width=True)


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
        titulo = f'<span style="color:{PALETA["neutro"]}">Nenhum timeframe em exaustão</span>'

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
        f'<div style="background:{PALETA["neutro"]}33; height:8px; border-radius:4px; margin-top:8px; position:relative;">'
        f'<div style="position:absolute; left:{sobrevenda:.0f}%; top:0; bottom:0; width:1px; background:{PALETA["neutro"]};"></div>'
        f'<div style="position:absolute; left:{sobrecompra:.0f}%; top:0; bottom:0; width:1px; background:{PALETA["neutro"]};"></div>'
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

    # Os fatos do candle viram chips, não uma frase de seis itens separados
    # por "·" — ninguém lia aquilo até o fim.
    _chips([
        ("Gráfico", TIMEFRAME_LABELS[tf_entrada]),
        ("Último candle", f"{contexto.df.index[-1].tz_convert('America/Sao_Paulo'):%d/%m %H:%M}"),
        ("ATR", f"{contexto.atr:.2f} ({contexto.atr_pct:.2f}%)"),
        ("RVOL", f"{contexto.rvol:.2f}x"),
        ("Volatilidade", contexto.volatility),
        ("Atualizado", f"{pd.Timestamp.now(tz='America/Sao_Paulo'):%H:%M:%S}"),
    ])
    render_rsi_badge(contexto, params)
    st.plotly_chart(
        build_chart(contexto, plano, symbol),
        use_container_width=True, key=f"chart_{symbol}_{tf_entrada}",
    )

    # ---- a prova: UMA tabela, com o timeframe virando coluna ----
    # Eram três tabelas empilhadas (uma por timeframe de confirmação, mais
    # uma de contexto), cada uma com o seu próprio cabeçalho e legenda. Como
    # todas têm as mesmas colunas, comparar M15 com H1 obrigava a saltar
    # entre blocos; numa tabela só, as linhas ficam lado a lado.
    st.markdown("#### O que sustenta (ou derruba) o veredito")
    linhas = []
    for tf in confirmation:
        resultado = mtf.results[tf]
        if resultado.error:
            st.warning(f"{TIMEFRAME_LABELS[tf]}: {resultado.error}")
            continue
        for s in resultado.signals:
            linhas.append({"Timeframe": TIMEFRAME_LABELS[tf], "Papel": "Confirma",
                           **_linha_leitura(s)})

    for tf in context_tfs:
        resultado = mtf.results.get(tf)
        if resultado is None or resultado.error:
            continue
        s = leitura_ativa(resultado.signals, modality)
        linhas.append({
            "Timeframe": TIMEFRAME_LABELS[tf],
            "Papel": "Contexto",
            "Leitura": s.name if s else modality,
            "Direção": (s.direction if s else overall_direction(resultado.signals)).value,
            "Score": round(s.score if s else overall_score(resultado.signals), 1),
        })

    if linhas:
        st.dataframe(pd.DataFrame(linhas), hide_index=True, use_container_width=True)
        st.caption(
            "**Confirma** são os timeframes que decidem a recomendação. "
            "**Contexto** é tendência mais ampla — não confirma nem bloqueia."
        )

    # ---- detalhe: uma leitura por vez, escolhida num seletor ----
    # O que existia aqui era `expander → st.tabs → painel`, com um segundo
    # expander dentro do painel. Fora a profundidade, `st.tabs` executa o
    # corpo de TODAS as abas: abrir o expander renderizava os cinco painéis
    # inteiros a cada rerun pra mostrar um.
    nomes = [s.name for s in sinais]
    padrao = plano.name if plano and plano.name in nomes else (nomes[0] if nomes else None)
    if padrao is not None:
        escolhida = st.selectbox(
            f"Detalhe da leitura em {TIMEFRAME_LABELS[tf_entrada]}", nomes,
            index=nomes.index(padrao), key=f"detalhe_leitura_{symbol}_{tf_entrada}",
        )
        alvo = next((s for s in sinais if s.name == escolhida), None)
        if alvo is not None:
            render_signal_panel(alvo, symbol, risk_budget, tf_entrada, contexto, mtf, params, perfil)


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

# Se alguma tela pediu pra "pular" pra um ativo, aplica ANTES do selectbox
# nascer. A troca de TELA não acontece mais aqui: quem chama `_ir_para` já
# fez `st.switch_page`, então esta chave carrega só o símbolo.
if st.session_state.get("jump_to_symbol"):
    target = st.session_state.pop("jump_to_symbol")
    if target in st.session_state.watchlist:
        st.session_state.symbol_select = target


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
# Rotas
# ========================================================================
# A tela é escolhida por CAMINHO de URL (`st.navigation` + `st.Page`), não
# por um valor de `session_state` nem por `?mode=`. Isso não é preferência
# estética, é o que faz o botão voltar do navegador funcionar:
#
#   - voltar/avançar ENTRE PÁGINAS funciona: era o bug streamlit#5293, e foi
#     corrigido pelo PR streamlit#6271 — a URL volta e o conteúdo re-renderiza;
#   - voltar/avançar por QUERY PARAM não funciona: streamlit#13963 segue
#     aberto e é específico de apps com `st.navigation` — a URL muda, o rerun
#     acontece, e `st.query_params` ainda devolve o valor velho.
#
# Daí a divisão que o resto deste bloco implementa: **a rota mora no caminho
# e é a única coisa no histórico**; símbolo/perfil/modalidade viajam em query
# param só para links compartilháveis, são LIDOS mas nunca ESCRITOS. Escrever
# a cada mudança encheria o histórico de entradas que o #13963 não consegue
# restaurar — o usuário apertaria voltar e nada mudaria na tela.
#
# As páginas são callables (`st.Page` aceita função), então o app segue num
# arquivo só e o `COPY` do Dockerfile.streamlit não muda.

ROTA_PADRAO = "oportunidades"

# Rótulo e ícone de cada rota. A ordem aqui é a ordem das pills.
ROTAS = {
    "oportunidades": ("🎯", "Oportunidades", "Melhores sinais operáveis agora"),
    "scanner":       ("🔍", "Scanner", "A watchlist inteira, ranqueada"),
    "ativo":         ("📈", "Agora", "Gráfico e as 6 leituras de um ativo"),
    "retroativa":    ("🕵️", "Retroativa", "Como o sinal teria se saído numa data passada"),
    "acompanhar":    ("📋", "Acompanhar", "Triagem dos sinais recentes"),
    "assertividade": ("📉", "Assertividade", "Taxa de acerto medida do histórico"),
}

# Os quatro grupos do topo. Grupo com mais de uma rota ganha sub-nav.
NAV_GRUPOS = {
    "🎯 Oportunidades": ["oportunidades"],
    "🔍 Scanner": ["scanner"],
    "📈 Ativo": ["ativo", "retroativa"],
    "📋 Sinais": ["acompanhar", "assertividade"],
}

# Links já compartilhados usam os nomes de modo antigos (o card de
# oportunidade emitia `?mode=Análise+individual`). Sem esta tradução eles
# cairiam na landing sem aviso, que é pior que um erro.
_ALIAS_MODOS = {
    "Dashboard": ("oportunidades", None),
    "Scanner": ("scanner", None),
    "Análise individual": ("ativo", None),
    "Verificação retroativa": ("retroativa", None),
    "Acompanhamento": ("acompanhar", None),
    "Assertividade": ("assertividade", None),
    "Mini Índice (WINFUT)": ("ativo", WINFUT_SYMBOL),
}

# Preenchida logo antes da sidebar, quando `st.navigation` já resolveu a rota.
ROTA_ATUAL = ROTA_PADRAO


def _url_publica() -> str:
    """Base dos links de compartilhamento. Vem do ambiente porque o domínio
    estava cravado no código, o que fazia o botão gerar link de produção
    mesmo rodando `streamlit run` na máquina de quem estava mexendo."""
    return (os.environ.get("ACOES_PUBLIC_URL") or "https://acoes.dondon.services").rstrip("/")


def _link_da_view(rota: str) -> str:
    """URL desta tela, com o estado que vale a pena carregar junto."""
    extras = {
        chave: st.session_state[skey]
        for chave, skey in (("symbol", "symbol_select"), ("perfil", "perfil_select"),
                            ("modality", "modality_select"))
        if skey in st.session_state
    }
    # A rota padrão é servida na raiz; as outras têm caminho próprio.
    caminho = "/" if rota == ROTA_PADRAO else f"/{rota}"
    query = ("?" + "&".join(f"{k}={v}" for k, v in extras.items())) if extras else ""
    return f"{_url_publica()}{caminho}{query}"


def _aplicar_deep_link() -> None:
    """Copia os query params para o `session_state`, sempre que a URL mudar.

    Roda ANTES de qualquer widget nascer — o Streamlit não deixa mexer numa
    chave depois que o widget dela existe. O guarda é a comparação com o que
    já foi consumido, não um "só na primeira vez": assim colar uma URL nova
    ou voltar para um link compartilhado também é aplicado, e ao mesmo tempo
    os widgets não são forçados de volta ao valor do link a cada rerun."""
    bruto = dict(st.query_params)
    if bruto == st.session_state.get("_deep_link_consumido"):
        return
    st.session_state["_deep_link_consumido"] = bruto

    symbol = bruto.get("symbol")
    if symbol and symbol in st.session_state.get("watchlist", []):
        st.session_state.symbol_select = symbol

    perfil = bruto.get("perfil")
    if perfil and perfil in st.session_state.get("perfis", {}):
        st.session_state.perfil_select = perfil

    modalidade = bruto.get("modality")
    if modalidade and modalidade in MODALITY_CHOICES:
        st.session_state.modality_select = modalidade


def _ir_para(rota: str, symbol: str | None = None) -> None:
    """Pulo entre telas. Usa `st.switch_page`, que a doc descreve como
    equivalente a clicar no menu — ou seja, gera a mesma entrada de
    histórico, e o voltar do navegador desfaz o pulo.

    `symbol` reusa o handshake `jump_to_symbol` já existente: o selectbox
    do ativo mora na sidebar e não pode ser reatribuído depois de criado."""
    if symbol:
        st.session_state.jump_to_symbol = symbol
    st.switch_page(PAGINAS[rota])


def _page_header(titulo: str, fatos: list[tuple[str, str]] | None = None,
                 rota: str | None = None) -> None:
    """Cabeçalho padrão de toda tela: título, os fatos da configuração atual
    como chips, e o compartilhar num popover.

    Substitui quatro coisas que estavam espalhadas: o breadcrumb (repetia a
    pill que o usuário acabou de clicar e nem era clicável), o botão de
    compartilhar de cada view, o if/elif de `st.title` por modo, e as
    captions corridas que concatenavam seis fatos numa frase só."""
    rota = rota or ROTA_ATUAL
    esq, dir_ = st.columns([6, 1])
    with esq:
        st.title(titulo)
    with dir_:
        with st.popover("🔗", use_container_width=True, help="Link para esta tela"):
            st.caption("Copie o link desta tela:")
            st.code(_link_da_view(rota), language=None)

    if fatos:
        _chips(fatos)

def render_acompanhamento() -> None:
    """Triagem dos sinais recentes: o que o worker gravou, e o que você
    decidiu sobre cada um.

    É uma TABELA com barra de ação, não uma pilha de cards. A versão
    anterior desenhava uma linha por sinal com oito widgets cada — cinco
    colunas, três métricas e quatro botões aninhados numa sub-coluna — até
    vinte linhas, o que dava ~160 widgets numa tela só. Ler qual sinal tinha
    o maior score exigia rolar comparando métricas soltas; aqui a tabela
    ordena e a decisão acontece numa barra única sobre a linha escolhida."""
    if not daytrade_smc.ACOES_API_URL:
        st.info(
            "O acompanhamento lê os sinais gravados, e eles moram na API do homelab — "
            "configure `ACOES_API_URL` para usar esta tela. Sem ela não há fallback em "
            "arquivo local de propósito: meia lista de sinais é pior que nenhuma."
        )
        return
    
    # Fetch recent signals (last 24h, operaveis apenas)
    try:
        sinais_raw = daytrade_smc.fetch_signals(
            origem="worker",
            perfil=st.session_state.perfil_select,
            dias=1,
            limite=500,
        )
        sinais = sinais_raw.get("signals", []) if isinstance(sinais_raw, dict) else []
    except Exception as e:
        st.error(f"Erro ao buscar sinais: {e}")
        return
    
    if not sinais:
        st.info("Nenhum sinal recente encontrado.")
        return
    
    # Fetch existing feedbacks
    try:
        fb_data = daytrade_smc.fetch_feedback(dias=7, limite=2000)
        fbs = fb_data.get("feedbacks", [])
        # Map signal_id -> latest feedback acao
        fb_map = {}
        for fb in fbs:
            sid = fb.get("signal_id")
            if sid and sid not in fb_map:
                fb_map[sid] = fb["acao"]
    except Exception:
        fb_map = {}
    
    # Classify signals
    pendentes = []
    acompanhando = []
    operados = []
    ignorados = []
    
    for s in sinais:
        sid = s.get("id")
        fb = fb_map.get(sid)
        if fb == "ACOMPANHAR":
            acompanhando.append(s)
        elif fb in ("OPERAR", "OPEREI"):
            operados.append(s)
        elif fb == "IGNORAR":
            ignorados.append(s)
        else:
            pendentes.append(s)
    
    baldes = {
        "⏳ Pendentes": pendentes,
        "👀 Acompanhando": acompanhando,
        "💰 Operados": operados,
        "🚫 Ignorados": ignorados,
    }

    c1, c2, c3, c4 = st.columns(4)
    for coluna, (rotulo, lista) in zip((c1, c2, c3, c4), baldes.items()):
        coluna.metric(rotulo, len(lista))

    # `segmented_control` e não quatro expanders: um balde de cada vez, e o
    # que está em foco fica com a tabela inteira em vez de espremido dentro
    # de um expander fechado.
    escolha = st.segmented_control(
        "Situação", list(baldes), key="acomp_balde", default="⏳ Pendentes", required=True,
        format_func=lambda r: f"{r} ({len(baldes[r])})",
        label_visibility="collapsed",
    ) or "⏳ Pendentes"

    lista = baldes[escolha]
    if not lista:
        st.info(f"Nada em **{escolha}** nas últimas 24h.")
        return

    tabela = pd.DataFrame([_linha_feedback(s) for s in lista])
    selecao = st.dataframe(
        tabela, hide_index=True, use_container_width=True,
        height=min(520, 45 + 35 * len(tabela)),
        on_select="rerun", selection_mode="single-row", key=f"acomp_tabela_{escolha}",
    )

    linhas = selecao.get("selection", {}).get("rows") or []
    if not linhas:
        st.caption("Selecione uma linha para decidir o que fazer com o sinal.")
        return

    alvo = lista[linhas[0]]
    sid = alvo.get("id")
    st.markdown(f"**{alvo.get('symbol')}** · {alvo.get('timeframe')} · {alvo.get('modalidade', '')}")
    a1, a2, a3, a4 = st.columns(4)
    if a1.button("👀 Acompanhar", key=f"fb_acomp_{sid}", use_container_width=True):
        _do_feedback(sid, "ACOMPANHAR")
    if a2.button("💰 Operei", key=f"fb_oper_{sid}", use_container_width=True):
        _do_feedback(sid, "OPEREI")
    if a3.button("🚫 Ignorar", key=f"fb_ign_{sid}", use_container_width=True):
        _do_feedback(sid, "IGNORAR")
    if a4.button("📊 Ver análise", key=f"fb_ver_{sid}", use_container_width=True,
                 type="primary"):
        _ir_para("ativo", symbol=alvo.get("symbol"))


def _linha_feedback(s: dict) -> dict:
    """Um sinal virado linha de tabela. Construtor PURO — nenhum `st.*` aqui
    dentro, no mesmo molde do `_linha_leitura`. Era o antigo
    `_render_feedback_row`, que misturava a formatação com oito widgets."""
    candle = s.get("candle_time")
    try:
        quando = pd.Timestamp(candle).tz_convert("America/Sao_Paulo").strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        quando = str(candle or "—")
    return {
        "Ativo": s.get("symbol", ""),
        "Direção": s.get("direcao", "NEUTRO"),
        "TF": s.get("timeframe", ""),
        "Leitura": s.get("modalidade", ""),
        "Score": round(s.get("score") or 0, 1),
        "Entrada": s.get("entrada"),
        "Stop": s.get("stop"),
        "Alvo": s.get("alvo_1"),
        "Candle": quando,
        "Setup": (s.get("setup") or "")[:60],
    }


def _do_feedback(signal_id: int, acao: str) -> None:
    try:
        daytrade_smc.save_feedback(signal_id, acao)
        st.success(f"{acao}!")
        st.rerun()
    except Exception as e:
        st.error(f"Erro: {e}")


def render_dashboard(source: str, count: int, risk_budget: float | None, params: AnalysisParams,
                      perfis: list[str], style: str) -> None:
    """Tela principal: top oportunidades de relance, organizadas por perfil.

    Cada card é uma decisão — entrada, stop, alvo, score, perfil. Sem
    parameter tweaking: isso aqui é pra operar, não pra calibrar. Quem quer
    calibrar desce pro Scanner ou pra Análise individual."""
    # `st.segmented_control`, NÃO `st.tabs`. Aqui não é preferência visual:
    # tabs executam o corpo de TODAS as abas a cada rerun, e o corpo de cada
    # aba é uma varredura completa da watchlist. Com quatro perfis isso eram
    # cinco varreduras a cada clique em qualquer widget da página, cinco
    # vezes o custo pra mostrar uma. Com o seletor, roda uma só.
    perfil_filtro = None
    if perfis:
        escolha = st.segmented_control(
            "Perfil", ["Todos"] + perfis, key="dash_perfil", default="Todos", required=True,
            help="Compara a calibragem de cada perfil sobre os mesmos candles.",
        )
        perfil_filtro = None if escolha in (None, "Todos") else escolha

    with st.spinner("Analisando oportunidades..."):
        df = run_scanner(
            st.session_state.watchlist, style, "Confluência",
            source, count, risk_budget,
            params if perfil_filtro is None else st.session_state.perfis.get(perfil_filtro, params),
        )

    # Só o que dá pra operar: sem entrada válida não há decisão a tomar.
    operáveis = df[df["Entrada"].notna()].copy() if "Entrada" in df.columns else df.head(0)
    if operáveis.empty:
        st.info(
            "Nenhum sinal operável neste momento — o motor achou leitura em todos os "
            "ativos, mas nenhuma com entrada, stop e alvo válidos. Tente outro perfil, "
            "ou veja a watchlist inteira no **Scanner**."
        )
        return

    for _, row in operáveis.head(5).iterrows():
        _render_oportunidade_card(row, symbol=row["Ativo"], risk_budget=risk_budget,
                                  perfil=perfil_filtro or st.session_state.perfil_select)


def _render_oportunidade_card(row: pd.Series, symbol: str, risk_budget: float | None,
                              perfil: str) -> None:
    """Um card de oportunidade — o que decide está na cara.

    O card é um `st.container(border=True)`, não HTML. O que existia antes
    era um `<div>` aberto num `st.markdown` e fechado noutro, com os widgets
    no meio: o Streamlit renderiza cada elemento no seu próprio bloco, então
    aquela borda nunca chegou a envolver as métricas — desenhava uma linha
    solta acima e outra abaixo."""
    direcao = row.get("Direção", "NEUTRO")
    comprar = direcao == "COMPRA"
    cor = PALETA["compra"] if comprar else PALETA["venda"]
    acao = "🟢 COMPRAR" if comprar else "🔴 VENDER"

    score = row.get("Score Geral", 0) or 0
    entrada, stop, alvo = row.get("Entrada"), row.get("Stop"), row.get("Alvo 1")
    qty = row.get("Quantidade")
    rr = (alvo - entrada) / (entrada - stop) if entrada and stop and alvo and (entrada - stop) else 0

    if score >= 80:
        qualidade = "🌟 Excepcional"
    elif score >= 60:
        qualidade = "✅ Boa"
    else:
        qualidade = "⚡ Regular"

    with st.container(border=True):
        cab, sc, agir = st.columns([3, 1.2, 1.4], vertical_alignment="center")
        with cab:
            st.markdown(
                f'<span style="color:{cor}; font-weight:700; font-size:13px;">{acao}</span>'
                f'<span style="font-size:22px; font-weight:700; margin-left:8px;">{symbol}</span>',
                unsafe_allow_html=True,
            )
            detalhe = row.get("Setup", "") or ""
            if rr:
                detalhe += f" · R/R 1:{rr:.1f}"
            st.caption(f"{detalhe} · perfil `{perfil}`")
        with sc:
            st.metric("Score", f"{score:.0f}", qualidade)
        with agir:
            if st.button("📊 Ver análise", key=f"dash_ver_{symbol}_{perfil}",
                         use_container_width=True, type="primary"):
                _ir_para("ativo", symbol=symbol)
            with st.popover("🔗 Link", use_container_width=True):
                st.code(f"{_url_publica()}/ativo?symbol={symbol}&perfil={perfil}", language=None)

        if entrada and stop and alvo:
            # "Total (R$)" saiu: era entrada × quantidade, derivável das duas
            # colunas ao lado, e ocupava a quinta coluna do card inteiro.
            cols = st.columns(4)
            cols[0].metric("Entrada", f"R$ {entrada:.2f}")
            cols[1].metric("Stop", f"R$ {stop:.2f}", f"{(stop - entrada) / entrada * 100:+.2f}%",
                           delta_color="inverse")
            cols[2].metric("Alvo", f"R$ {alvo:.2f}", f"{(alvo - entrada) / entrada * 100:+.2f}%")
            if qty and qty > 0:
                cols[3].metric("Qtd", f"{int(qty)}")
            elif risk_budget and abs(entrada - stop) > 0:
                cols[3].metric("Qtd", f"{int(risk_budget // abs(entrada - stop))}")
            else:
                cols[3].metric("Qtd", "—", help="Defina o risco máximo na barra lateral")


# ========================================================================
# Scanner
# ========================================================================
def render_scanner(style: str, modality: str, source: str, count: int,
                   risk_budget: float | None, params: AnalysisParams, perfil: str) -> None:
    """A watchlist inteira, ranqueada.

    O botão de rodar mora AQUI, não na barra lateral: a ação e o resultado
    dela são a mesma tela, e o botão escondido entre as configurações fazia
    o estado vazio ("clique em Rodar scanner") apontar pra fora da vista."""
    barra_esq, barra_dir = st.columns([1, 2])
    with barra_esq:
        rodar = st.button("🔍 Rodar scanner", type="primary", use_container_width=True,
                          key="scanner_rodar")

    if rodar:
        st.session_state.scanner_result = run_scanner(
            st.session_state.watchlist, style, modality, source, count, risk_budget, params,
        )
        st.session_state.scanner_risk_budget = risk_budget

    if "scanner_result" not in st.session_state:
        st.info(
            "Clique em **🔍 Rodar scanner** para analisar os "
            f"{len(st.session_state.watchlist)} ativos da watchlist e ranqueá-los pelo score."
        )
        return

    result_df = st.session_state.scanner_result

    if not st.session_state.get("scanner_risk_budget"):
        st.caption(
            "Defina o **Risco máximo (R$)** na barra lateral e rode de novo pra ver a "
            "quantidade sugerida de ações em cada ativo."
        )

    def _color_direction(val):
        if val == "COMPRA":
            return f"color: {PALETA['compra']}; font-weight: 600"
        if val == "VENDA":
            return f"color: {PALETA['venda']}; font-weight: 600"
        return f"color: {PALETA['neutro']}"

    def _color_exaustao(val):
        if not val:
            return f"color: {PALETA['neutro']}"
        cor = PALETA["compra"] if "↑" in str(val) else PALETA["venda"]
        return f"color: {cor}; font-weight: 600"

    vista = result_df
    if "Exaustão" in result_df.columns:
        n_exaustao = int((result_df["Exaustão"] != "").sum())
        if n_exaustao:
            with barra_dir:
                so_exaustao = st.checkbox(
                    f"🎯 Só os {n_exaustao} com exaustão de IFR", value=False,
                    key="scan_f_exaustao",
                    help="Exaustão simultânea em 2+ timeframes é rara e vale mais "
                         "que score alto isolado.",
                )
            if so_exaustao:
                vista = result_df[result_df["Exaustão"] != ""]

    # `on_select`: clicar na linha abre o ativo. Antes eram dois widgets
    # (um selectbox e um botão) abaixo da tabela pra fazer a mesma coisa,
    # com o nome do ativo tendo que ser reencontrado numa lista.
    selecao = st.dataframe(
        vista.style.map(_color_direction, subset=["Direção"])
                   .map(_color_exaustao, subset=["Exaustão"]),
        hide_index=True, use_container_width=True,
        height=min(520, 45 + 35 * len(vista)),
        on_select="rerun", selection_mode="single-row", key="scanner_tabela",
        column_config={
            "Exaustão": st.column_config.TextColumn(
                "Exaustão IFR", width="small",
                help="Timeframes em exaustão simultânea na mesma direção",
            ),
        },
    )
    st.caption("Clique numa linha para abrir o gráfico e as 6 leituras do ativo.")

    linhas = selecao.get("selection", {}).get("rows") or []
    if linhas:
        _ir_para("ativo", symbol=vista.iloc[linhas[0]]["Ativo"])


# ========================================================================
# Páginas
# ========================================================================
# `st.Page` só aceita callable SEM argumentos ("The callable can't accept
# arguments"), então o que a sidebar apura (fonte, estilo, modalidade,
# perfil, params...) chega aqui por este dicionário em vez de por parâmetro.
# As funções `render_*` mantêm as assinaturas antigas — estas páginas são
# casquinhas que montam o cabeçalho e chamam elas.
CTX: dict = {}


def _rotulo_fonte(source: str) -> str:
    return "Homelab · tempo real" if source == "Homelab (API)" else "Yahoo · ~20min"


def _estilo_do_ativo(symbol: str) -> str:
    """O Mini Índice deixou de ser um modo à parte e virou um símbolo como
    outro qualquer — mas ele opera em M5+M15 com contexto M2/H1, não no
    estilo escolhido na sidebar. Resolver por aqui é o que permite a
    unificação: `estilo()` já conhece os dois conjuntos (`_ESTILOS_TODOS`)."""
    return WINFUT_STYLE if symbol == WINFUT_SYMBOL else CTX["style"]


def _ativo_disponivel(symbol: str) -> bool:
    """Guardas do WINFUT. Ele NÃO existe no Yahoo: o nome atravessa
    `yahoo_symbol` intacto e volta "símbolo não encontrado", o que pareceria
    bug da ferramenta. Avisa antes de tentar, dizendo o que fazer."""
    if symbol != WINFUT_SYMBOL:
        return True
    if CTX["source"] != "Homelab (API)":
        st.warning(
            "O Mini Índice só existe pela **Homelab (API)** — ele vem do MetaTrader 5 "
            "pelo scraper, e o Yahoo Finance não tem esse contrato. Troque a fonte em "
            "*Configuração › Dados*, na barra lateral.",
            icon="🏠",
        )
        return False
    if symbol not in st.session_state.watchlist:
        st.warning(
            f"**{symbol}** não está na watchlist, então o scraper não está coletando as "
            "velas dele. Adicione em *Configuração › Watchlist* e confira na VM se "
            "`SCRAPER_SYMBOL_MT5` aponta pro nome do contrato no seu MT5 (contínuo "
            "`WIN$` ou o vencimento vigente).",
            icon="📋",
        )
        return False
    st.info(
        "**Contrato futuro, não é ação.** Alavancagem e horário de negociação são "
        "diferentes, e o contrato vira de vencimento periodicamente — o histórico é "
        "contínuo aqui porque o scraper traduz o nome, não porque o papel é o mesmo.",
        icon="⚠️",
    )
    return True


def _pagina_oportunidades() -> None:
    _page_header(
        "Oportunidades agora",
        [("Estilo", CTX["style"]), ("Perfil", CTX["perfil"]),
         ("Watchlist", f"{len(st.session_state.watchlist)} ativos"),
         ("Fonte", _rotulo_fonte(CTX["source"]))],
    )
    render_dashboard(
        CTX["source"], CTX["count"], CTX["risk_budget"], CTX["params"],
        # top 4 perfis customizados — o "padrão" já é a opção "Todos"
        perfis=[p for p in sorted(st.session_state.perfis) if p != DEFAULT_PROFILE_NAME][:4],
        style=CTX["style"],
    )


def _pagina_scanner() -> None:
    _page_header(
        "Scanner de mercado",
        [("Watchlist", f"{len(st.session_state.watchlist)} ativos"),
         ("Estilo", CTX["style"]), ("Leitura", CTX["modality"]),
         ("Perfil", CTX["perfil"]), ("Fonte", _rotulo_fonte(CTX["source"]))],
    )
    render_scanner(CTX["style"], CTX["modality"], CTX["source"], CTX["count"],
                   CTX["risk_budget"], CTX["params"], CTX["perfil"])


def _pagina_ativo() -> None:
    symbol = st.session_state.symbol_select
    style = _estilo_do_ativo(symbol)
    _page_header(
        symbol,
        [("Estilo", style), ("Leitura", CTX["modality"]), ("Perfil", CTX["perfil"]),
         ("Fonte", _rotulo_fonte(CTX["source"]))],
    )

    # Auto-atualização mora aqui, não na sidebar: é uma ação SOBRE esta tela.
    with st.popover("🔄 Atualização"):
        auto_refresh = st.checkbox("Atualizar automaticamente", key="auto_refresh_on")
        intervalo = st.select_slider(
            "Intervalo", options=[30, 60, 120, 300], value=60,
            format_func=lambda s: f"{s}s", disabled=not auto_refresh,
            key="auto_refresh_intervalo",
        )

    if not _ativo_disponivel(symbol):
        return

    args = (symbol, style, CTX["modality"], CTX["source"], CTX["count"],
            CTX["risk_budget"], params_para_estilo(CTX["params"], style), CTX["perfil"])
    if auto_refresh:
        st.caption(f"🔄 Atualizando sozinho a cada {intervalo}s")
        _AUTO_REFRESH_FRAGMENTS[intervalo](*args)
    else:
        render_individual_analysis(*args)


def _pagina_retroativa() -> None:
    symbol = st.session_state.symbol_select
    style = _estilo_do_ativo(symbol)
    _page_header(
        "Verificação retroativa",
        [("Ativo", symbol), ("Estilo", style), ("Leitura", CTX["modality"]),
         ("Perfil", CTX["perfil"])],
    )
    if not _ativo_disponivel(symbol):
        return
    render_retro_check(symbol, style, CTX["modality"], CTX["source"],
                       CTX["count"], params_para_estilo(CTX["params"], style))


def _pagina_acompanhar() -> None:
    _page_header(
        "Acompanhamento de sinais",
        [("Perfil", CTX["perfil"]), ("Janela", "últimas 24h")],
    )
    render_acompanhamento()


def _pagina_assertividade() -> None:
    _page_header("Assertividade medida")
    render_assertividade(sorted(st.session_state.perfis))


_FUNCOES_DE_PAGINA = {
    "oportunidades": _pagina_oportunidades,
    "scanner": _pagina_scanner,
    "ativo": _pagina_ativo,
    "retroativa": _pagina_retroativa,
    "acompanhar": _pagina_acompanhar,
    "assertividade": _pagina_assertividade,
}

# `default=True` faz a rota ser servida na raiz e IGNORA `url_path` (doc do
# st.Page), por isso a landing não declara caminho.
PAGINAS = {
    rota: st.Page(
        funcao, title=ROTAS[rota][1], icon=ROTAS[rota][0],
        **({"default": True} if rota == ROTA_PADRAO else {"url_path": rota}),
    )
    for rota, funcao in _FUNCOES_DE_PAGINA.items()
}

# `position="hidden"`: as pills de dois níveis lá embaixo são a navegação. O
# menu nativo mostraria as seis rotas achatadas numa lista, que é exatamente
# a falta de hierarquia que este redesenho existe pra resolver.
_pg = st.navigation(list(PAGINAS.values()), position="hidden")
ROTA_ATUAL = _pg.url_path or ROTA_PADRAO


def _redirecionar_link_antigo() -> None:
    """Links já compartilhados apontam para `?mode=<nome do modo antigo>`.
    Traduz para a rota nova e redireciona; sem isto eles cairiam na landing
    sem nenhum aviso, que é pior que um erro."""
    antigo = st.query_params.get("mode")
    if not antigo:
        return
    destino, symbol = _ALIAS_MODOS.get(antigo, (None, None))
    # Some com o `mode` primeiro, senão o redirect se repete a cada rerun.
    del st.query_params["mode"]
    if destino is None:
        return
    if symbol:
        st.session_state.symbol_select = symbol
    st.switch_page(PAGINAS[destino])


_redirecionar_link_antigo()
_aplicar_deep_link()


# ========================================================================
# Sidebar
# ========================================================================
# Dividida em duas metades por um `st.divider()`: em cima o que se mexe todo
# dia (estilo, leitura, risco, ativo, perfil); embaixo, no bloco
# "Configuração", o que se ajusta uma vez e não se olha mais (fonte da
# informação, watchlist, os 39 parâmetros do motor). Antes disso eram dez
# seções na mesma pilha, e as três decisões que importam ficavam soterradas.
#
# Os expanders de configuração são IRMÃOS, não aninhados: o Streamlit não
# permite expander dentro de expander.
with st.sidebar:
    st.markdown("## 📊 Day Trade SMC")

    # O frescor do dado sobe pro topo: é o único fato da fonte que muda de
    # minuto a minuto, e é o que decide se dá pra confiar na tela agora.
    # `last_candle_time`, não `last_ingested_at` — este último congela com o
    # mercado fechado e pareceria que a coleta caiu.
    if st.session_state.source_select != "Homelab (API)":
        st.caption("⏱️ Yahoo Finance · atraso de ~15-20min")
    elif not daytrade_smc.ACOES_API_URL:
        st.caption("⚠️ `ACOES_API_URL` não configurada — use o Yahoo por enquanto")
    else:
        _ultima = _ultima_vela()
        if _ultima is None:
            st.caption("🏠 Homelab · não consegui ler o `/status` agora")
        else:
            st.caption(
                "🏠 Última vela "
                f"{_ultima.tz_convert('America/Sao_Paulo').strftime('%d/%m %H:%M')}"
                " · congela com o mercado fechado"
            )

    st.divider()

    style = st.segmented_control(
        "Estilo", list(STYLES.keys()), key="style_select", default="Day Trade",
        required=True,
        help="Day Trade confirma em M15+H1 (posições no mesmo dia). "
             "Swing Trade confirma em Diário+Semanal (posições de dias a semanas), "
             "com H4 como contexto de timing de entrada.",
    )
    conf_a, conf_b = estilo(style)["confirmation"]

    modality = st.selectbox(
        "Leitura", MODALITY_CHOICES, key="modality_select",
        help="Confluência combina as 4 categorias estruturais (SMC, Price Action, Médias Móveis, "
             "VWAP). SMC/Price Action/Médias Móveis/VWAP/IFR usam só a leitura isolada daquela "
             "categoria. O IFR é leitura de EXAUSTÃO, contrária por natureza: só aponta direção "
             "em ≤10 ou ≥90, fica NEUTRO quase sempre (de propósito) e por isso NÃO entra nem na "
             "Confluência nem no Score Geral. \"Todas as modalidades\" calcula um SCORE GERAL "
             "(média das 5 leituras agregáveis) e usa ele — não uma única leitura — pra decidir "
             "a confirmação e ordenar o Scanner.",
    )

    # Risco subiu da antiga seção "Parâmetros": é ele que transforma um sinal
    # em quantidade de ações, então aparece em todo card do Oportunidades.
    risk_budget = st.number_input(
        "Risco máximo (R$)", min_value=0.0, value=0.0, step=50.0, key="risk_budget_input",
        help="Quanto você aceita perder se o stop for acionado. Zero = não calcular quantidade.",
    )
    risk_budget = risk_budget if risk_budget > 0 else None

    # O seletor de ativo só existe nas rotas que leem `symbol_select`. Antes
    # ele aparecia também no Dashboard, que nunca olhou pra essa chave.
    if ROTA_ATUAL in ("ativo", "retroativa"):
        st.selectbox("Ativo", st.session_state.watchlist, key="symbol_select")

    perfil = st.selectbox(
        "Perfil", sorted(st.session_state.perfis), key="perfil_select",
        help="Um perfil é um conjunto nomeado de parâmetros do motor. Cada sinal salvo "
             "guarda o perfil que o gerou, então dá pra comparar a assertividade de uma "
             "calibragem contra a outra na tela Assertividade.",
    )
    # O estilo pode trocar os limiares do IFR (Swing opera 20/80, Day
    # Trade 10/90) quando o perfil não escolheu os seus. A comparação
    # do "alterado, não salvo" aplica o MESMO ajuste no perfil salvo,
    # senão todo perfil apareceria como alterado em Swing sem ninguém
    # ter mexido em nada.
    params = params_para_estilo(_params_da_sessao(), style)
    _salvo = st.session_state.perfis.get(perfil)
    _alterado = _salvo is None or params != params_para_estilo(_salvo, style)
    st.caption(f"hash `{params.params_hash()[:8]}`"
               + (" · **alterado, não salvo**" if _alterado else ""))

    if params.normalizacao_score != params.filtro_isolada_score_max:
        st.warning(
            "O divisor de normalização e o teto de score da leitura isolada estão "
            "diferentes. Os dois são acoplados por construção: uma leitura isolada é "
            "capada pelo teto, e a confluência divide por esse mesmo número pra ela "
            "normalizar em 1.0. Separados, a escala da confluência sai do lugar."
        )

    st.divider()
    st.caption("**Configuração** — ajusta uma vez e esquece")

    with st.expander("⚙️ Dados", expanded=False):
        source = st.radio(
            "Fonte", DATA_SOURCES, key="source_select",
            help="\"Homelab (API)\" lê candles pela API do homelab, alimentada por um scraper "
                 "MT5 que roda continuamente numa VM — dado real, poucos segundos de atraso. "
                 "É o caminho recomendado. \"Yahoo Finance\" funciona em qualquer lugar, sem "
                 "depender de nada seu estar no ar, mas o dado nasce ~15-20min atrasado.",
        )
        count = st.slider(estilo(style)["count_label"], min_value=50, max_value=400,
                          value=250, step=10, key="count_slider")
        st.caption(
            f"A recomendação exige **{TIMEFRAME_LABELS[conf_a]}** e "
            f"**{TIMEFRAME_LABELS[conf_b]}** concordando. "
            f"{', '.join(TIMEFRAME_LABELS[tf] for tf in estilo(style)['context'])} "
            "entra como contexto."
        )

    with st.expander("📋 Watchlist", expanded=False):
        st.caption(f"{len(st.session_state.watchlist)} ativo(s) monitorado(s)")
        new_symbol = st.text_input("Adicionar ativo (ex: VALE3)", key="new_symbol_input")
        if st.button("Adicionar", use_container_width=True) and new_symbol.strip():
            value = new_symbol.strip().upper().replace(" ", "")
            if value not in st.session_state.watchlist:
                st.session_state.watchlist.append(value)
                _persist_watchlist()
            st.rerun()

        remove_symbol = st.selectbox("Remover ativo", ["—"] + st.session_state.watchlist,
                                     key="remove_symbol_select")
        if st.button("Remover", use_container_width=True) and remove_symbol != "—":
            st.session_state.watchlist = [s for s in st.session_state.watchlist
                                          if s != remove_symbol]
            _persist_watchlist()
            st.rerun()

        if st.button("Restaurar lista padrão", use_container_width=True):
            st.session_state.watchlist = DEFAULT_SYMBOLS.copy()
            _persist_watchlist()
            st.rerun()

    with st.expander("🎛️ Parâmetros do motor", expanded=False):
        if (params.rsi_sobrevenda, params.rsi_sobrecompra) != (
            _params_da_sessao().rsi_sobrevenda, _params_da_sessao().rsi_sobrecompra
        ):
            st.caption(
                "IFR ajustado pro estilo: exaustão em "
                f"{params.rsi_sobrevenda:.0f}/{params.rsi_sobrecompra:.0f}. "
                "Salve o perfil com outro par pra fixar."
            )
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
                        st.number_input(rotulo, step=passo, key=f"param_{campo}_{i}",
                                        format=formato)

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
            if st.button("Remover perfil", use_container_width=True,
                         disabled=perfil == DEFAULT_PROFILE_NAME):
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


CTX.update(
    source=source, style=style, modality=modality, count=count,
    risk_budget=risk_budget, params=params, perfil=perfil,
)


# ========================================================================
# Corpo principal
# ========================================================================
def _render_nav(rota: str) -> None:
    """As pills de dois níveis. Elas REFLETEM a URL — quem manda é a rota,
    não o session_state.

    A ressincronização só acontece quando a URL mudou por fora (voltar,
    avançar, F5, link colado). Reatribuir a chave em todo rerun apagaria o
    clique do usuário antes de conseguirmos lê-lo: no rerun do clique a URL
    ainda é a antiga, e é o `st.switch_page` que vai trocá-la."""
    grupo_atual = next(g for g, rotas in NAV_GRUPOS.items() if rota in rotas)

    if st.session_state.get("_nav_url_vista") != rota:
        st.session_state["_nav_url_vista"] = rota
        st.session_state["nav_grupo"] = grupo_atual
        st.session_state["nav_sub"] = rota

    # Sem `default=`: o bloco acima garante que a chave já existe no
    # session_state antes do widget nascer, e passar os dois faz o Streamlit
    # avisar "created with a default value but also had its value set via the
    # Session State API" em todo rerun.
    #
    # `required=True` não é estético: sem ele dá pra clicar no pill já
    # selecionado pra DESMARCAR, e a navegação devolveria None.
    escolha = st.segmented_control(
        "Navegação", list(NAV_GRUPOS), key="nav_grupo",
        required=True, label_visibility="collapsed",
    )
    if escolha and escolha != grupo_atual:
        _ir_para(NAV_GRUPOS[escolha][0])

    irmas = NAV_GRUPOS[grupo_atual]
    if len(irmas) > 1:
        sub = st.segmented_control(
            "Seção", irmas, key="nav_sub", required=True,
            label_visibility="collapsed",
            format_func=lambda r: f"{ROTAS[r][0]} {ROTAS[r][1]}",
        )
        if sub and sub != rota:
            _ir_para(sub)


_render_nav(ROTA_ATUAL)
_pg.run()
