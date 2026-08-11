# Day Trade SMC — análise técnica e histórico de sinais

Ferramenta de leitura técnica para ações brasileiras. Combina cinco
abordagens — **Smart Money Concepts**, **Price Action**, **Médias Móveis**,
**VWAP** e **IFR** — em seis leituras por ativo (as cinco isoladas mais a
**Confluência**, que as combina), com plano de risco (entrada, stop, alvos e
RR) em cada uma.

Cada sinal gerado é gravado e tem o desfecho conferido contra os candles que
vieram depois. Isso é o que separa esta ferramenta de um indicador: dá pra
perguntar **quanto** cada leitura acerta, e não só o que ela está dizendo
agora.

**Não envia ordens.** Serve para viés e estrutura — tendência, níveis, força
relativa entre ativos. Confirme preço e liquidez na sua corretora antes de
operar.

## Como rodar

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py          # a interface — é por aqui que se usa
```

Relatório de um ativo no terminal, útil pra conferir o motor sem subir a web:

```bash
python daytrade_smc.py VALE3
python daytrade_smc.py VALE3 --timeframe M15 --count 250 --risco 500
```

## As duas fontes de dados

Seletor **Fonte de dados** na barra lateral.

| Fonte | Atraso | Precisa de quê |
|---|---|---|
| **Homelab (API)** | poucos segundos | o pipeline do homelab no ar |
| **Yahoo Finance** | ~15-20 min | nada |

**Homelab (API)** é o caminho recomendado. Um scraper roda continuamente numa
VM Windows, ao lado de um terminal MetaTrader 5 logado, e alimenta um
TimescaleDB; a interface lê esse banco por HTTP. Dado real de corretora, sem
DLL na máquina de quem acessa e sem credencial de banco fora do backend.
Configure `ACOES_API_URL` e `ACOES_API_KEY` (em `st.secrets` ou variável de
ambiente) — com elas presentes, esta vira a fonte padrão.

**Yahoo Finance** funciona em qualquer lugar sem depender de nada seu estar
no ar, e é o fallback quando a API não está configurada. O atraso de 15-20
minutos é do próprio Yahoo. Candles de H4 não existem lá: são sintetizados
agregando H1, ancorados na meia-noite de Brasília.

O aviso no topo da tela acompanha a fonte selecionada — ele diz qual atraso
você está realmente olhando.

## Os cinco modos

### Análise individual

Gráfico de candles (Plotly) com EMAs, VWAP, swings, marcações de BOS/CHoCH e
a zona de FVG ainda não preenchida, mais os painéis das seis leituras. Pode
auto-atualizar a cada 30s–5min, recarregando só o painel em vez da página
inteira.

### Scanner

Roda a análise em todos os ativos da watchlist de uma vez e ranqueia por
**Score Geral**, confirmados primeiro. Uma coluna **Posição** numera o
ranking, e dá pra abrir a análise completa de qualquer ativo direto dali.

### Verificação retroativa

"Esse sinal teria dado certo?" — escolhe um ativo, um timeframe e uma data
passada; a análise roda usando **só os dados que existiam até aquele
fechamento**, sem espiar o futuro, e depois confere o que aconteceu de
verdade nos candles seguintes: bateu o Alvo 1, o Alvo 2, o Stop, ou segue em
aberto.

É uma checagem pontual, não um backtest em massa. O histórico intraday do
Yahoo (M15/H1/H4) cobre só ~60 dias; pra datas mais antigas, use Diário ou
Semanal.

### Assertividade

Taxa de acerto **por tipo de análise**, recortada por timeframe, ativo,
direção, faixa de score e se o sinal estava confirmado no multi-timeframe.
Além do acerto, a **expectativa em R** — porque uma leitura pode acertar
pouco e ainda ser lucrativa, ou o contrário.

De onde vêm os sinais:

- **Worker `analyzer`**, no homelab, varrendo a watchlist a cada vela. É ele
  que torna isso uma medição: sem o worker só existiriam os sinais que você
  por acaso olhou, e a taxa sairia enviesada pelo seu uso da tela.
- **Backfill** (`analyzer.py --backfill`), reconstruindo sinais das velas já
  guardadas.
- **Botão "💾 Salvar sinal"** em qualquer painel, marcado com origem
  `manual`. Clicar duas vezes não cria duas linhas.

**Cuidado ao ler os números:** a taxa de acerto e a expectativa contam só os
sinais **já resolvidos**. Os em aberto aparecem no contador mas ficam fora da
conta até bater alvo ou stop — por isso a tabela sempre mostra "sinais" e
"resolvidos" em colunas separadas.

Este modo depende de `ACOES_API_URL`: é onde o histórico mora. Sem ela, o
modo explica isso em vez de mostrar número errado.

### Mini Índice (WINFUT)

Modo dedicado ao contrato futuro do mini índice, separado da watchlist de
ações e com timeframes próprios: confirmação em **5 + 15 minutos**, com 2
minutos pra afinar o timing e 60 minutos de contexto da sessão.

**Só funciona pela Homelab (API).** O mini índice não existe no Yahoo — ele
vem do MetaTrader 5, pelo scraper. Pra habilitar:

1. Adicione `WINFUT` à watchlist (em *Gerenciar watchlist*).
2. Na VM do scraper, confira `SCRAPER_SYMBOL_MT5` — o padrão é
   `WINFUT=WIN$`, mas o nome do contrato depende da corretora (pode ser o
   contínuo `WIN$`/`WIN$N` ou o vencimento vigente, tipo `WINZ25`). É o que
   aparece no Observador de Mercado do seu MT5.
3. Reentregue o scraper (`make release-scraper`) e reinicie o serviço.

Os prazos de 2 e 5 minutos são coletados **só** pro WINFUT
(`SCRAPER_TIMEFRAMES_POR_SYMBOL`) — as ações seguem em M15/H1/H4/D1, sem
requisição extra.

O worker de assertividade ainda não cobre o mini índice nos timeframes dele:
ele varre tudo em M15/H1/H4/D1, com confirmação de Day Trade. A análise ao
vivo está correta; o histórico de acerto do WINFUT é que ainda não é medido
nos prazos próprios.

## Estilo de operação e confirmação multi-timeframe

| Estilo | Confirmação obrigatória | Contexto |
|---|---|---|
| **Day Trade** | M15 + H1 concordando | H4 e Diário |
| **Swing Trade** | Diário + Semanal concordando | H4 |

Se os dois timeframes obrigatórios discordam, a recomendação final é
**NEUTRO**, mesmo que um deles sozinho pareça forte. Vale igual na Análise
Individual (selo ✅/❌) e no Scanner (coluna "Confirmado").

O motor é o mesmo em qualquer timeframe — as fórmulas de stop e alvo (ATR,
Fibonacci, estrutura, expectativa estatística) se ajustam sozinhas à escala
do dado. Trocar de estilo muda só **quais dois timeframes precisam
concordar** e **qual contexto é mostrado**.

## Modalidade — qual leitura decide

Seletor **Modalidade**: uma leitura específica (Confluência, SMC, Price
Action, Médias Móveis, VWAP, IFR) ou **"Todas as modalidades"** (padrão), que
calcula um **Score Geral** — a média das cinco agregáveis — e decide a direção
por votação majoritária (empate = NEUTRO). É esse Score Geral que confirma e
ordena o Scanner.

O **IFR** é diferente das outras: só aponta direção em **exaustão** (IFR ≤ 10
ou ≥ 90 no Day Trade; 20/80 no Swing), então fica NEUTRO quase sempre — isso é
o desenho, não falha. Quando dispara, é raro e vale mais que score alto
isolado. A coluna **Exaustão IFR** do Scanner mostra quando dois ou mais
timeframes estão exauridos na mesma direção, que é a leitura de maior
convicção que a ferramenta produz.

Justamente por ficar NEUTRO quase sempre, **o IFR não entra na Confluência nem
no Score Geral**. Ele é leitura contrária, de exaustão; as outras quatro leem
estrutura e tendência. Somá-lo às duas contas diluía todo score sem
acrescentar informação e, no Score Geral, ainda endurecia a votação (a maioria
absoluta subia de 3-de-5 pra 4-de-6 por causa de uma leitura que nunca vota).
O IFR continua selecionável, com linha própria no histórico e coluna própria
no Scanner — só não vota pelos outros.

## Perfis de análise

Cerca de 35 parâmetros do motor que eram números cravados no código viraram
campos ajustáveis: períodos do ATR e do IFR, limiares de exaustão do IFR,
largura do swing, bandas de volatilidade, pesos da confluência, faixas de
qualidade, risco/retorno dos alvos, distância mínima do stop. Um **perfil** é
um conjunto nomeado desses valores, e cada sinal salvo guarda o perfil que o
gerou — então dá pra comparar a assertividade de uma calibragem contra a
outra.

Confira os números com `scripts/conferir-refactor-params.py`, que roda dois
motores sobre as mesmas séries e compara campo a campo. **Rode esse script
antes de mexer em qualquer um desses valores: este repo não tem suíte de
testes.** O perfil padrão não pode ser removido.

> **Mudança de calibragem em 10/08/2026.** O ATR passou a usar o suavizamento
> de Wilder (a convenção do MT5 e do TradingView) no lugar da média simples, e
> o IFR entrou como quinta leitura isolada, o que rebalanceou os pesos da
> confluência. Os dois mudam número: sinais gravados antes dessa data **não
> são diretamente comparáveis** com os de depois. O campo `atr_suavizacao`
> entra no `params_hash` justamente pra que dê pra separar os dois conjuntos
> no histórico.

Com a API do homelab configurada os perfis ficam no banco; sem ela, num
`daytrade_profiles.json` local — mesmo esquema da watchlist.

## Arquitetura

```
VM Windows (MT5 logado)          k3s (homelab)                  navegador
┌────────────────┐      ┌──────────────────────────────┐     ┌───────────┐
│ scraper/       │─────>│ processor ──> TimescaleDB     │     │ Streamlit │
│ (poll contínuo)│ HTTP │ analyzer  ──>  (candles +     │<────│           │
└────────────────┘      │ api       <──   sinais)       │HTTP └───────────┘
                        └──────────────────────────────┘
