"""Modelos Pydantic compartilhados pelos dois entrypoints do backend
(`processor.py`, de escrita, e `api.py`, de leitura)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class CandleIn(BaseModel):
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandleBatchIn(BaseModel):
    symbol: str
    timeframe: str
    source: str = "MetaTrader 5"
    candles: list[CandleIn]


class IngestResult(BaseModel):
    symbol: str
    timeframe: str
    upserted: int


class CandleOut(BaseModel):
    """Uma vela na resposta de GET /candles. Os nomes de campo batem com as
    colunas que `daytrade_smc._fetch_ohlcv_api` monta no DataFrame."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class CandlesResponse(BaseModel):
    symbol: str
    timeframe: str
    candles: list[CandleOut]


class WatchlistResponse(BaseModel):
    symbols: list[str]


class WatchlistAdd(BaseModel):
    symbol: str


class WatchlistReplace(BaseModel):
    """Corpo do PUT /watchlist: a lista COMPLETA de símbolos ativos.

    Existe porque `daytrade_smc.save_symbols` tem semântica de sobrescrever
    tudo de uma vez — um POST por símbolo somado a um DELETE por símbolo
    removido não expressa isso atomicamente."""

    symbols: list[str]


class SymbolStatus(BaseModel):
    symbol: str
    timeframe: str
    last_candle_time: datetime
    last_ingested_at: datetime


class ProfileIn(BaseModel):
    """Corpo do PUT /profiles/{nome}.

    `params` carrega SÓ os campos diferentes do default do motor — é o que
    faz um perfil salvo hoje continuar carregando depois de um parâmetro
    novo entrar em `AnalysisParams`. Um `{}` significa "todos os defaults"."""

    params: dict = {}
    params_hash: str = ""
    descricao: str = ""


class ProfileAtivoIn(BaseModel):
    """Corpo do PUT /profiles/{nome}/ativo.

    `ativo: false` faz o analyzer pular esse perfil na próxima varredura
    (nenhum sinal novo dele é gravado); `true` o reativa na varredura
    seguinte. Só a flag muda — `params`, `descricao` e o histórico de sinais
    ficam intactos."""

    ativo: bool


class ProfileOut(BaseModel):
    nome: str
    params: dict
    params_hash: str
    descricao: str
    ativo: bool
    criado_em: datetime
    alterado_em: datetime


class ProfilesResponse(BaseModel):
    profiles: list[ProfileOut]


class SignalIn(BaseModel):
    """Um sinal gerado, pronto pra gravar.

    O corpo é montado por `daytrade_smc.signal_payload`, e SÓ por ela: o
    worker e o botão "salvar sinal" do Streamlit usam a mesma função de
    propósito, senão as duas origens gravariam linhas com formatos que
    divergiriam na primeira mudança do motor."""

    symbol: str
    timeframe: str
    modalidade: str
    candle_time: datetime      # UTC, abertura da vela FECHADA analisada
    perfil: str
    params_hash: str
    origem: str                # 'worker' | 'manual' | 'backfill'
    direcao: str
    score: float
    confianca: float
    setup: str
    mtf_confirmado: bool = False
    mtf_direcao: str = "NEUTRO"
    entrada: float | None = None
    stop: float | None = None
    alvo_1: float | None = None
    alvo_2: float | None = None
    r_alvo_1: float | None = None
    r_alvo_2: float | None = None
    stop_basis: str = ""
    detalhes: dict = {}


class SignalOut(SignalIn):
    id: int
    criado_em: datetime
    resultado: str | None = None
    resultado_detalhe: str | None = None
    candles_ate_resultado: int | None = None
    avaliado_em: datetime | None = None
    # A decisão MAIS RECENTE do operador sobre este sinal (ACOMPANHAR,
    # OPERAR, IGNORAR, OPEREI, CANCELEI), ou None quando ninguém decidiu
    # nada ainda. Vem de `signal_feedback`, que guarda o histórico completo
    # — aqui só a última, porque é ela que descreve o estado atual.
    #
    # Nasceu porque a tela de acompanhamento buscava sinais e feedbacks em
    # duas chamadas e cruzava as duas listas no navegador: além de não dar
    # pra FILTRAR por decisão no servidor, o cruzamento só enxergava o que
    # coubesse nos dois limites de paginação ao mesmo tempo.
    feedback: str | None = None
    feedback_origem: str | None = None


class SignalSaveResult(BaseModel):
    """`duplicado` distingue "gravei agora" de "já estava lá".

    Sem isso a interface diria "salvo!" nas duas situações, e o usuário não
    teria como saber se o clique anterior tinha funcionado."""

    signal: SignalOut
    duplicado: bool


class SignalsResponse(BaseModel):
    signals: list[SignalOut]
    total: int


