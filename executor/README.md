# executor — envio automático de ordens

Serviço Windows que transforma sinais em ordens no MetaTrader 5, segundo
regras de (perfil, modalidade, timeframe, ativo).

Roda na **mesma VM do scraper**, e não no k3s, pela mesma razão dele: a
integração do MT5 é binária de Windows. O `analyzer` é quem gera os sinais,
mas roda em Linux no cluster e nunca vai poder enviar ordem, por mais
natural que pareça colocá-lo ali.

## O ciclo

1. lê as regras ativas — `GET /auto-ordem`
2. para cada regra, os sinais recentes daquele recorte — `GET /signals`
3. descarta o que não deve virar ordem (ver as travas)
4. **reserva** o sinal — `POST /ordens`; se vier `duplicado`, para aqui
5. envia a mercado com stop e alvo — `execucao.enviar_ordem_mercado`
6. grava o que a corretora respondeu — `PUT /ordens/{signal_id}`

## A reconciliação

Em paralelo ao ciclo de envio, o executor pergunta ao terminal o que
aconteceu com cada posição em curso (`GET /ordens?aberta=true` →
`execucao.consultar_posicao` → `PUT /ordens/{signal_id}/fechamento`). É o que
transforma "mandei 40 ordens" em "estas regras deram dinheiro".

O resultado vem do MT5, não de conta feita aqui: soma **todos** os deals da
posição (o de entrada tem lucro zero mas carrega corretagem), pondera o preço
de saída por volume quando houve fechamento parcial, e traduz o
`DEAL_REASON_*` do deal de saída em `STOP` / `ALVO` / `MANUAL` / `EXPERT` /
`MARGEM`. Só o extrato sabe de deslize e de fechamento na mão.

Enquanto a posição está aberta ela também é gravada, com `fechado_em` vazio e
o resultado **não realizado** do momento — é o que faz a tela mostrar "3
abertas, +R$ 48 no papel" sem misturar isso com o que já é dinheiro.

Roda em três momentos: no startup (pega o que fechou com o serviço fora do
ar), a cada `EXECUTOR_CONCILIACAO_SEGUNDOS` durante o pregão, e **uma última
vez quando o pregão fecha** — sem essa, o que fechar no leilão só apareceria
no dia seguinte.

E roda mesmo com `ORDENS_HABILITADAS` desligado: ler o desfecho do que já saiu
não manda nada, e é justamente o que se quer poder fazer *depois* de desligar
o envio.

> ⚠️ **Fuso.** O MT5 devolve horário no relógio do servidor codificado como se
> fosse UTC. `execucao.hora_do_mt5` reinterpreta, igual `_fetch_ohlcv_mt5`. Ler
> direto como UTC não quebra nada visível — as datas continuam plausíveis, só
> apontam 3h para o lado — e todo relatório por dia sai torto em silêncio. O
> executor confere isso a cada reconciliação contra o `criado_em` da ordem e
> avisa no log se a convenção mudar.

## As travas, e o que cada uma evita

| Trava | Evita |
|---|---|
| `ORDENS_HABILITADAS` (padrão **desligado**) | subir o serviço ser a mesma decisão que autorizar ordem |
| `MODO_CONTA_EXIGIDO` (padrão **DEMO**) | ordem cair em conta real |
| `MT5_LOGIN` | operar numa conta que não é a que você acha |
| frescor (padrão 15 min) | rajada de ordens antigas depois de um reinício |
| reserva antes do envio | posição dobrada se o processo morrer no meio |
| posição já aberta | dobrar exposição no mesmo papel sem decidir isso |
| coerência de stop/alvo | alvo atrás do preço — prejuízo certo gravado como "ALVO" |
| tamanho pela cotação | risco real virar múltiplo do configurado quando o preço anda |

O **frescor** é a mais importante. Sem ela, subir o serviço depois de
qualquer parada dispararia ordens para sinais de horas atrás, a preços que
não existem mais — e o pior momento para isso é justamente a volta de uma
queda.

A **reserva antes do envio** é o que sobrevive a reinício: a linha em
`ordens` nasce antes de a ordem sair, com `signal_id` único. O pior caso
vira uma linha `ENVIANDO` órfã, que se vê, em vez de posição dobrada, que só
aparece no extrato.

## Instalação (NSSM)

```
nssm install AcoesExecutor "C:\...\python.exe" C:\acoes\executor\executor.py
nssm set AcoesExecutor AppDirectory C:\acoes
nssm set AcoesExecutor AppStdout C:\acoes\executor.log
nssm set AcoesExecutor AppStderr C:\acoes\executor.log
nssm set AcoesExecutor AppExit Default Restart
nssm set AcoesExecutor AppEnvironmentExtra ^
  ACOES_API_URL=https://acoes-api.dondon.services ^
  ACOES_API_KEY=<a chave> ^
  MT5_LOGIN=<login da conta demo> ^
  MODO_CONTA_EXIGIDO=DEMO ^
  ORDENS_HABILITADAS=true
nssm start AcoesExecutor
```

`AppEnvironmentExtra` **substitui** a lista inteira — repita tudo a cada
alteração.