```

`backend/` é uma imagem só com quatro entrypoints (`processor`, `api`,
`analyzer`, `migrate`), entregue por ArgoCD. Os manifests não moram neste
repo — estão no repo `homelab`, em `applications/acoes/`.

**Detalhes de implantação, decisões de arquitetura e as armadilhas já pagas
estão em [`docs/homelab-pipeline.md`](docs/homelab-pipeline.md).** Comece por
lá antes de mexer no pipeline.

## Sobre Times & Trades / contagem de agressores

**Não está implementado, e é importante entender por quê:** contagem de
agressores (quem "bateu" no preço, compra ou venda) é dado de **negócio
individual (tick)**, que o Yahoo Finance não fornece — só dá candles OHLCV
agregados. Tape real exige uma fonte como ProfitDLL ou o MT5 com terminal
rodando localmente.

Existe uma alternativa **aproximada** (estimativa a partir do candle, não
tape real): usar a posição do fechamento dentro da máxima/mínima pra estimar
se o volume daquele candle foi majoritariamente comprador ou vendedor
(Money Flow / Close Location Value). Pode ser construído, mas **precisa ficar
claramente rotulado como estimativa**, não como dado de agressão real — dado
o que já aconteceu neste projeto por confusão de dado, isso se confirma antes
de adicionar.