class StatsRow(BaseModel):
    """Uma linha de assertividade.

    ATENÇÃO ao denominador: `n` conta todos os sinais do recorte, mas
    `taxa_acerto` e `expectativa_r` só olham os RESOLVIDOS (os `EM_ABERTO`
    ainda não têm desfecho). Mostrar "66,7% de n" seria mentira — a
    interface tem que exibir `resolvidos` junto."""

    modalidade: str
    recorte: str | None = None
    n: int
    resolvidos: int
    acertos: int
    em_aberto: int
    taxa_acerto: float | None = None
    expectativa_r: float | None = None
    alvo_1: int = 0
    alvo_2: int = 0
    stop: int = 0
    # Quantos dos `resolvidos` já foram medidos pelo modelo de execução atual
    # (fill na abertura seguinte, R líquido de custo). Abaixo de `resolvidos`,
    # a expectativa está misturando dois modelos e o número infla nos gaps —
    # o conserto é `analyzer.py --reavaliar-tudo`.
    modelo_atual: int = 0


class AnaliseIn(BaseModel):
    """Corpo do POST /analisar — rodar o motor AGORA, sob demanda.

    Todos os campos além de `symbol` são opcionais e caem nos mesmos
    defaults do worker, pra que uma consulta avulsa e a varredura periódica
    descrevam a mesma coisa quando ninguém pede nada diferente."""

    symbol: str
    timeframes: list[str] | None = None   # default: os quatro varridos pelo worker
    perfil: str | None = None             # default: 'padrão'
    modalidade: str | None = None         # default: todas as seis


class AnaliseResponse(BaseModel):
    """Resultado de uma análise sob demanda.

    `leituras` reusa `SignalIn` de propósito: é exatamente o mesmo shape que
    o worker grava, montado pela mesma `daytrade_smc.signal_payload`. Assim
    "o que o motor está vendo agora" e "o que o motor viu naquela vela" são
    comparáveis campo a campo, sem tradução no meio.

    Ao contrário da varredura do worker, aqui as leituras NEUTRO vêm junto:
    o worker não as grava porque elas não entram em estatística nenhuma, mas
    numa consulta "e a PETR4?" a resposta "está neutro nos quatro
    timeframes" é o dado que se foi buscar.

    `erros` traz, por timeframe, o motivo de não ter dado pra analisar
    (símbolo sem candle, só vela em formação). Um timeframe sem dado não
    derruba os outros — mas some da resposta em silêncio se não for dito.
    """

    symbol: str
    perfil: str
    confirmacao: list[str]        # o par de timeframes que define "confirmado"
    analisado_em: datetime
    leituras: list[SignalIn]
    erros: dict[str, str] = {}


class StatsResponse(BaseModel):
    filtros: dict
    total: int
    geral: list[StatsRow]
    por_timeframe: list[StatsRow]
    por_symbol: list[StatsRow]
    por_direcao: list[StatsRow]
    por_faixa_score: list[StatsRow]
    # Recorte por RVOL, lido do JSONB `detalhes->>'rvol'`. As faixas são as do
    # harness de 2026-08-12, que mediu o gate de volume atual (1,3-2,0×) como
    # a pior faixa de todas — é o recorte que responde "devo confiar mais num
    # sinal de volume baixo?" em produção, sem tocar no motor.
    por_rvol: list[StatsRow]
    por_mtf: list[StatsRow]
    # Recorte pela decisão do operador. É o que responde "acertei mais no
    # que eu escolhi operar do que na média?" — a única pergunta que
    # justifica coletar o feedback. `recorte='PENDENTE'` agrupa os sinais
    # sobre os quais ninguém decidiu nada.
    por_feedback: list[StatsRow]

class FeedbackIn(BaseModel):
    """Feedback do usuário sobre um sinal."""
    acao: str  # ACOMPANHAR | OPERAR | IGNORAR | OPEREI | CANCELEI
    origem: str = "web"  # web | whatsapp | telegram
    nota: str | None = None


class FeedbackOut(BaseModel):
    id: int
    signal_id: int
    acao: str
    origem: str
    nota: str | None
    criado_em: datetime


class FeedbackResponse(BaseModel):
    feedbacks: list[FeedbackOut]
    total: int
    por_acao: dict[str, int]  # {"ACOMPANHAR": 5, "IGNORAR": 3, ...}


class WebhookPayload(BaseModel):
    """Payload genérico de webhook (WhatsApp/Telegram)."""
    signal_id: int
    acao: str
    origem: str  # whatsapp | telegram
    nota: str | None = None
    user_id: str | None = None  # id do usuário no messenger


class AutoAcompanhamentoIn(BaseModel):
    """Regra de auto-acompanhamento: perfil + modalidade → ACOMPANHAR automático."""
    perfil: str
    modalidade: str
    ativo: bool = True


class AutoAcompanhamentoOut(BaseModel):
    perfil: str
    modalidade: str
    ativo: bool
    criado_em: datetime