Código novo chega por `make release-scraper`, que para e sobe os **dois**
serviços da VM (`AcoesScraper` e `AcoesExecutor`). Até 2026-08-12 ele só
reiniciava o scraper: o `executor.py` novo chegava no disco e o serviço seguia
rodando o antigo em memória, sem nada indicar isso.

## Ligando uma regra

O executor não faz nada sem regra: com a tabela vazia, ele varre e dorme.
Ligar uma regra é o ato deliberado que inicia o envio.

```bash
curl -X PUT https://acoes-api.dondon.services/auto-ordem \
  -H "X-API-Key: <a chave>" -H "Content-Type: application/json" \
  -d '{"perfil":"fine_tuned_v2","modalidade":"Confluência",
       "timeframe":"M15","risco_maximo":200,"ativo":true}'
```

A identidade da regra é `(perfil, modalidade, timeframe, symbol)` — o ativo faz
parte da chave. Sem `symbol` (ou com `""`) a regra vale para **qualquer** ativo,
que é o comportamento histórico; com `"symbol":"VALE3"` ela só opera aquele
papel, e pode coexistir com outra regra no mesmo recorte para outro ativo.

`horario_inicio`/`horario_fim` (HH:MM, fuso do pregão) limitam em que horas do
dia a regra pode enviar. Nulos = sem restrição além do gate de pregão; dá para
mandar só um dos dois. Uma faixa com início maior que o fim cruza a meia-noite:
`{"horario_inicio":"22:30","horario_fim":"10:00"}` envia de 22:30 até as 10:00.

```bash
curl -X PUT https://acoes-api.dondon.services/auto-ordem \
  -H "X-API-Key: <a chave>" -H "Content-Type: application/json" \
  -d '{"perfil":"fine_tuned_v2","modalidade":"Confluência",
       "timeframe":"M15","symbol":"VALE3",
       "horario_inicio":"10:15","horario_fim":"17:30",
       "risco_maximo":200,"ativo":true}'
```

`risco_maximo` é em **reais**, não em quantidade. A quantidade sai da
distância até o stop, então toda operação arrisca o mesmo valor: um papel com
stop a R$0,90 recebe 222 ações e outro com stop a R$1,20 recebe 167. Com
quantidade fixa, o segundo arriscaria 33% a mais sem ninguém ter escolhido
isso.

A distância é medida contra a **cotação do instante do envio**, não contra a
entrada modelada do sinal, e o lote é arredondado **para baixo** — as duas
coisas para o "máximo" do nome valer. Se nem um lote mínimo couber no risco
(papel caro com stop largo), o executor manda o mínimo e avisa no log o
quanto passou: nunca operar aquele papel seria uma falha mais silenciosa que
um risco maior escrito.

Para desligar, o mesmo PUT com `"ativo": false`.

## Conferindo

```bash
curl -s https://acoes-api.dondon.services/ordens -H "X-API-Key: <a chave>"
curl -s https://acoes-api.dondon.services/ordens/stats -H "X-API-Key: <a chave>"
```

`status`: `ENVIANDO` (reservada, sem resposta ainda), `ENVIADA`, `FALHOU`
(a corretora recusou) ou `RECUSADA` (as travas locais barraram, nada saiu da
máquina). `tipo_conta` diz se foi DEMO ou REAL — resultado em demo não é
dinheiro.

`/ordens/stats` agrega por regra, ativo, motivo de saída, direção e conta. Ao
ler: a taxa de acerto sai de `ganhos + perdas`, e `resultado_reais` soma só as
FECHADAS — o não realizado das abertas vem à parte em `aberto_reais`, porque
somar os dois é anunciar lucro que ainda pode sumir. `recusadas` e `falhadas`
ficam fora das taxas (nada delas chegou ao mercado), mas número alto ali é
problema de configuração, não de estratégia.

Pela interface web, a mesma coisa está em **Sinais › Ordens**, junto do
botão de ligar e desligar cada regra.

## Ordem de teste

Ordem disparada à mão pra conferir o encanamento sai de verdade e executa de
verdade — e não mede regra nenhuma, porque quem a escolheu foi uma pessoa e
não o filtro da regra. Por isso ela é **marcada**, não apagada:

```python
executor._processar(sinal, regra, teste=True)
```

A linha nasce com `teste=true` e fica fora de `GET /ordens` e
`GET /ordens/stats` por padrão. Para vê-la, `incluir_testes=true` na rota, ou
o toggle "Incluir ordens de teste" na tela — onde ela aparece com 🧪 ao lado
do ativo. A notificação também sai marcada.

O laço do serviço nunca passa `teste=True`; só chamada manual passa, o que é
exatamente o que distingue uma da outra. E a reconciliação **enxerga** as de
teste: posição aberta na corretora é posição aberta, e deixá-la de fora a
deixaria pendente pra sempre, ocupando o símbolo na trava de "posição já
aberta".

## Duas contas na mesma máquina

Ver a seção correspondente em `scraper/README.md`. Resumo: instâncias
abertas da mesma pasta compartilham o diretório de dados e o
`mt5.initialize()` pega uma delas sem critério — medido em 2026-08-11, o
serviço pegou a demo e um processo manual pegou a real com minutos de
diferença. Para separar de verdade, duas instalações (ou uma cópia rodando
com `/portable`) e `MT5_PATH` apontando cada serviço para a sua.
