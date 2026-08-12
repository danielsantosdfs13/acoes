"""
executor/executor.py

Serviço que envia ordens automaticamente, na VM Windows, ao lado do terminal
MetaTrader 5.

Por que é um serviço separado, e não um pedaço do `analyzer`: o analyzer é
quem gera os sinais, mas roda em k3s, em Linux. A integração do MT5 é binária
de Windows. Ele nunca vai conseguir mandar ordem, por mais natural que
pareça. Mesma restrição que fez o `scraper/` existir — e por isso este
serviço se parece tanto com ele: mesmo gate de pregão, mesmo laço, mesma
entrega por `make`.

O que faz, a cada ciclo:

  1. lê as regras ativas (`GET /auto-ordem`) — (perfil, modalidade, timeframe)
  2. para cada regra, lê os sinais recentes daquele recorte (`GET /signals`)
  3. descarta o que não deve virar ordem (ver as guardas abaixo)
  4. RESERVA o sinal (`POST /ordens`) — se vier `duplicado`, para aqui
  5. envia a ordem a mercado com stop e alvo (`execucao.enviar_ordem_mercado`)
  6. grava o que a corretora respondeu (`PUT /ordens/{signal_id}`)

E, em paralelo, a RECONCILIAÇÃO (`_conciliar`): pergunta ao terminal o que
aconteceu com cada posição em curso e grava o desfecho — preço de saída,
motivo (STOP/ALVO/MANUAL) e resultado em reais, líquido de corretagem. É o
que transforma "mandei 40 ordens" em "estas regras deram dinheiro".

Ela roda mesmo com `ORDENS_HABILITADAS` desligado, porque ler o desfecho do
que já saiu não manda nada — e é justamente o que se quer poder fazer depois
de desligar o envio.

As guardas, e o que cada uma evita:

  - **frescor** (`FRESCOR_MAXIMO_MINUTOS`): sinal de vela velha não vira
    ordem. É a guarda mais importante do arquivo. Sem ela, subir o serviço
    depois de qualquer parada dispararia uma rajada de ordens para sinais de
    horas atrás, a preços que não existem mais — e o pior momento pra isso é
    exatamente a volta de uma queda. Conta a partir do FECHAMENTO da vela, e
    não da abertura: ver `_fechamento_da_vela`, e a medição que mostrou que
    a versão antiga era impossível de satisfazer em M15.
  - **reserva antes do envio**: a linha em `ordens` nasce ANTES de a ordem
    sair, com `signal_id` único. Um reinício no meio do envio encontra a
    reserva e não manda de novo.
  - **posição já aberta**: se o símbolo já tem posição, pula. Sinais de
    modalidades ou velas seguidas dobrariam a exposição no mesmo papel sem
    ninguém ter decidido isso.
  - **interruptor** (`ORDENS_HABILITADAS`): desligado por padrão, aqui e em
    `execucao.py`. Subir o serviço não é o mesmo que autorizar ordem.
  - **tipo de conta**: quem recusa é `execucao.py`, que só opera no tipo
    configurado em `MODO_CONTA_EXIGIDO` (DEMO por padrão).

Uso:
    python executor.py

Ver scraper/README.md para o padrão de serviço Windows via NSSM.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import daytrade_smc  # noqa: E402
import execucao  # noqa: E402
from daytrade_smc import _api_headers, fetch_signals  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("executor")

API_URL = (os.environ.get("ACOES_API_URL") or "").rstrip("/")
CICLO_SEGUNDOS = float(os.environ.get("EXECUTOR_CICLO_SEGUNDOS", "30"))

# Idade máxima da VELA do sinal. 15 minutos cobre uma vela de M15 recém
# fechada com folga pro worker gravá-la; qualquer coisa mais velha que isso
# é história, não oportunidade.
FRESCOR_MAXIMO_MINUTOS = float(os.environ.get("EXECUTOR_FRESCOR_MINUTOS", "15"))

# Intervalo da reconciliação (perguntar à corretora o que aconteceu com as
# posições em curso). Mais folgado que o ciclo de envio de propósito: uma
# posição não fecha a cada 30 segundos, e cada passada abre o terminal.
CONCILIACAO_SEGUNDOS = float(os.environ.get("EXECUTOR_CONCILIACAO_SEGUNDOS", "60"))
# Janela de ordens que a reconciliação ainda persegue. Uma posição esquecida
# por mais que isso é assunto de gente, não de laço.
CONCILIACAO_DIAS = int(os.environ.get("EXECUTOR_CONCILIACAO_DIAS", "30"))

# Interruptor mestre do serviço. Desligado por padrão: instalar e subir o
# serviço não pode ser a mesma decisão que autorizar envio de ordem.
ORDENS_HABILITADAS = os.environ.get("ORDENS_HABILITADAS", "").lower() in ("1", "true", "sim")

# Tipo de conta exigido. Só muda quem quiser operar de verdade, e a mudança
# fica registrada na config do serviço em vez de escondida no código.
MODO_CONTA_EXIGIDO = os.environ.get("MODO_CONTA_EXIGIDO", "DEMO").upper()

MERCADO_TIMEZONE = os.environ.get("MERCADO_TIMEZONE", "America/Sao_Paulo")
MERCADO_ABERTURA_HORA = int(os.environ.get("MERCADO_ABERTURA_HORA", "9"))
MERCADO_FECHAMENTO_HORA = int(os.environ.get("MERCADO_FECHAMENTO_HORA", "19"))
MERCADO_FECHADO_SLEEP_SECONDS = float(os.environ.get("MERCADO_FECHADO_SLEEP_SECONDS", "300"))

TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))


def _mercado_aberto(agora: datetime | None = None) -> bool:
    """Mesma janela folgada do scraper — e pela mesma razão: manter um
    calendário da B3 correto custa mais do que economiza."""
    agora = agora or datetime.now(ZoneInfo(MERCADO_TIMEZONE))
    if agora.weekday() >= 5:
        return False
    return MERCADO_ABERTURA_HORA <= agora.hour < MERCADO_FECHAMENTO_HORA


def _regras() -> list[dict]:
    r = requests.get(f"{API_URL}/auto-ordem", headers=_api_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("regras", [])


def _reservar(sinal: dict, risco: float, teste: bool = False) -> tuple[dict, bool]:
    r = requests.post(
        f"{API_URL}/ordens",
        json={
            "signal_id": sinal["id"], "symbol": sinal["symbol"],
            "direcao": sinal["direcao"], "risco_maximo": risco,
            "stop": sinal.get("stop"), "alvo": sinal.get("alvo_1"),
            "teste": teste,
        },
        headers=_api_headers(), timeout=TIMEOUT,
    )
    r.raise_for_status()
    corpo = r.json()
    return corpo["ordem"], corpo["duplicado"]


def _registrar(signal_id: int, dados: dict) -> None:
    r = requests.put(
        f"{API_URL}/ordens/{signal_id}", json=dados,
        headers=_api_headers(), timeout=TIMEOUT,
    )
    r.raise_for_status()


def _ordens_abertas() -> list[dict]:
    """Ordens que saíram e ainda não têm fechamento gravado.

    `incluir_testes=true` e não o default: a rota esconde ordem de validação
    das ESTATÍSTICAS, que é onde ela suja. Aqui não — uma posição de teste
    aberta na corretora é uma posição aberta, e deixá-la fora da
    reconciliação a deixaria pendente pra sempre, ocupando o símbolo na
    guarda de "posição já aberta"."""
    r = requests.get(
        f"{API_URL}/ordens",
        params={"aberta": "true", "dias": CONCILIACAO_DIAS, "limite": 500,
                "incluir_testes": "true"},
        headers=_api_headers(), timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("ordens", [])


def _registrar_fechamento(signal_id: int, dados: dict) -> None:
    r = requests.put(
        f"{API_URL}/ordens/{signal_id}/fechamento", json=dados,
        headers=_api_headers(), timeout=TIMEOUT,
    )
    r.raise_for_status()


def _conferir_fuso(ordem: dict, abertura_em: datetime | None) -> None:
    """Compara a hora que o MT5 dá para a abertura com a que o Postgres
    gravou ao RESERVAR a ordem — segundos antes de ela sair, e portanto UTC
    de verdade.

    Existe porque `execucao.hora_do_mt5` reinterpreta o epoch do terminal
    como relógio do servidor (ver a docstring de lá). Se algum dia a
    convenção mudar, ou o servidor trocar de fuso, o erro seria de 3 horas em
    datas que continuam plausíveis — o tipo de coisa que não quebra nada e
    contamina todo relatório por dia. Aqui ele vira aviso no log."""
    if abertura_em is None:
        return
    criado = datetime.fromisoformat(ordem["criado_em"])
    desvio = abs((abertura_em - criado).total_seconds())
    if desvio > 1800:
        log.warning(
            "FUSO SUSPEITO na ordem %s: o MT5 diz que a posição abriu em %s, mas a "
            "reserva foi gravada em %s (%.1f h de diferença). Confira a conversão em "
            "execucao.hora_do_mt5 antes de confiar nos horários de fechamento.",
            ordem["signal_id"], abertura_em.isoformat(), criado.isoformat(), desvio / 3600,
        )


def _conciliar() -> None:
    """Pergunta à corretora o que aconteceu com cada posição em curso.

    Roda mesmo com `ORDENS_HABILITADAS` desligado: ler o desfecho do que já
    foi enviado não manda nada, e é exatamente o que se quer poder fazer
    depois de desligar o envio.

    Uma conexão para o lote inteiro — o `_tem_posicao` conecta e desconecta
    por chamada, o que aqui viraria N conexões por ciclo."""
    abertas = _ordens_abertas()
    if not abertas:
        return

    import MetaTrader5 as mt5

    daytrade_smc._mt5_conectar(mt5)
    try:
        for ordem in abertas:
            ticket = ordem.get("ticket")
            if not ticket:
                continue
            try:
                estado = execucao.consultar_posicao(mt5, int(ticket))
            except Exception as exc:  # noqa: BLE001 — uma linha ruim não para o lote
                log.warning("ordem %s: falha ao consultar ticket %s: %s",
                            ordem["signal_id"], ticket, exc)
                continue
            if estado is None:
                continue

            _conferir_fuso(ordem, estado.pop("abertura_em", None))
            fechou = not estado.pop("aberta")
            _registrar_fechamento(ordem["signal_id"], {
                "resultado_reais": estado["resultado_reais"],
                "fechado_em": estado["fechado_em"].isoformat() if estado["fechado_em"] else None,
                "preco_saida": estado["preco_saida"],
                "volume_saida": estado["volume_saida"],
                "motivo_saida": estado["motivo_saida"],
            })
            if fechou:
                log.info(
                    "FECHOU %s ticket=%s por %s a %s: R$ %.2f",
                    ordem["symbol"], ticket, estado["motivo_saida"],
                    estado["preco_saida"], estado["resultado_reais"],
                )
    finally:
        mt5.shutdown()


def _tem_posicao(symbol: str) -> bool:
    """O símbolo já tem posição aberta nesta conta?

    Consulta direta ao terminal, e não ao banco: a posição pode ter sido
    aberta à mão, ou fechada no stop sem ninguém avisar o banco. Quem sabe
    a verdade é a corretora."""
    import MetaTrader5 as mt5

    daytrade_smc._mt5_conectar(mt5)
    try:
        posicoes = mt5.positions_get(symbol=symbol)
        return bool(posicoes)
    finally:
        mt5.shutdown()


def _quantidade_estimada(sinal: dict, risco_maximo: float) -> float | None:
    """Quantidade ESTIMADA, só pro log e pra descartar sinal sem plano.

    Quem dimensiona de verdade é `execucao`, contra a cotação do instante do
    envio (`risco_maximo=`). Até 2026-08-12 a conta feita aqui era a que
    valia, e ela usa a `entrada` MODELADA do sinal: medido na primeira ordem
    de validação, um sinal de entrada 41,60 e stop 41,46 preencheu a 41,78 e
    o risco real virou R$ 128 onde a regra pedia R$ 50. A distância até o
    stop mais que dobrou entre o sinal e o preenchimento, e só quem envia
    enxerga isso."""
    entrada, stop = sinal.get("entrada"), sinal.get("stop")
    if not entrada or not stop:
        return None
    risco_por_acao = abs(entrada - stop)
    if risco_por_acao <= 0:
        return None
    return risco_maximo / risco_por_acao


def _fechamento_da_vela(sinal: dict) -> datetime:
    """Quando a vela do sinal FECHOU.

    `candle_time` é a ABERTURA da vela (ver o comentário da coluna em
    schema.sql), então uma vela de M15 recém-fechada já nasce com
    `candle_time` de 15 minutos atrás. Comparar o frescor contra a abertura
    descontava a duração inteira da vela do orçamento — e em M15, onde a
    janela de frescor é do mesmo tamanho da vela, isso a zerava.

    Medido em 2026-08-12, com 24h de dados de produção: dos 3.060 sinais de
    M15 gravados, ZERO passavam na trava, e o atraso MÍNIMO observado era de
    15,2 minutos — logo depois do corte de 15. Não era calibragem apertada,
    era condição impossível: as 6 regras ativas casaram com 1.141 sinais e
    nenhum virou ordem. Medindo do fechamento, 2.985 dos 3.060 passam, com
    8,6 minutos de atraso médio depois de a vela fechar.

    Timeframe desconhecido devolve duração zero, que reproduz o
    comportamento antigo (mais restritivo) em vez de deixar passar sinal
    velho por engano."""
    abertura = datetime.fromisoformat(sinal["candle_time"])
    tf = daytrade_smc.TIMEFRAMES.get(sinal.get("timeframe") or "")
    if tf is None:
        return abertura
    return abertura + tf["duration"].to_pytimedelta()


def _elegiveis(regra: dict, limite_idade: datetime) -> list[dict]:
    """Sinais desta regra que ainda são candidatos a virar ordem."""
    resposta = fetch_signals(
        perfil=regra["perfil"], modalidade=regra["modalidade"],
        timeframe=regra["timeframe"], origem="worker", dias=1, limite=200,
    )
    candidatos = []
    for s in resposta.get("signals", []):
        if not s.get("entrada") or not s.get("stop"):
            continue
        if s.get("direcao") not in ("COMPRA", "VENDA"):
            continue
        if _fechamento_da_vela(s) < limite_idade:
            continue
        candidatos.append(s)
    return candidatos


def _processar(sinal: dict, regra: dict, *, teste: bool = False) -> None:
    """Um sinal virando ordem.

    `teste=True` é o caminho de quem está conferindo o encanamento à mão: a
    ordem sai igual, com as mesmas travas, mas a linha nasce marcada e fica
    fora das estatísticas. O laço do serviço NUNCA passa isso — só chamada
    manual passa, o que é exatamente o que distingue uma da outra.

    Marcar em vez de apagar depois: a primeira ordem de validação teve que
    ser removida na mão do banco, e apagar de tabela de auditoria some com um
    evento que aconteceu de verdade."""
    symbol, sid = sinal["symbol"], sinal["id"]
    risco = float(regra["risco_maximo"])
    estimada = _quantidade_estimada(sinal, risco)
    if estimada is None:
        return

    if _tem_posicao(symbol):
        log.info("%s: já tem posição aberta, pulando sinal %s.", symbol, sid)
        return

    ordem, duplicado = _reservar(sinal, risco, teste=teste)
    if duplicado:
        log.debug("sinal %s já tinha ordem (%s), pulando.", sid, ordem["status"])
        return

    log.info(
        "ENVIANDO%s %s %s ~%.0f ações (risco R$%.2f, stop %.2f, alvo %.2f) sinal=%s",
        " [TESTE]" if teste else "",
        sinal["direcao"], symbol, estimada, risco,
        sinal["stop"], sinal.get("alvo_1") or 0, sid,
    )
    try:
        # `risco_maximo` e não `quantidade`: o dimensionamento acontece lá
        # dentro, contra a cotação do envio. Ver `_quantidade_estimada`.
        r = execucao.enviar_ordem_mercado(
            symbol, sinal["direcao"],
            risco_maximo=risco,
            stop=sinal.get("stop"), alvo=sinal.get("alvo_1"),
            comentario=f"auto {regra['modalidade'][:8]}",
        )
    except execucao.OrdemRecusada as exc:
        # RECUSADA e não FALHOU: as travas locais barraram, nada saiu da
        # máquina. A distinção importa na auditoria — uma é configuração
        # errada aqui, a outra é a corretora dizendo não.
        log.warning("sinal %s recusado localmente: %s", sid, exc)
        _registrar(sid, {"status": "RECUSADA", "mensagem": str(exc)[:500]})
        return
    except Exception as exc:  # noqa: BLE001
        log.error("sinal %s falhou no envio: %s", sid, exc)
        _registrar(sid, {"status": "FALHOU", "mensagem": str(exc)[:500]})
        return

    _registrar(sid, {
        "status": "ENVIADA" if r["enviado"] else "FALHOU",
        "volume": r["volume"], "preco_pedido": r["preco_pedido"],
        "preco_executado": r.get("preco_executado"), "conta": r["conta"],
        "servidor": r["servidor"], "tipo_conta": r["tipo_conta"],
        "ticket": r.get("ticket"), "retcode": r.get("retcode"),
        "mensagem": (r.get("comentario") or "")[:500],
    })
    log.info(
        "%s ticket=%s preço=%s vol=%s risco R$%.2f de R$%.2f (%s)",
        "ENVIADA" if r["enviado"] else "FALHOU",
        r.get("ticket"), r.get("preco_executado"), r.get("volume_executado"),
        r.get("risco_efetivo") or 0.0, risco, r.get("comentario"),
    )


def _um_ciclo() -> None:
    limite = datetime.now(UTC) - timedelta(minutes=FRESCOR_MAXIMO_MINUTOS)
    for regra in _regras():
        try:
            for sinal in _elegiveis(regra, limite):
                _processar(sinal, regra)
        except Exception as exc:  # noqa: BLE001 — uma regra ruim não derruba as outras
            log.warning("regra %s/%s/%s falhou: %s", regra.get("perfil"),
                        regra.get("modalidade"), regra.get("timeframe"), exc)


def main() -> None:
    if not API_URL:
        raise SystemExit("ACOES_API_URL não configurada — o executor lê os sinais por ela.")
    daytrade_smc.ACOES_API_URL = API_URL
    daytrade_smc.ACOES_API_KEY = os.environ.get("ACOES_API_KEY") or None
    daytrade_smc.MT5_PATH = os.environ.get("MT5_PATH") or None
    daytrade_smc.MT5_LOGIN = os.environ.get("MT5_LOGIN") or None
    daytrade_smc.MT5_SERVER = os.environ.get("MT5_SERVER") or None
    daytrade_smc.MT5_PASSWORD = os.environ.get("MT5_PASSWORD") or None

    execucao.ORDENS_HABILITADAS = ORDENS_HABILITADAS
    execucao.MODO_CONTA_EXIGIDO = MODO_CONTA_EXIGIDO

    conta = daytrade_smc.mt5_conta_ativa()
    log.info(
        "Executor — api=%s ciclo=%ss frescor=%smin | conta %s (%s) tipo=%s",
        API_URL, CICLO_SEGUNDOS, FRESCOR_MAXIMO_MINUTOS,
        conta["login"], conta["servidor"], conta["tipo"],
    )
    if not ORDENS_HABILITADAS:
        log.warning(
            "ORDENS_HABILITADAS está DESLIGADO: o executor vai varrer as regras "
            "e registrar o que FARIA, sem mandar nada. Ligue na config do "
            "serviço quando quiser valer."
        )
    if conta["tipo"] != MODO_CONTA_EXIGIDO:
        log.error(
            "Conta é %s e o exigido é %s — toda ordem será recusada até isso bater.",
            conta["tipo"], MODO_CONTA_EXIGIDO,
        )

    def conciliar_protegido(motivo: str) -> None:
        try:
            _conciliar()
        except Exception as exc:  # noqa: BLE001 — não pode derrubar o envio
            log.error("reconciliação (%s) falhou: %s", motivo, exc)

    # No startup, antes de qualquer coisa: se o serviço passou um tempo fora,
    # é aqui que o que fechou nesse intervalo entra no banco.
    conciliar_protegido("startup")

    estava_aberto: bool | None = None
    ultima_conciliacao = 0.0
    while True:
        aberto = _mercado_aberto()
        if aberto != estava_aberto:
            log.info("Mercado %s.", "ABERTO" if aberto else "FECHADO")
            # Na virada para fechado, uma última passada: sem ela, o que
            # fechar no leilão só apareceria no pregão seguinte.
            if estava_aberto and not aberto:
                conciliar_protegido("fechamento do pregão")
            estava_aberto = aberto
        if not aberto:
            time.sleep(MERCADO_FECHADO_SLEEP_SECONDS)
            continue
        if time.monotonic() - ultima_conciliacao >= CONCILIACAO_SEGUNDOS:
            ultima_conciliacao = time.monotonic()
            conciliar_protegido("ciclo")
        try:
            _um_ciclo()
        except Exception as exc:  # noqa: BLE001
            log.error("ciclo falhou: %s", exc)
        time.sleep(CICLO_SEGUNDOS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Encerrado pelo usuário.")