class AutoAcompanhamentoResponse(BaseModel):
    regras: list[AutoAcompanhamentoOut]


class AutoOrdemIn(BaseModel):
    """Regra de ordem automática: perfil + modalidade + timeframe → ordem.

    `timeframe` faz parte da chave, e não é rigor à toa: sem ele a regra
    casaria com o mesmo sinal em M15, H1, H4 e D1 e abriria quatro posições
    no mesmo ativo achando que abriu uma.

    `risco_maximo` é em REAIS. A quantidade sai da distância até o stop do
    próprio sinal, então toda operação arrisca o mesmo valor independente da
    volatilidade do papel — que é o oposto do que uma quantidade fixa faz.
    """
    perfil: str
    modalidade: str
    timeframe: str
    risco_maximo: float
    ativo: bool = True


class AutoOrdemOut(AutoOrdemIn):
    criado_em: datetime


class AutoOrdemResponse(BaseModel):
    regras: list[AutoOrdemOut]


class OrdemIn(BaseModel):
    """Reserva de ordem: gravada ANTES de a ordem sair para a corretora.

    O estado inicial é `ENVIANDO` de propósito. Gravar depois do envio
    deixaria uma janela em que a ordem existe na corretora e não no banco, e
    um reinício ali dentro mandaria a segunda ordem para o mesmo sinal. Com
    a reserva antes, o pior caso é uma linha `ENVIANDO` órfã — visível — em
    vez de posição dobrada, que só aparece no extrato."""
    signal_id: int
    symbol: str
    direcao: str
    risco_maximo: float | None = None
    stop: float | None = None
    alvo: float | None = None
    # Ordem de validação: sai de verdade, mas fica fora das estatísticas. Ver
    # o comentário da coluna em schema.sql.
    teste: bool = False


class OrdemResultado(BaseModel):
    """O que a corretora respondeu, gravado depois do envio."""
    status: str  # ENVIADA | FALHOU | RECUSADA
    volume: float | None = None
    preco_pedido: float | None = None
    preco_executado: float | None = None
    conta: int | None = None
    servidor: str | None = None
    tipo_conta: str | None = None
    ticket: int | None = None
    retcode: int | None = None
    mensagem: str | None = None
    # O alvo que REALMENTE saiu. A reserva grava o `alvo_1` do sinal, mas o
    # envio o recoloca à razão contratada sobre o risco real (ver
    # `execucao._alvo_por_rr`); sem regravar aqui, a coluna guardaria um
    # nível que a corretora nunca recebeu.
    alvo: float | None = None
    # Desvio do preenchimento contra a entrada modelada, em R. Ver o
    # comentário da coluna em schema.sql.
    desvio_entrada_r: float | None = None


class OrdemFechamento(BaseModel):
    """O desfecho da posição, lido do MetaTrader 5 pela reconciliação.

    ⚠️ `resultado_reais` só é FINAL quando vem com `fechado_em`. Enquanto a
    posição está aberta a reconciliação manda o não realizado, com
    `fechado_em=None`, e a linha é reescrita a cada passada."""
    resultado_reais: float | None = None
    fechado_em: datetime | None = None
    preco_saida: float | None = None
    volume_saida: float | None = None
    motivo_saida: str | None = None   # STOP | ALVO | MANUAL | EXPERT | MARGEM | OUTRO


class OrdemOut(BaseModel):
    """Uma ordem enviada, já com a regra que a produziu e o que ela rendeu.

    Os campos de `perfil` a `r_realizado` vêm do SINAL (join), não da tabela
    de ordens: sem eles não dá pra dizer qual das regras de `auto_ordem`
    produziu esta ordem, e "qual regra está dando dinheiro?" fica sem
    resposta do lado do cliente.

    `resultado` (do sinal, calculado sobre candles com entrada modelada) e
    `resultado_reais` (o que a corretora pagou) são coisas diferentes de
    propósito: comparar os dois é o que mostra o quanto o motor promete a
    mais do que a execução entrega."""

    id: int
    signal_id: int
    symbol: str
    direcao: str
    volume: float | None = None
    risco_maximo: float | None = None
    preco_pedido: float | None = None
    preco_executado: float | None = None
    stop: float | None = None
    alvo: float | None = None
    conta: int | None = None
    servidor: str | None = None
    tipo_conta: str | None = None
    ticket: int | None = None
    status: str
    retcode: int | None = None
    mensagem: str | None = None
    criado_em: datetime
    enviado_em: datetime | None = None
    # Ordem de validação — não entra em estatística. Uma interface que liste
    # estas linhas junto das reais TEM que marcá-las, senão o rótulo só troca
    # de lugar o problema que ele existe pra resolver.
    teste: bool = False
    # Desfecho na corretora
    fechado_em: datetime | None = None
    preco_saida: float | None = None
    volume_saida: float | None = None
    resultado_reais: float | None = None
    motivo_saida: str | None = None
    conciliado_em: datetime | None = None
    # Quanto o preenchimento andou contra a entrada modelada, em R planejado
    # (positivo = preencheu pior). Ver o comentário da coluna em schema.sql:
    # é a variável que explicou o prejuízo das 39 primeiras ordens.
    desvio_entrada_r: float | None = None
    # A regra que produziu esta ordem, e o que o motor previu
    perfil: str | None = None
    modalidade: str | None = None
    timeframe: str | None = None
    candle_time: datetime | None = None
    score: float | None = None
    resultado: str | None = None       # desfecho do SINAL, não da ordem
    r_realizado: float | None = None   # idem, em múltiplos de R
    # Derivados, calculados na resposta e não guardados:
    # `risco_efetivo` é o dinheiro que ficou DE FATO em risco depois do
    # arredondamento ao lote — ele diverge do `risco_maximo` configurado, e
    # essa diferença é exatamente o que se quer enxergar. `resultado_r` põe
    # ordens de ativos diferentes na mesma escala, e na mesma escala do
    # `r_realizado` do sinal.
    risco_efetivo: float | None = None
    resultado_r: float | None = None


