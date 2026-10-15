"""
executor/notificador.py

Notificações Telegram padronizadas para o robô multioperacional.

Usa o mesmo bot e formato dos outros setups. Publica via NATS no subject
`agents.notify.telegram.acoes`.

Tipos de mensagem:
- Sinal gerado
- Ordem enviada/executada/cancelada/rejeitada
- Saída (ALVO/STOP/ZERO_A_ZERO/STOP_MOVEL/TEMPO/ZERAGEM)
- Sinal bloqueado (motivo)
- Limites (50% e 100% da perda diária)
- Rebaixamento automático
- Kill switch
- Erros
- Resumo do pregão
- Heartbeat (30 min)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("executor.notificador")

TZ_MERCADO = ZoneInfo("America/Sao_Paulo")

# Cache do bus NATS
_bus = None


async def _get_bus():
    """Obtém ou cria conexão com NATS."""
    global _bus
    if _bus is None:
        from agents_runtime.bus import Bus
        nats_url = os.environ.get("NATS_URL", "nats://nats.agents.svc.cluster.local:4222")
        _bus = Bus(nats_url)
    return _bus


async def _publicar(texto: str) -> None:
    """Publica mensagem no Telegram via NATS."""
    try:
        bus = await _get_bus()
        await bus.publish(
            "agents.notify.telegram.acoes",
            texto.encode("utf-8"),
        )
        log.debug("Notificação publicada: %s", texto[:80])
    except Exception as exc:  # noqa: BLE001
        log.error("Falha ao publicar notificação: %s", exc)


def _reais(valor: float) -> str:
    """Formata valor em reais: R$ 1.234,56"""
    if valor is None:
        return "—"
    s = f"{abs(valor):,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{'-' if valor < 0 else ''}R$ {s}"


def _sinal_vantajoso(score: float, rr: float) -> str:
    """Indicador de operação vantajosa (mesmo padrão dos outros setups)."""
    if score >= 85 and rr >= 1.5:
        return "🌟 EXCEPCIONAL"
    elif score >= 75 and rr >= 1.2:
        return "✅ BOA"
    elif score >= 65:
        return "⚠️ REGULAR"
    return "❌ FRACA"


async def notificar_sinal(
    *,
    setup: str,
    livro: str,
    ativo: str,
    lado: str,  # COMPRA | VENDA
    preco: float,
    stop: float,
    alvo: float,
    rr: float,
    score: float,
    modo: str,
) -> None:
    """Notifica sinal gerado."""
    vantajoso = _sinal_vantajoso(score, rr)
    texto = (
        f"📡 **SINAL** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"📈 {ativo} **{lado}**\n"
        f"💰 Preço: {preco:.2f}\n"
        f"🛑 Stop: {stop:.2f}\n"
        f"🎯 Alvo: {alvo:.2f}\n"
        f"📊 R/R: 1:{rr:.2f} | Score: {score:.0f}\n"
        f"⚡ {vantajoso}\n"
        f"📋 Modo: {modo}"
    )
    await _publicar(texto)


async def notificar_ordem_enviada(
    *,
    setup: str,
    livro: str,
    ativo: str,
    lado: str,
    preco_pedido: float,
    stop: float,
    alvo: float,
    volume: float,
    ticket: int,
) -> None:
    """Notifica ordem enviada."""
    texto = (
        f"📤 **ORDEM ENVIADA** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"📈 {ativo} **{lado}**\n"
        f"💰 Preço pedido: {preco_pedido:.2f}\n"
        f"📊 Volume: {volume:.0f}\n"
        f"🛑 Stop: {stop:.2f}\n"
        f"🎯 Alvo: {alvo:.2f}\n"
        f"🎫 Ticket: {ticket}"
    )
    await _publicar(texto)


async def notificar_ordem_executada(
    *,
    setup: str,
    livro: str,
    ativo: str,
    lado: str,
    preco_executado: float,
    preco_pedido: float,
    volume: float,
    ticket: int,
) -> None:
    """Notifica ordem executada com deslizamento."""
    deslizamento = preco_executado - preco_pedido
    deslizamento_pct = (deslizamento / preco_pedido * 100) if preco_pedido else 0
    texto = (
        f"✅ **ORDEM EXECUTADA** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"📈 {ativo} **{lado}**\n"
        f"💰 Preço executado: {preco_executado:.2f}\n"
        f"📊 Volume: {volume:.0f}\n"
        f"📉 Deslizamento: {deslizamento:+.2f} ({deslizamento_pct:+.2f}%)\n"
        f"🎫 Ticket: {ticket}"
    )
    await _publicar(texto)


async def notificar_saida(
    *,
    setup: str,
    livro: str,
    ativo: str,
    lado: str,
    motivo: str,  # ALVO | STOP | ZERO_A_ZERO | STOP_MOVEL | TEMPO | ZERAGEM
    preco_saida: float,
    resultado_r: float,
    resultado_brl: float,
) -> None:
    """Notifica saída de posição."""
    emojis = {
        "ALVO": "🎯",
        "STOP": "🛑",
        "ZERO_A_ZERO": "➖",
        "STOP_MOVEL": "🔄",
        "TEMPO": "⏰",
        "ZERAGEM": "🔚",
    }
    emoji = emojis.get(motivo, "📊")
    resultado_emoji = "✅" if resultado_r > 0 else "❌"
    texto = (
        f"{emoji} **SAÍDA** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"📈 {ativo} **{lado}**\n"
        f"📊 Motivo: {motivo}\n"
        f"💰 Preço saída: {preco_saida:.2f}\n"
        f"{resultado_emoji} Resultado: {_reais(resultado_brl)} ({resultado_r:+.2f}R)"
    )
    await _publicar(texto)


async def notificar_sinal_bloqueado(
    *,
    setup: str,
    livro: str,
    ativo: str,
    motivo: str,
    detalhe: str = "",
) -> None:
    """Notifica sinal bloqueado."""
    textos_motivo = {
        "ATIVO_POSICIONADO": "Ativo já posicionado",
        "MAX_POSICOES": "Limite de posições atingido",
        "MAX_TRADES_DIA": "Limite de trades do dia atingido",
        "LIMITE_PERDA_DIARIA": "Limite de perda diária atingido",
        "RR_INSUFICIENTE": "R/R insuficiente",
        "STOP_ACIMA_DO_MAXIMO": "Stop acima do máximo",
        "SEM_RECUO": "Sem recuo para entrada",
        "IBOV": "Filtro IBOV",
        "HORARIO": "Fora do horário",
    }
    texto_motivo = textos_motivo.get(motivo, motivo)
    texto = (
        f"🚫 **SINAL BLOQUEADO** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"📈 {ativo}\n"
        f"❌ Motivo: {texto_motivo}"
    )
    if detalhe:
        texto += f"\n📝 {detalhe}"
    await _publicar(texto)


async def notificar_limite_perda(
    *,
    carteira: str,
    perda_atual_r: float,
    perda_max_r: float,
    percentual: int,  # 50 ou 100
) -> None:
    """Notifica atingimento de limite de perda diária."""
    emoji = "⚠️" if percentual == 50 else "🚨"
    texto = (
        f"{emoji} **LIMITE DE PERDA {percentual}%**\n"
        f"📂 Carteira: {carteira}\n"
        f"📉 Perda acumulada: {perda_atual_r:.1f}R / {perda_max_r:.1f}R\n"
    )
    if percentual == 100:
        texto += "\n⛔ Novas entradas bloqueadas nesta carteira."
    await _publicar(texto)


async def notificar_rebaixamento(
    *,
    setup: str,
    livro: str,
    motivo: str,
) -> None:
    """Notifica rebaixamento automático para PAPER."""
    texto = (
        f"📉 **REBAIXADO PARA PAPER** {setup}\n"
        f"📂 Livro: {livro}\n"
        f"❌ Motivo: {motivo}\n"
        f"ℹ️ Volta para LIVE é manual."
    )
    await _publicar(texto)


async def notificar_kill_switch(
    *,
    carteira: str,
    acao: str,  # ATIVADO | DESATIVADO
    simbolos: list[str] | None = None,
) -> None:
    """Notifica kill switch."""
    if acao == "ATIVADO":
        texto = (
            f"🛑 **KILL SWITCH ATIVADO**\n"
            f"📂 Carteira: {carteira}\n"
        )
        if simbolos:
            texto += f"📊 Posições para fechar: {', '.join(simbolos)}\n"
        texto += "\n⛔ Novas entradas bloqueadas. Use /retomar para liberar."
    else:
        texto = (
            f"✅ **KILL SWITCH DESATIVADO**\n"
            f"📂 Carteira: {carteira}\n"
            f"ℹ️ Novas entradas liberadas."
        )
    await _publicar(texto)


async def notificar_erro(mensagem: str) -> None:
    """Notifica erro do sistema."""
    texto = f"🚨 **ERRO DO ROBÔ**\n{mensagem}"
    await _publicar(texto)


async def notificar_status(
    *,
    contas: list[dict],
    carteiras: list[dict],
    limites: list[dict],
) -> None:
    """Notifica status do robô (após /status ou na abertura)."""
    texto = "📊 **STATUS DO ROBÔ**\n\n"

    # Contas
    texto += "🔑 **Contas:**\n"
    for c in contas:
        tipo = "SIMULADA" if c.get("demo") else "REAL"
        texto += f"  • {c.get('login')} ({c.get('servidor')}) — {tipo}\n"

    texto += "\n"

    # Carteiras
    for carteira in carteiras:
        nome = carteira.get("carteira", "?")
        posicoes = carteira.get("posicoes_abertas", 0)
        trades = carteira.get("trades_hoje", 0)
        max_trades = carteira.get("max_trades", 20)
        perda = carteira.get("perda_acumulada_r", 0)
        perda_max = carteira.get("perda_max_r", 4)
        resultado = carteira.get("resultado_dia_r", 0)
        kill = carteira.get("kill_switch", False)

        texto += f"📂 **{nome}**\n"
        texto += f"  Posições: {posicoes}\n"
        texto += f"  Trades hoje: {trades}/{max_trades}\n"
        texto += f"  Perda: {perda:.1f}R / {perda_max:.1f}R\n"
        texto += f"  Resultado dia: {_reais(resultado)}\n"
        if kill:
            texto += "  🛑 KILL SWITCH ATIVO\n"
        texto += "\n"

    # Limites
    texto += "⚙️ **Limites:**\n"
    for lim in limites:
        texto += f"  • {lim.get('setup')}: risco R$ {lim.get('risco_r', 150):.0f}, MTF: {'sim' if lim.get('exigir_mtf') else 'não'}\n"

    await _publicar(texto)


async def notificar_resumo_pregao(
    *,
    data: str,
    por_carteira: list[dict],
    por_setup: list[dict],
) -> None:
    """Notifica resumo do fim do pregão."""
    texto = f"📋 **RESUMO DO PREGÃO** {data}\n\n"

    # Por carteira
    texto += "📂 **Por Carteira:**\n"
    for c in por_carteira:
        nome = c.get("carteira", "?")
        ops = c.get("operacoes", 0)
        acerto = c.get("acerto_pct", 0)
        equilibrio = c.get("acerto_equilibrio_pct", 0)
        expectativa = c.get("expectativa_r", 0)
        resultado = c.get("resultado_brl", 0)
        maior_perda = c.get("maior_perda_r", 0)
        drawdown = c.get("drawdown_r", 0)

        texto += f"  **{nome}:**\n"
        texto += f"    Operações: {ops} | Acerto: {acerto:.0f}%\n"
        texto += f"    Equilíbrio: {equilibrio:.0f}% | Expectativa: {expectativa:+.2f}R\n"
        texto += f"    Resultado: {_reais(resultado)}\n"
        texto += f"    Maior perda: {maior_perda:.1f}R | Drawdown: {drawdown:.1f}R\n\n"

    # Por setup
    texto += "⚙️ **Por Setup:**\n"
    for s in por_setup:
        nome = s.get("setup", "?")
        ops = s.get("operacoes", 0)
        acerto = s.get("acerto_pct", 0)
        resultado = s.get("resultado_brl", 0)
        texto += f"  {nome}: {ops} ops, {acerto:.0f}% acerto, {_reais(resultado)}\n"

    await _publicar(texto)


async def notificar_heartbeat(
    *,
    posicoes_abertas: int,
    carteiras: list[dict],
) -> None:
    """Heartbeat a cada 30 minutos durante o pregão."""
    texto = "💓 **ROBÔ ATIVO**\n"
    texto += f"📊 Posições abertas: {posicoes_abertas}\n"
    for c in carteiras:
        nome = c.get("carteira", "?")
        resultado = c.get("resultado_dia_r", 0)
        texto += f"  {nome}: {_reais(resultado)}\n"
    await _publicar(texto)


async def fechar_bus() -> None:
    """Fecha a conexão NATS."""
    global _bus
    if _bus is not None:
        try:
            await _bus.nc.close()
        except Exception:
            pass
        _bus = None
