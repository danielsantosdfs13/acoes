"""
execucao.py

Envio de ordens ao MetaTrader 5. É o ÚNICO lugar do repositório que manda
ordem; todo o resto lê preço e calcula plano.

Mora fora de `daytrade_smc.py` de propósito: aquele arquivo é o motor de
análise ("No UI code lives here", e nenhuma escrita no mundo). Ordem não é
análise — é o efeito colateral irreversível que a análise sugere. Misturar
os dois faria com que qualquer import do motor carregasse junto a capacidade
de operar.

Roda SÓ na VM Windows, ao lado de um terminal MT5 aberto — mesma restrição
do `scraper/`, mesma DLL. O `analyzer` do k3s NÃO consegue enviar ordem, por
mais que seja ele quem gera os sinais: é Linux, e a integração do MT5 é
binária de Windows. Daí a existência do `executor/` como serviço separado.

As travas, na ordem em que rodam:

  1. `ORDENS_HABILITADAS` — interruptor mestre, DESLIGADO por padrão. Um
     import acidental deste módulo não consegue mandar nada.
  2. conta certa — `_mt5_conectar` recusa se o terminal estiver servindo
     outro login que não o de `MT5_LOGIN`.
  3. `MODO_CONTA_EXIGIDO` — recusa se o tipo da conta não for o exigido.
     Padrão "DEMO": enquanto ninguém mudar isso explicitamente, este módulo
     é incapaz de tocar em dinheiro de verdade, mesmo que o terminal errado
     responda.
  4. distância mínima até o stop (`STOP_MINIMO_ATR`) — contra a cotação do
     momento. Um stop a um centavo do preço é executado por oscilação, não
     por a tese ter falhado.
  5. coerência de stop e alvo — também contra a COTAÇÃO do momento, e depois
     de o alvo ter sido recolocado (`_alvo_por_rr`), para conferir o que
     realmente sai. A trava 4 confere a DISTÂNCIA do stop; esta confere o
     LADO de ambos, e alvo atrás do preço é prejuízo garantido que ainda se
     grava como "bateu o alvo".
  6. volume — dimensionado pelo preço de agora quando vem `risco_maximo`, e
     normalizado pelo passo do símbolo, porque volume inválido é rejeição no
     servidor, não erro local.
  7. `order_check` antes de `order_send` — a corretora valida margem e
     preços sem executar nada.

A trava 3 é a que importa: as outras protegem contra engano de
configuração, essa protege contra a não-determinação medida em 2026-08-11,
quando dois terminais (real e demo) da mesma instalação faziam o
`initialize()` cair ora num, ora noutro, sem nada configurado ter mudado.

As travas 5 e 6 nasceram juntas, da primeira ordem de validação
(2026-08-12): PETR4 de um sinal com entrada 41,60 e stop 41,46 preencheu a
41,78, o que fez o risco real virar R$ 128 onde a regra pedia R$ 50, e
mandou junto um alvo em 41,72 — atrás da própria entrada. Nenhuma das duas
coisas era visível antes de a reconciliação existir.

A trava 4 e a recolocação do alvo (`_alvo_por_rr`) vieram da mesma raiz,
medidas depois com volume: as 39 primeiras
ordens renderam -R$ 1.369 com 31% de acerto, enquanto os MESMOS recortes
mediam +0,17R e 61% de acerto na tabela de sinais. A diferença é que o
executor manda os níveis modelados sobre um preenchimento que acontece
minutos depois, a outro preço — e quando o preço andou A FAVOR, o stop
estrutural ficou a centavos do preenchimento. Nessa faixa (< 0,6 ATR) foram
seis ordens, seis stops, -1,00R cada: 40% de todo o prejuízo. Ver
`STOP_MINIMO_ATR`.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from daytrade_smc import LOCAL_TZ, _mt5_conectar

log = logging.getLogger("execucao")

# Interruptor mestre. Ligado por quem opera (o executor lê do ambiente),
# nunca por padrão.
ORDENS_HABILITADAS = False

# Tipo de conta em que este módulo aceita operar: "DEMO", "REAL" ou
# "CONCURSO". Trocar isto para "REAL" é a decisão de passar a arriscar
# dinheiro — está num só lugar, e é de propósito que seja um lugar chato de
# achar por acidente.
MODO_CONTA_EXIGIDO = "DEMO"

# Desvio máximo aceito entre o preço pedido e o executado, em "points" do
# símbolo. Ordem a mercado no book da B3 raramente desliza, mas 0 faria a
# corretora rejeitar em vez de preencher um tick adiante.
DESVIO_MAXIMO_POINTS = 20

# Distância MÍNIMA até o stop no instante do envio, em ATRs. Abaixo disso o
# stop está dentro do ruído do próprio ativo e é executado por oscilação, não
# por a tese ter falhado.
#
# O motor já tem essa ideia: `stop_minimo_atr` (0,85 no perfil fine_tuned_v2)
# alarga o stop que nasceu apertado demais. Só que ele mede contra a entrada
# MODELADA, e entre o fechamento da vela e o preenchimento o preço anda — se
# andar a favor, a distância até o stop encolhe e o piso do motor evapora sem
# que nada perceba.
#
# Medido em 2026-08-12 sobre as 39 ordens enviadas até aqui, por faixa de
# distância até o stop no preenchimento:
#
#   < 0,6 ATR   6 ordens   6 fechadas   0% de acerto   -1,00R CADA UMA
#   0,6-1,0     6 ordens   5 fechadas  60% de acerto   +0,30R
#   >= 1,0     27 ordens  18 fechadas  33% de acerto   -0,58R
#
# Seis de seis, sem exceção, e valendo -6R dos -14,9R que a carteira inteira
# perdeu: 40% do prejuízo saiu desta faixa. Os stops eram de UM a QUATRO
# centavos (MGLU3 com R$ 0,01, BBAS3 com R$ 0,03).
STOP_MINIMO_ATR = 0.6

# Tipos de conta do MT5. Fora das funções porque agora são DUAS as que
# precisam conferir isto: o envio e a movimentação de stop.
_TIPOS_CONTA = {0: "DEMO", 1: "CONCURSO", 2: "REAL"}


class OrdemRecusada(RuntimeError):
    """Recusa ANTES de qualquer coisa sair da máquina.

    Distinta de falha do servidor: se isto sobe, nada foi enviado e nada
    precisa ser desfeito."""


def _conferir_tipo_conta(conta) -> str:
    """Devolve o tipo da conta, recusando se não for o exigido.

    Vale para tudo que escreve na corretora, e não só para o envio: apertar o
    stop de uma posição também é uma escrita, e a trava que impede este módulo
    de tocar em dinheiro de verdade não pode ter porta dos fundos."""
    tipo = _TIPOS_CONTA.get(conta.trade_mode, str(conta.trade_mode))
    if tipo != MODO_CONTA_EXIGIDO:
        raise OrdemRecusada(
            f"Conta é {tipo} e este módulo só opera em {MODO_CONTA_EXIGIDO}: "
            f"login {conta.login} em {conta.server}. Nada foi enviado."
        )
    return tipo


def _normalizar_volume(info, volume: float) -> float:
    """Ajusta o volume ao passo do símbolo e confere os limites.

    A B3 negocia ação em lote de 100; o `volume_step` do símbolo é quem diz
    isso, e mandar 150 num passo de 100 é rejeição no servidor — erro que
    chega tarde, depois da ida e volta, e sem dizer que o problema era o
    arredondamento."""
    passo = info.volume_step or 1.0
    ajustado = round(round(volume / passo) * passo, 8)
    if ajustado < info.volume_min:
        raise OrdemRecusada(
            f"Volume {volume:g} fica abaixo do mínimo de {symbol_str(info)} "
            f"({info.volume_min:g}); com o passo de {passo:g} viraria {ajustado:g}."
        )
    if info.volume_max and ajustado > info.volume_max:
        raise OrdemRecusada(
            f"Volume {ajustado:g} passa do máximo de {symbol_str(info)} ({info.volume_max:g})."
        )
    return ajustado


def symbol_str(info) -> str:
    return getattr(info, "name", "?")


# Máscara de `symbol_info().filling_mode` — o que o SÍMBOLO aceita.
#
# Ficam aqui como literais porque o pacote Python não os exporta: ele expõe
# só os `ORDER_FILLING_*`, que são o valor a mandar no pedido (0=FOK, 1=IOC,
# 2=RETURN) e NÃO servem para testar a máscara. Usar um pelo outro passa no
# import e erra em silêncio — `ORDER_FILLING_FOK` é 0, e `mascara & 0` é
# sempre falso. Valores conforme ENUM_SYMBOL_FILLING_FLAGS do MQL5.
_SYMBOL_FILLING_FOK = 1
_SYMBOL_FILLING_IOC = 2


def _modo_preenchimento(mt5, info) -> int:
    """Escolhe um filling mode que o símbolo aceite.

    Não é detalhe: cravar FOK dá "Unsupported filling mode" em quem só
    aceita outro, e a mensagem não diz qual usar. Medido na Clear em
    2026-08-11: VALE3 e WIN$ devolvem `filling_mode=3`, ou seja FOK e IOC
    aceitos."""
    permitidos = getattr(info, "filling_mode", 0)
    if permitidos & _SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    if permitidos & _SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    # Nenhum bit setado: RETURN é o que sobra para ordem a mercado.
    return mt5.ORDER_FILLING_RETURN


# Quanto esperar pela PRIMEIRA cotação de um símbolo recém-adicionado ao
# Market Watch. Folgado em relação ao observado (o segundo pedido, ~1s depois,
# já vinha bom) e curto em relação ao ciclo do executor, que é de 30s.
ESPERA_COTACAO_SEGUNDOS = 3.0


def _esperar_cotacao(mt5, symbol: str):
    """A primeira cotação depois de o símbolo entrar no Market Watch.

    `symbol_select(symbol, True)` devolve True na hora, mas o terminal ainda
    vai assinar o símbolo e receber o primeiro tick — e nesse intervalo o
    `symbol_info_tick` volta vazio. Pedir uma vez só e desistir transforma
    isso em recusa, e a recusa é definitiva: a linha em `ordens` já existe, o
    dedup impede nova tentativa, e o sinal se perde.

    Medido em produção em 2026-08-12, no primeiro ciclo em que o executor
    disparou de verdade: das 11 tentativas, 6 foram recusadas com "sem
    cotação" e QUATRO delas eram o primeiro pedido de um símbolo que, um
    segundo depois, foi negociado sem problema por outra regra. BBAS3 e PRIO3
    tinham uma regra só e ficaram sem ordem nenhuma — sinal legítimo perdido
    por corrida de inicialização.

    A pista de que não era mercado fechado estava no próprio erro:
    `last_error()` devolvia `(1, 'Success')`, ou seja, NENHUM erro. Não era
    "não há preço", era "o preço ainda não chegou".
    """
    import time

    limite = time.monotonic() + ESPERA_COTACAO_SEGUNDOS
    while True:
        tick = mt5.symbol_info_tick(symbol)
        if tick is not None and (tick.ask or tick.bid):
            return tick
        if time.monotonic() >= limite:
            raise OrdemRecusada(
                f"Sem cotação para {symbol} depois de "
                f"{ESPERA_COTACAO_SEGUNDOS:g}s ({mt5.last_error()}) — mercado "
                "fechado, símbolo sem book, ou fora do pregão deste ativo."
            )
        time.sleep(0.2)


def _alvo_por_rr(info, direcao: str, preco: float, stop: float | None,
                 rr: float | None) -> float | None:
    """Recoloca o alvo à distância contratada do preço que vai ser PAGO.

    O alvo do sinal foi calculado sobre o fechamento da vela; a ordem sai a
    mercado depois disso. Mandar o alvo modelado junto de um preenchimento
    que andou é assinar um R/R que não é o que o perfil pediu — e as duas
    caudas disso foram medidas em 2026-08-12, nas 39 primeiras ordens:

      - R/R no envio com mediana 1,00 mas faixa de 0,02 a 7,00, contra um
        projeto de 0,8 a 1,2.
      - Quando o preço andou A FAVOR, o alvo ficou a um centavo do
        preenchimento e a ordem fechou registrada como "ALVO" pagando
        +0,02R. Quatro dos nove ganhos eram ruído assim: inflam a taxa de
        acerto sem pagar nada, que é exatamente o jeito de a medição mentir.

    O stop NÃO se mexe: ele é estrutural (`stop_for_signal` o tira de swing,
    EMA21 ou mínima de 10 velas), e movê-lo para uma distância arbitrária do
    preenchimento destruiria a justificativa dele. Quem se recoloca é o
    alvo, que é só uma razão sobre o risco.

    A régua é a cotação do envio, não o preenchimento de fato — este só se
    conhece depois do `order_send`, e a diferença entre os dois está limitada
    a `DESVIO_MAXIMO_POINTS`. Sem `rr`, devolve None e o chamador mantém o
    alvo que recebeu."""
    if rr is None or stop is None or rr <= 0:
        return None
    risco = abs(preco - stop)
    if risco <= 0:
        return None
    # Arredonda PARA LONGE da entrada, como `attach_risk` já faz no motor: em
    # papel barato o tique de R$ 0,01 é uma fatia grande da distância, e
    # arredondar para dentro encurta o alvo justamente onde ele já é curto.
    digitos = getattr(info, "digits", 2) or 2
    bruto = preco + rr * risco if direcao == "COMPRA" else preco - rr * risco
    fator = 10 ** digitos
    if direcao == "COMPRA":
        return math.ceil(bruto * fator) / fator
    return math.floor(bruto * fator) / fator


def _conferir_coerencia(direcao: str, preco: float, stop: float | None,
                        alvo: float | None) -> None:
    """Stop e alvo têm que estar do lado certo do preço que vai ser pago.

    Medido em 2026-08-12, na primeira ordem de validação: PETR4 comprado a
    41,78 com alvo em 41,72 — o alvo ATRÁS da entrada. Ninguém recusou, nem
    as travas locais nem o `order_check` da corretora, e a posição vira
    prejuízo garantido que ainda por cima é gravado como `motivo_saida=ALVO`.
    "Bateu o alvo" perdendo dinheiro contamina toda comparação entre regras.

    A trava de frescor não cobre isso: um gap na abertura põe o alvo atrás
    do preço com um sinal recém-nascido. Por isso a conferência é contra a
    COTAÇÃO do momento do envio, não contra a entrada modelada do sinal."""
    lado = "abaixo" if direcao == "COMPRA" else "acima"
    contra = "acima" if direcao == "COMPRA" else "abaixo"
    if stop is not None:
        errado = stop >= preco if direcao == "COMPRA" else stop <= preco
        if errado:
            raise OrdemRecusada(
                f"Stop {stop:g} está {contra} do preço {preco:g} numa {direcao} — "
                f"numa {direcao} o stop tem que ficar {lado}. Nada foi enviado."
            )
    if alvo is not None:
        errado = alvo <= preco if direcao == "COMPRA" else alvo >= preco
        if errado:
            raise OrdemRecusada(
                f"Alvo {alvo:g} está {lado} do preço {preco:g} numa {direcao} — "
                f"a ordem já nasceria no prejuízo, e fecharia registrada como "
                f"'ALVO'. Nada foi enviado."
            )


def _conferir_stop_minimo(preco: float, stop: float | None,
                          atr: float | None) -> None:
    """O stop tem que estar longe o bastante do preço para medir a tese, e
    não o ruído.

    A trava anterior (`_conferir_coerencia`) confere o LADO do stop; esta
    confere a DISTÂNCIA. São defeitos diferentes: um stop do lado certo a um
    centavo do preço passa naquela e é executado pela primeira oscilação.

    Só roda quando o chamador informa o ATR — sem ele não há régua, e inventar
    uma a partir do preço (um percentual fixo) mediria volatilidade errada em
    todo ativo que não fosse o da calibragem. Ausente, o comportamento é o de
    antes, que é o que mantém a chamada manual de conferência funcionando.

    Ver o comentário de `STOP_MINIMO_ATR` para os números que motivaram o
    corte: nesta faixa, seis ordens em seis terminaram no stop."""
    if stop is None or not atr or atr <= 0:
        return
    distancia = abs(preco - stop)
    minima = STOP_MINIMO_ATR * atr
    if distancia < minima:
        raise OrdemRecusada(
            f"Stop {stop:g} está a {distancia:.4g} do preço {preco:g}, menos que "
            f"o mínimo de {minima:.4g} ({STOP_MINIMO_ATR:g} × ATR {atr:.4g}). "
            f"A esta distância quem executa o stop é o ruído do ativo, não a "
            f"tese: medidas em 2026-08-12, seis ordens em seis nesta faixa "
            f"fecharam no stop, a -1,00R cada. Nada foi enviado."
        )


def _volume_por_risco(info, risco_maximo: float, preco: float,
                      stop: float | None) -> float:
    """Quantidade a partir do risco em reais e do preço que vai ser PAGO.

    A conta em si é a mesma de sempre — risco ÷ distância até o stop — mas o
    preço é o da cotação de agora, e não a `entrada` modelada do sinal. Essa
    é a diferença que importa: medido em 2026-08-12, um sinal com entrada
    41,60 e stop 41,46 preencheu a 41,78, e o risco real virou R$ 128 onde a
    regra pedia R$ 50 — 2,6×, porque a distância até o stop mais que dobrou
    entre o sinal e o preenchimento.

    Só quem envia enxerga a cotação do instante do envio, e é por isso que a
    conta mora aqui e não no `executor`.

    O arredondamento ao lote é PARA BAIXO, e isso é o que faz o "máximo" do
    nome valer: a B3 negocia de 100 em 100, e arredondar ao mais próximo
    passa do teto na metade das vezes (156 ações viram 200, e R$ 50 de risco
    viram R$ 64). Para baixo, o risco fica sempre igual ou menor que o
    pedido.

    A exceção é quando nem um lote cabe no risco — papel caro com stop
    largo. Aí a escolha é entre mandar o lote mínimo estourando o teto ou
    nunca operar aquele papel. Manda, e AVISA: "esta regra nunca opera VALE3"
    é uma falha silenciosa pior que um risco maior explicado no log."""
    if stop is None:
        raise OrdemRecusada(
            "Dimensionar por risco exige um stop — sem ele não existe distância "
            "de risco pra dividir. Nada foi enviado."
        )
    distancia = abs(preco - stop)
    if distancia <= 0:
        raise OrdemRecusada(
            f"Stop {stop:g} coincide com o preço {preco:g}: risco por ação zero, "
            "quantidade infinita. Nada foi enviado."
        )

    ideal = risco_maximo / distancia
    passo = info.volume_step or 1.0
    volume = round((ideal // passo) * passo, 8)
    if volume < info.volume_min:
        minimo = info.volume_min
        log.warning(
            "%s: nem um lote mínimo (%g) cabe em R$%.2f de risco — o ideal eram "
            "%.1f ações. Mandando %g, o que arrisca R$%.2f (%.0f%% do pedido).",
            symbol_str(info), minimo, risco_maximo, ideal, minimo,
            minimo * distancia, minimo * distancia / risco_maximo * 100,
        )
        return minimo
    return volume


def enviar_ordem_mercado(
    symbol: str,
    direcao: str,
    volume: float | None = None,
    stop: float | None = None,
    alvo: float | None = None,
    comentario: str = "",
    simular: bool = False,
    risco_maximo: float | None = None,
    atr: float | None = None,
    rr: float | None = None,
) -> dict:
    """Envia uma ordem A MERCADO com stop e alvo anexados.

    `direcao` é "COMPRA" ou "VENDA" — o mesmo vocabulário de `Direction`, pra
    não haver tradução no meio do caminho.

    O tamanho vem de UM dos dois: `volume` (quantidade pronta) ou
    `risco_maximo` (em reais, dimensionado aqui dentro contra a cotação do
    momento — ver `_volume_por_risco`). `risco_maximo` é o caminho preferido
    de quem opera por regra, porque é o único que faz o risco configurado
    valer depois que o preço andou.

    `atr` é o ATR do sinal que originou a ordem, e serve de régua para a
    trava de distância mínima até o stop (`_conferir_stop_minimo`). É
    opcional porque só quem veio de um sinal o tem; sem ele a trava não roda
    e o comportamento é o de antes.

    `rr` é a razão risco/retorno contratada (o `r_alvo_1` do sinal). Quando
    vem, o `alvo` recebido é só referência: o que sai é recalculado a essa
    razão sobre o risco REAL, medido do preço do envio até o stop — ver
    `_alvo_por_rr`. Sem ele, o `alvo` vai como veio.

    `simular=True` roda só o `order_check`: monta a ordem de verdade e deixa
    a corretora validar margem, preço e volume, sem executar. É o caminho
    para conferir uma configuração nova sem consequência.

    Devolve um dict com o que aconteceu, sempre — inclusive na simulação.
    """
    import MetaTrader5 as mt5  # import tardio — DLL de Windows

    if direcao not in ("COMPRA", "VENDA"):
        raise OrdemRecusada(f"Direção inválida: {direcao!r}. Use 'COMPRA' ou 'VENDA'.")

    if (volume is None) == (risco_maximo is None):
        raise OrdemRecusada(
            "Passe `volume` OU `risco_maximo`, nunca os dois nem nenhum — senão "
            "não dá pra saber qual manda no tamanho da posição."
        )

    if not ORDENS_HABILITADAS and not simular:
        raise OrdemRecusada(
            "Envio de ordens desligado (ORDENS_HABILITADAS=False). Este é o "
            "padrão: ligue explicitamente em quem opera, nunca no módulo."
        )

    conta = _mt5_conectar(mt5)
    try:
        tipo = _conferir_tipo_conta(conta)

        if not mt5.symbol_select(symbol, True):
            raise OrdemRecusada(f"Símbolo {symbol} não existe nesta conta ({mt5.last_error()}).")
        info = mt5.symbol_info(symbol)
        if info is None:
            raise OrdemRecusada(f"Sem informação do símbolo {symbol} ({mt5.last_error()}).")

        # A COTAÇÃO vem antes do volume, e a ordem aqui é o conserto de
        # 2026-08-12: enquanto o volume era normalizado primeiro, o tamanho da
        # posição saía da entrada MODELADA do sinal e ninguém conferia se o
        # stop e o alvo ainda faziam sentido contra o preço de agora.
        tick = _esperar_cotacao(mt5, symbol)

        comprar = direcao == "COMPRA"
        preco = tick.ask if comprar else tick.bid

        # A distância até o stop é conferida ANTES de o alvo ser recolocado:
        # é ela que serve de base para o alvo, e um stop dentro do ruído não
        # vira ordem boa só porque o alvo foi recalculado em cima dele.
        _conferir_stop_minimo(preco, stop, atr)

        alvo_recolocado = _alvo_por_rr(info, direcao, preco, stop, rr)
        if alvo_recolocado is not None:
            if alvo is not None:
                log.info(
                    "%s: alvo recolocado de %g para %g (R/R %g contra o risco "
                    "real de %g)", symbol, alvo, alvo_recolocado, rr,
                    abs(preco - stop),
                )
            alvo = alvo_recolocado

        # Depois da recolocação, para conferir o que REALMENTE vai sair. Na
        # ordem inversa a trava validaria um alvo que já não é o enviado.
        _conferir_coerencia(direcao, preco, stop, alvo)

        if risco_maximo is not None:
            volume = _volume_por_risco(info, risco_maximo, preco, stop)
        # Segue rodando no caminho por risco também: lá o arredondamento já
        # aconteceu, mas os limites de mínimo/máximo do símbolo continuam
        # valendo, e é aqui que eles são conferidos.
        volume = _normalizar_volume(info, volume)

        # O que ficou DE FATO em risco, contra o preço que vai ser pago.
        risco_efetivo = volume * abs(preco - stop) if stop is not None else None

        pedido = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if comprar else mt5.ORDER_TYPE_SELL,
            "price": float(preco),
            "deviation": DESVIO_MAXIMO_POINTS,
            "type_time": mt5.ORDER_TIME_DAY,
            "type_filling": _modo_preenchimento(mt5, info),
            # 20 caracteres é o limite do campo no servidor; cortar aqui evita
            # rejeição por um comentário comprido demais.
            "comment": (comentario or "acoes")[:20],
        }
        if stop:
            pedido["sl"] = float(stop)
        if alvo:
            pedido["tp"] = float(alvo)

        checagem = mt5.order_check(pedido)
        if checagem is None:
            raise OrdemRecusada(f"order_check não respondeu ({mt5.last_error()}).")
        if checagem.retcode != 0:
            raise OrdemRecusada(
                f"A corretora recusou a ordem na validação: {checagem.comment} "
                f"(retcode {checagem.retcode})."
            )

        base = {
            "symbol": symbol, "direcao": direcao, "volume": volume,
            "preco_pedido": preco, "stop": stop, "alvo": alvo,
            "conta": conta.login, "servidor": conta.server, "tipo_conta": tipo,
            "margem_necessaria": getattr(checagem, "margin", None),
            # Quanto ficou DE FATO em risco, já com o lote arredondado. Volta
            # no payload pra quem chama poder registrar e comparar com o que
            # pediu — é a mesma conta que `GET /ordens` refaz depois com o
            # preço executado.
            "risco_maximo": risco_maximo,
            "risco_efetivo": risco_efetivo,
        }
        if simular:
            return {**base, "simulado": True, "enviado": False,
                    "comentario": checagem.comment}

        resultado = mt5.order_send(pedido)
        if resultado is None:
            raise OrdemRecusada(f"order_send não respondeu ({mt5.last_error()}).")

        ok = resultado.retcode == mt5.TRADE_RETCODE_DONE
        return {
            **base,
            "simulado": False,
            "enviado": ok,
            "ticket": resultado.order or None,
            "preco_executado": resultado.price or None,
            "volume_executado": resultado.volume or None,
            "retcode": resultado.retcode,
            "comentario": resultado.comment,
        }
    finally:
        mt5.shutdown()


# ==========================================================================
# Proteção do stop de uma posição já aberta (2026-08-14)
#
# Até aqui a única saída que o sistema tinha era o par `sl`/`tp` que sai
# grudado na ordem de abertura: ou o preço batia num, ou no outro, ou a
# posição ficava. A ITUB4 aberta em 12/08 mostrou o buraco — dois dias no ar,
# +1,86R não realizados, alvo a 0,43 de distância que o preço nunca alcançou,
# e nada em lugar nenhum capaz de transformar aquele lucro em dinheiro. Um
# recuo normal devolveria os +1,86R e ainda cobraria -1,00R.
#
# A regra é uma só, e a mesma nas duas pontas: passando de `gatilho_r` de
# avanço, o stop passa a andar a `distancia_r` atrás do preço. Com 0,8/0,8 a
# primeira mexida cai exatamente no zero a zero, e daí em diante é trailing.
#
# O que torna isto seguro de rodar até com o ENVIO desligado é o invariante:
# **o stop só aperta**. Nunca afrouxa, nunca se afasta do preço, nunca
# aumenta exposição. Por isso `mover_stop` compara contra o `sl` que está NA
# CORRETORA no instante da escrita, e não contra o banco: se alguém mexeu à
# mão, a mão vence.
# ==========================================================================


def _arredondar_stop(bruto: float, comprar: bool, digitos: int) -> float:
    """Arredonda o stop PARA LONGE do preço — piso na compra, teto na venda.

    O inverso de `_alvo_por_rr`, e pela razão simétrica: lá o alvo não podia
    encurtar, aqui o stop não pode apertar mais do que a conta pediu. Em papel
    de R$ 4 o tique de um centavo é uma fatia grande da distância.

    A folga de 1e-9 é contra a representação binária, e é o que torna esta
    função IDEMPOTENTE: o mesmo valor passa por ela duas vezes (a política
    calcula, e `mover_stop` reconfere depois de afastar pelo `stops_level`), e
    39,41 vale 3940,9999... vezes cem — sem a folga, o piso comeria um centavo
    a cada passada."""
    fator = 10 ** digitos
    escala = bruto * fator
    if comprar:
        return math.floor(escala + 1e-9) / fator
    return math.ceil(escala - 1e-9) / fator


def stop_protegido(direcao: str, preco_executado: float, preco_atual: float,
                   risco: float, *, gatilho_r: float, distancia_r: float,
                   digitos: int = 2) -> float | None:
    """Onde o stop DEVERIA estar, dado o quanto o preço já andou a favor.

    Função pura, sem MT5: é a política, e quem a aplica é o executor. `risco`
    é o R ORIGINAL da ordem (|preço executado − stop do envio|), imutável —
    medir contra o stop já movido faria a proteção se perseguir, encolhendo o
    R a cada passada até fechar a posição na primeira oscilação.

    Devolve None enquanto o avanço não chega ao gatilho, que é o caso comum:
    quem chama não faz nada e a posição segue com o stop do envio."""
    if risco <= 0 or gatilho_r <= 0 or distancia_r <= 0:
        return None
    comprar = direcao == "COMPRA"
    avanco = (preco_atual - preco_executado) if comprar else (preco_executado - preco_atual)
    if avanco < gatilho_r * risco:
        return None
    bruto = (preco_atual - distancia_r * risco) if comprar else (preco_atual + distancia_r * risco)
    return _arredondar_stop(bruto, comprar, digitos)


def mover_stop(mt5, ticket: int, novo_stop: float, *,
               simular: bool = False) -> dict | None:
    """Aperta o stop de uma posição aberta. NÃO abre, não fecha, não aumenta.

    NÃO conecta nem desconecta — mesma convenção de `consultar_posicao`, e
    pelo mesmo motivo: quem chama percorre um lote.

    Devolve None para "não havia o que fazer" (posição já fechou, ou o stop
    proposto não é melhor que o que está lá). Isso é o caso COMUM, chamada a
    cada passada da reconciliação, e por isso não é exceção: só é
    `OrdemRecusada` o que denuncia configuração ou estado errado.

    ⚠️ `TRADE_ACTION_SLTP` grava OS DOIS níveis da posição. Mandar só `sl`
    apaga o alvo — e o alvo é hoje a única saída que de fato funciona. Por
    isso o `tp` vivo é relido e reenviado igual, e é o que a conferência pós
    deploy tem que olhar primeiro."""
    conta = mt5.account_info()
    if conta is None:
        raise OrdemRecusada(f"Sem informação da conta ({mt5.last_error()}).")
    _conferir_tipo_conta(conta)

    # Releitura logo antes de escrever, e não o estado que o chamador já tem:
    # entre a leitura do lote e esta linha o preço andou, e o `sl` pode até ter
    # sido movido à mão. Quem manda é o que está na corretora agora.
    posicoes = mt5.positions_get(ticket=ticket)
    if not posicoes:
        return None
    p = posicoes[0]
    comprar = p.type == mt5.POSITION_TYPE_BUY
    preco = float(p.price_current)

    info = mt5.symbol_info(p.symbol)
    if info is None:
        raise OrdemRecusada(f"Sem informação do símbolo {p.symbol} ({mt5.last_error()}).")

    # Distância mínima que o servidor aceita entre o stop e o preço. Abaixo
    # dela a resposta é "Invalid stops" (10016) — recusa que não diz nada a
    # quem lê o log dois dias depois. Afasta em vez de tentar e falhar.
    minimo = float(getattr(info, "trade_stops_level", 0) or 0) * float(info.point or 0.0)
    if minimo:
        limite = preco - minimo if comprar else preco + minimo
        novo_stop = min(novo_stop, limite) if comprar else max(novo_stop, limite)
    novo_stop = _arredondar_stop(novo_stop, comprar, getattr(info, "digits", 2) or 2)

    # Só aperta. `sl == 0` é posição SEM stop na corretora (não um stop no
    # zero), e aí qualquer valor do lado certo é melhora.
    anterior = float(p.sl or 0.0)
    if anterior:
        melhora = novo_stop > anterior if comprar else novo_stop < anterior
        if not melhora:
            return None

    # Do lado certo do preço de agora. Reaproveita a trava do envio: um stop
    # do lado errado fecharia a posição a mercado na hora.
    _conferir_coerencia("COMPRA" if comprar else "VENDA", preco, novo_stop, None)

    pedido = {
        "action": mt5.TRADE_ACTION_SLTP,
        "position": int(ticket),
        "symbol": p.symbol,
        "sl": float(novo_stop),
        # O alvo vivo, reenviado igual. Ver o aviso da docstring.
        "tp": float(p.tp or 0.0),
    }
    base = {
        "symbol": p.symbol, "ticket": int(ticket),
        "stop_anterior": anterior or None, "stop_novo": novo_stop,
        "alvo": float(p.tp or 0.0) or None, "preco_atual": preco,
    }

    if simular:
        # `order_check` só no ensaio: no caminho valendo ele seria uma ida a
        # mais ao servidor para validar margem, que numa mudança de stop não
        # muda. Aqui vale, porque é o único jeito de conferir sem escrever.
        checagem = mt5.order_check(pedido)
        return {**base, "simulado": True, "enviado": False,
                "retcode": getattr(checagem, "retcode", None),
                "comentario": getattr(checagem, "comment", None)}

    resultado = mt5.order_send(pedido)
    if resultado is None:
        raise OrdemRecusada(f"order_send (SLTP) não respondeu ({mt5.last_error()}).")
    return {
        **base, "simulado": False,
        "enviado": resultado.retcode == mt5.TRADE_RETCODE_DONE,
        "retcode": resultado.retcode, "comentario": resultado.comment,
    }


# ==========================================================================
# Leitura do desfecho
#
# Daqui pra baixo nada envia ordem: é a reconciliação, que pergunta à
# corretora o que aconteceu com uma posição já aberta. Mora neste arquivo, e
# não no `executor/`, porque as convenções (e as armadilhas) da API de
# trading do MT5 são as mesmas — a de fuso logo abaixo é a mesma que já
# obrigou `_fetch_ohlcv_mt5` a reinterpretar o epoch.
#
# Por que ler da corretora em vez de derivar de `signals.resultado`: aquilo
# é o desfecho do SINAL, com entrada modelada na abertura da vela seguinte.
# A ordem executou noutro preço, com quantidade arredondada ao lote, e pode
# ter sido fechada à mão, em partes, ou com deslize. Só o extrato sabe.
# ==========================================================================

# `DEAL_REASON_*` → o vocabulário que a gente guarda. Ao contrário dos
# `SYMBOL_FILLING_*` lá em cima, estes o pacote Python EXPORTA (conferido por
# `dir(MetaTrader5)` na VM em 2026-08-12), então aqui não há literal solto.
#
# Separar MANUAL é o ponto da tabela: uma regra cujo resultado veio de
# fechamento à mão não foi medida, foi pilotada — e misturar as duas coisas
# corrompe a comparação entre regras, que é justamente o que se quer medir.
def _motivo_da_saida(mt5, razao: int) -> str:
    return {
        mt5.DEAL_REASON_SL: "STOP",
        mt5.DEAL_REASON_TP: "ALVO",
        mt5.DEAL_REASON_CLIENT: "MANUAL",
        mt5.DEAL_REASON_MOBILE: "MANUAL",
        mt5.DEAL_REASON_WEB: "MANUAL",
        mt5.DEAL_REASON_EXPERT: "EXPERT",
        mt5.DEAL_REASON_SO: "MARGEM",
    }.get(razao, "OUTRO")


def hora_do_mt5(epoch: int) -> datetime:
    """Converte um horário do MT5 (deal, ordem, tick) para UTC de verdade.

    O terminal devolve o horário no relógio LOCAL DO SERVIDOR codificado como
    se fosse epoch UTC — para a Clear, Brasília. Ler direto como UTC produz
    um deslocamento de 3 horas que não quebra nada visivelmente: as datas
    continuam plausíveis, só apontam para a hora errada, e todo relatório por
    dia sai torto em silêncio. Mesma conversão de `_fetch_ohlcv_mt5`, e pela
    mesma razão.

    Medido em 2026-08-12 no terminal da Clear: as três últimas velas M15 de
    VALE3 do pregão anterior leem como 16:15 / 16:30 / 16:45 se
    interpretadas como UTC — que seriam 13:15-13:45 de Brasília, muito antes
    do fechamento. Como relógio do servidor, batem com o fim do pregão.

    Quem chama confere o resultado contra o `criado_em` da ordem (ver
    `executor._conciliar`): se a convenção mudar, isso aparece como aviso em
    vez de virar dado errado."""
    return datetime.fromtimestamp(epoch, UTC).replace(
        tzinfo=ZoneInfo(LOCAL_TZ)).astimezone(UTC)


def consultar_posicao(mt5, ticket: int) -> dict | None:
    """Estado atual de uma posição, pelo ticket que `enviar_ordem_mercado`
    devolveu. `None` quando a corretora não conhece esse ticket.

    NÃO conecta nem desconecta: quem chama é dono da conexão, porque a
    reconciliação percorre um lote e abrir/fechar o terminal por linha custa
    mais que a consulta inteira.

    O ticket da ordem serve como `position_id` — para execução a mercado, a
    posição herda o número da ordem que a abriu (conferido na VM em
    2026-08-12 com a posição 2500144303).
    """
    abertas = mt5.positions_get(ticket=ticket)
    if abertas:
        p = abertas[0]
        # `profit` é o NÃO REALIZADO. Vai pro banco assim mesmo, com
        # `fechado_em` nulo — é o que permite a tela dizer "3 abertas, +R$ 48
        # no papel" sem somar isso ao que já é dinheiro.
        return {
            "aberta": True,
            "resultado_reais": float(p.profit),
            "fechado_em": None,
            "preco_saida": None,
            "volume_saida": None,
            "motivo_saida": None,
            "abertura_em": hora_do_mt5(p.time),
            # Os quatro abaixo existem só enquanto a posição está viva, e são
            # o que a proteção de stop precisa: onde ela abriu, quanto vale
            # agora, e onde estão os dois níveis NA CORRETORA (que podem ter
            # sido movidos à mão desde o envio). `sl`/`tp` em zero significam
            # "não tem", não "está no zero" — daí o `or None`.
            "sl": float(p.sl) or None,
            "tp": float(p.tp) or None,
            "preco_abertura": float(p.price_open),
            "preco_atual": float(p.price_current),
        }

    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        # Nem posição aberta nem histórico: ticket desconhecido, ou o
        # histórico ainda não sincronizou. Devolver None e tentar de novo no
        # próximo ciclo é melhor que gravar "fechada com resultado zero".
        return None

    saidas = [d for d in deals
              if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
    if not saidas:
        return None  # só a entrada registrada: a posição está abrindo

    # Soma TODOS os deals, não só os de saída: o deal de entrada tem
    # `profit=0` mas carrega a corretagem da entrada. Contar só a saída
    # reporta um lucro que não existiu.
    resultado = sum(
        float(d.profit) + float(getattr(d, "commission", 0.0) or 0.0)
        + float(getattr(d, "swap", 0.0) or 0.0)
        + float(getattr(d, "fee", 0.0) or 0.0)
        for d in deals
    )
    volume_saida = sum(float(d.volume) for d in saidas)
    # Preço médio ponderado: fechamento parcial gera mais de um deal de
    # saída, e a média simples mentiria proporcionalmente ao desequilíbrio.
    preco_saida = (
        sum(float(d.price) * float(d.volume) for d in saidas) / volume_saida
        if volume_saida else None
    )
    ultima = max(saidas, key=lambda d: d.time)
    entrada = next((d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None)

    return {
        "aberta": False,
        "resultado_reais": resultado,
        "fechado_em": hora_do_mt5(ultima.time),
        "preco_saida": preco_saida,
        "volume_saida": volume_saida,
        "motivo_saida": _motivo_da_saida(mt5, ultima.reason),
        "abertura_em": hora_do_mt5(entrada.time) if entrada else None,
        # Mesma forma nos dois ramos, para quem consome não ter que adivinhar
        # quais chaves existem. Posição fechada não tem mais níveis vivos.
        "sl": None, "tp": None, "preco_abertura": None, "preco_atual": None,
    }