class OrdemStatsRow(BaseModel):
    """Uma linha de desempenho de ordens.

    ATENÇÃO ao denominador, pelo mesmo motivo do `StatsRow`: `n` conta as
    ordens ENVIADAS do recorte, mas `taxa_acerto` sai de `ganhos + perdas` e
    `resultado_reais` soma só as FECHADAS. As abertas ainda podem virar
    prejuízo — por isso o não realizado vive em `aberto_reais`, campo
    separado, e a interface tem que exibir o denominador junto da taxa.

    `fechadas = ganhos + perdas + zeradas`; as zeradas ficam fora da taxa
    porque um resultado exatamente zero não é acerto nem erro. Na prática
    são raras — a corretagem quase sempre desempata."""

    recorte: str
    n: int
    fechadas: int
    abertas: int
    ganhos: int
    perdas: int
    zeradas: int
    taxa_acerto: float | None = None
    resultado_reais: float = 0.0
    resultado_r_medio: float | None = None
    aberto_reais: float = 0.0


class OrdemStatsResponse(BaseModel):
    """Desempenho das ordens, por recorte.

    `recusadas` e `falhadas` ficam FORA das taxas — nada delas chegou ao
    mercado, então não têm desempenho — mas vêm no topo porque "40 ordens, 12
    barradas pelas travas locais" é fato operacional, não ruído: é a
    diferença entre uma regra ruim e uma configuração errada."""

    filtros: dict
    total: int
    recusadas: int
    falhadas: int
    geral: list[OrdemStatsRow]
    por_conta: list[OrdemStatsRow]
    por_regra: list[OrdemStatsRow]
    por_symbol: list[OrdemStatsRow]
    por_motivo_saida: list[OrdemStatsRow]
    por_direcao: list[OrdemStatsRow]
    # Recortes da EXECUÇÃO, não da estratégia: respondem "a ordem saiu com a
    # geometria que a regra pediu?" em vez de "a regra presta?". Existem
    # porque a primeira vez que essa pergunta foi feita — à mão, cruzando
    # `ordens` com `signals` — ela encontrou 40% do prejuízo concentrado numa
    # única faixa de R/R.
    por_rr_envio: list[OrdemStatsRow] = []
    por_desvio_entrada: list[OrdemStatsRow] = []
    # Recortes TEMPORAIS, agrupados pelo dia/hora do ENVIO no fuso de
    # Brasília. `por_dia` vem em ordem cronológica e é a série de onde sai a
    # curva de capital: a soma corrida de `resultado_reais`. Nenhum outro
    # recorte responde "estou ganhando ou perdendo ao longo do tempo?" —
    # duas taxas de acerto idênticas podem ser um platô ou uma escada
    # descendo.
    por_dia: list[OrdemStatsRow] = []
    por_hora: list[OrdemStatsRow] = []
    por_dia_semana: list[OrdemStatsRow] = []


class OrdemReserva(BaseModel):
    """`duplicado=True` diz "outro processo já reservou este sinal" — é o
    sinal de PARE do executor, não um erro."""
    ordem: OrdemOut
    duplicado: bool


class OrdensResponse(BaseModel):
    ordens: list[OrdemOut]
    total: int


class OrdensLimpeza(BaseModel):
    """O que o reset de desenvolvimento apagou — ver `DELETE /ordens`.

    `abertas_preservadas` é o número que importa conferir: são as posições
    que continuam vivas na corretora e cuja linha ficou de pé pra
    conciliação ainda achar."""
    apagadas: int
    abertas_preservadas: int
    restantes: int
