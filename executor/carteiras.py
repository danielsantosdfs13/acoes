"""
executor/carteiras.py

Gestão de duas carteiras independentes: SOMADOS e RENKO.

Regras:
- SOMADOS (VWAP, SMC, MEDIAS, CONFLUENCIA): disputam vaga por ativo
- RENKO (RENKO_TOURO_URSO): carteira à parte, NÃO disputa vaga com SOMADOS
- Limites próprios por carteira: max_posicoes, max_trades_dia, perda_diaria_r
- Todas as regras globais (horário, zeragem, kill switch) valem para as duas

Registrar em toda operação o campo `livro` = SOMADOS | RENKO.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("executor.carteiras")

TZ_MERCADO = ZoneInfo("America/Sao_Paulo")

# Caminho do arquivo de configuração
CONFIG_PATH = Path(os.environ.get(
    "EXECUTOR_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "config" / "setups.json")
))

# Cache da configuração
_config_cache: dict | None = None
_config_mtime: float | None = None
_data_pregao: str | None = None  # Data do último carregamento


@dataclass
class LimiteCarteira:
    """Limites de uma carteira."""
    max_posicoes: int = 5
    max_trades_dia: int = 20
    perda_diaria_r: float = 4.0


@dataclass
class EstadoCarteira:
    """Estado atual de uma carteira durante o pregão."""
    posicoes_abertas: dict[str, dict] = field(default_factory=dict)  # symbol -> info
    trades_hoje: int = 0
    perda_acumulada_r: float = 0.0
    resultado_dia_r: float = 0.0
    ultimo_heartbeat: datetime | None = None
    kill_switch: bool = False  # /parar ativo


# Estado das carteiras (em memória, reinicia com o processo)
_estados: dict[str, EstadoCarteira] = {
    "SOMADOS": EstadoCarteira(),
    "RENKO": EstadoCarteira(),
}

# Mapeamento setup -> carteira
SETUP_CARTEIRA: dict[str, str] = {
    "VWAP": "SOMADOS",
    "SMC": "SOMADOS",
    "Médias Móveis": "SOMADOS",
    "MEDIAS": "SOMADOS",
    "Confluência": "SOMADOS",
    "CONFLUENCIA": "SOMADOS",
    "RENKO_TOURO_URSO": "RENKO",
    "CHATGPT": "SOMADOS",  # desabilitado
    "Price Action": "SOMADOS",  # desabilitado
    "PRICE_ACTION": "SOMADOS",  # desabilitado
    "TPO/VWAP": "SOMADOS",  # desabilitado
    "TPO_VWAP": "SOMADOS",  # desabilitado
}


def carteira_do_setup(setup: str) -> str:
    """Retorna a carteira de um setup."""
    return SETUP_CARTEIRA.get(setup, "SOMADOS")


def carregar_config(forcar: bool = False) -> dict | None:
    """Carrega config/setups.json.

    Recarrega automaticamente quando:
    - `forcar=True`
    - O arquivo mudou (mtime)
    - Mudou a data do pregão (recarrega na abertura)
    """
    global _config_cache, _config_mtime, _data_pregao

    try:
        if not CONFIG_PATH.exists():
            log.warning("config/setups.json não encontrado: %s", CONFIG_PATH)
            return _config_cache  # retorna última válida

        mtime = CONFIG_PATH.stat().st_mtime
        hoje = datetime.now(TZ_MERCADO).date().isoformat()

        # Verifica se precisa recarregar
        precisa_recarregar = (
            forcar
            or _config_cache is None
            or mtime != _config_mtime
            or hoje != _data_pregao  # novo pregão
        )

        if not precisa_recarregar:
            return _config_cache

        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)

        # Validação básica
        if "setups" not in data or "carteiras" not in data:
            raise ValueError("JSON inválido: falta 'setups' ou 'carteiras'")

        _config_cache = data
        _config_mtime = mtime
        _data_pregao = hoje
        log.info("config/setups.json carregado (v%s, %d setups)",
                 data.get("versao", "?"), len(data.get("setups", {})))
        return data

    except json.JSONDecodeError as exc:
        log.error("config/setups.json inválido: %s — mantendo última config", exc)
        _notificar_erro(f"JSON inválido: {exc}")
        return _config_cache
    except Exception as exc:  # noqa: BLE001
        log.error("falha ao carregar config: %s", exc)
        _notificar_erro(f"Erro ao carregar config: {exc}")
        return _config_cache


def _notificar_erro(mensagem: str) -> None:
    """Notifica erro no Telegram (via NATS)."""
    try:
        import asyncio
        from agents_runtime.bus import Bus
        nats_url = os.environ.get("NATS_URL", "nats://nats.agents.svc.cluster.local:4222")
        bus = Bus(nats_url)
        # Publicação assíncrona simplificada
        asyncio.get_event_loop().run_until_complete(
            bus.publish("agents.notify.telegram.acoes", f"⚠️ ERRO CONFIG: {mensagem}".encode())
        )
    except Exception:
        pass  # não pode derrubar o executor


def obter_setup(setup: str) -> dict | None:
    """Retorna a configuração de um setup específico."""
    config = carregar_config()
    if config is None:
        return None
    return config.get("setups", {}).get(setup)


def setup_habilitado(setup: str) -> bool:
    """Verifica se um setup está habilitado."""
    cfg = obter_setup(setup)
    if cfg is None:
        return False
    return cfg.get("enabled", False)


def modo_do_setup(setup: str) -> str:
    """Retorna o modo (LIVE/PAPER/SHADOW) de um setup."""
    cfg = obter_setup(setup)
    if cfg is None:
        return "PAPER"
    return cfg.get("modo", "PAPER")


def limites_da_carteira(carteira: str) -> LimiteCarteira:
    """Retorna os limites de uma carteira."""
    config = carregar_config()
    if config is None:
        return LimiteCarteira()
    dados = config.get("carteiras", {}).get(carteira, {})
    return LimiteCarteira(
        max_posicoes=dados.get("max_posicoes", 5),
        max_trades_dia=dados.get("max_trades_dia", 20),
        perda_diaria_r=dados.get("perda_diaria_r", 4.0),
    )


def estado_da_carteira(carteira: str) -> EstadoCarteira:
    """Retorna o estado atual de uma carteira."""
    if carteira not in _estados:
        _estados[carteira] = EstadoCarteira()
    return _estados[carteira]


def pode_operar(carteira: str, symbol: str) -> tuple[bool, str]:
    """Verifica se uma carteira pode operar um ativo.

    Returns:
        (pode, motivo)
    """
    estado = estado_da_carteira(carteira)
    limites = limites_da_carteira(carteira)

    # Kill switch
    if estado.kill_switch:
        return (False, "KILL_SWITCH")

    # Posição já aberta neste ativo (apenas SOMADOS disputa vaga)
    if carteira == "SOMADOS" and symbol in estado.posicoes_abertas:
        return (False, "ATIVO_POSICIONADO")

    # Limite de posições
    if len(estado.posicoes_abertas) >= limites.max_posicoes:
        return (False, "MAX_POSICOES")

    # Limite de trades por dia
    if estado.trades_hoje >= limites.max_trades_dia:
        return (False, "MAX_TRADES_DIA")

    # Perda diária
    if estado.perda_acumulada_r >= limites.perda_diaria_r:
        return (False, "LIMITE_PERDA_DIARIA")

    return (True, "")


def registrar_abertura(carteira: str, symbol: str, info: dict) -> None:
    """Registra abertura de posição em uma carteira."""
    estado = estado_da_carteira(carteira)
    estado.posicoes_abertas[symbol] = info
    estado.trades_hoje += 1
    log.info("Carteira %s: posição aberta em %s (total: %d)",
             carteira, symbol, len(estado.posicoes_abertas))


def registrar_fechamento(carteira: str, symbol: str, resultado_r: float) -> None:
    """Registra fechamento de posição em uma carteira."""
    estado = estado_da_carteira(carteira)
    if symbol in estado.posicoes_abertas:
        del estado.posicoes_abertas[symbol]
    estado.resultado_dia_r += resultado_r
    if resultado_r < 0:
        estado.perda_acumulada_r += abs(resultado_r)
    log.info("Carteira %s: posição fechada em %s (resultado: %.2fR, dia: %.2fR)",
             carteira, symbol, resultado_r, estado.resultado_dia_r)


def zerar_carteira(carteira: str) -> list[str]:
    """Zera uma carteira (kill switch). Retorna lista de símbolos para fechar."""
    estado = estado_da_carteira(carteira)
    simbolos = list(estado.posicoes_abertas.keys())
    estado.posicoes_abertas.clear()
    estado.kill_switch = True
    log.warning("Carteira %s ZERADA (kill switch). %d posições para fechar.",
                carteira, len(simbolos))
    return simbolos


def liberar_carteira(carteira: str) -> None:
    """Libera uma carteira (após kill switch)."""
    estado = estado_da_carteira(carteira)
    estado.kill_switch = False
    log.info("Carteira %s liberada (kill switch desativado).", carteira)


def verificar_rebaixamento(carteira: str, resultados_recentes: list[float]) -> tuple[bool, str]:
    """Verifica se deve rebaixar para PAPER.

    Critérios:
    - Média das últimas 20 ordens < 0R
    - Drawdown > 10R

    Returns:
        (deve_rebaixar, motivo)
    """
    if len(resultados_recentes) < 5:
        return (False, "")

    # Média das últimas 20 ordens
    ultimos = resultados_recentes[-20:]
    media = sum(ultimos) / len(ultimos)
    if media < 0:
        return (True, f"MÉDIA {media:.2f}R < 0 nas últimas {len(ultimos)} ordens")

    # Drawdown
    pico = 0.0
    drawdown = 0.0
    for r in resultados_recentes:
        pico = max(pico, pico + r)
        drawdown = max(drawdown, pico - (pico + r))
    if drawdown > 10:
        return (True, f"DRAWDOWN {drawdown:.1f}R > 10R")

    return (False, "")


def resumo_carteira(carteira: str) -> dict:
    """Retorna resumo da carteira para o Telegram."""
    estado = estado_da_carteira(carteira)
    limites = limites_da_carteira(carteira)
    return {
        "carteira": carteira,
        "posicoes_abertas": len(estado.posicoes_abertas),
        "simbolos": list(estado.posicoes_abertas.keys()),
        "trades_hoje": estado.trades_hoje,
        "max_trades": limites.max_trades_dia,
        "perda_acumulada_r": estado.perda_acumulada_r,
        "perda_max_r": limites.perda_diaria_r,
        "resultado_dia_r": estado.resultado_dia_r,
        "kill_switch": estado.kill_switch,
    }


def resetar_dia() -> None:
    """Reseta o estado das carteiras (nova sessão)."""
    global _estados
    _estados = {
        "SOMADOS": EstadoCarteira(),
        "RENKO": EstadoCarteira(),
    }
    log.info("Estado das carteiras resetado (nova sessão).")
