# Day Trade SMC — versão web (Streamlit)

## O que mudou em relação à sua versã0

- `daytrade_smc.py` — **seu arquivo original, sem nenhuma alteração**. Todo o motor de análise (SMC, Price Action, Médias, VWAP, Confluência, alvos alternativos) continua exatamente como você construiu.
- `streamlit_app.py` — **novo**. É a camada visual: importa as funções do seu arquivo e desenha um gráfico de candles real (Plotly) com EMAs, VWAP, swings e marcações de BOS/CHoCH, mais os painéis das 5 leituras.

Por que isso resolve os 3 pontos que você pediu:
1. **Roda pela internet, sem instalar nada** — Streamlit Community Cloud hospeda de graça, e o navegador do visitante só acessa uma URL normal.
2. **Vira ferramenta gráfica de verdade** — antes era tudo texto; agora tem candle, EMA, VWAP e swings desenhados.
3. **Lista fixa de ativos** — sua `DEFAULT_SYMBOLS` já existia e continua sendo o ponto de partida da watchlist, editável na barra lateral.

**Importante sobre a interface Tkinter que você fez:** ela continua no arquivo e funciona normalmente se um dia você rodar num Windows local. Só não dá pra publicar na internet, porque interface de desktop precisa de tela — servidor não tem. O Streamlit substitui essa parte pra uso online; localmente, se quiser, ainda pode usar `python daytrade_smc.py --gui`.

---

## Como publicar na internet (sem instalar nada, 100% pelo navegador)

### Passo 1 — Criar uma conta no GitHub (gratuito)
Acesse **github.com**, crie uma conta se ainda não tiver.

### Passo 2 — Criar o repositório e subir os arquivos
1. No GitHub, clique em **"New repository"** (botão verde).
2. Dê um nome, ex: `daytrade-smc`. Marque como **Public**. Clique em **Create repository**.
3. Na página do repositório vazio, clique em **"uploading an existing file"**.
4. Arraste estes 3 arquivos (estão nesta pasta que te entreguei):
   - `daytrade_smc.py`
   - `streamlit_app.py`
   - `requirements.txt`
5. Clique em **Commit changes**.

Isso é só upload de arquivo pelo navegador — sem terminal, sem git instalado, sem nada.

### Passo 3 — Conectar no Streamlit Community Cloud
1. Acesse **share.streamlit.io** e entre com sua conta GitHub (botão "Continue with GitHub").
2. Clique em **"New app"**.
3. Selecione o repositório `daytrade-smc` que você acabou de criar.
4. No campo **"Main file path"**, digite: `streamlit_app.py`
5. Clique em **Deploy**.

Em 1-2 minutos o app fica no ar, com uma URL tipo `https://seu-usuario-daytrade-smc.streamlit.app` — acessível de qualquer navegador, computador ou celular, sem login nem instalação pra quem for usar.

### Atualizando depois
Qualquer mudança nos arquivos: edite direto na página do arquivo no GitHub (ícone de lápis) e clique em **Commit changes**. O Streamlit Cloud redeploya sozinho em menos de um minuto.

---

## Usando pelo celular

Depois de publicado, é só abrir a URL no navegador do celular — a interface do Streamlit já se adapta a telas pequenas. Pra ficar com "cara de app", use a opção **"Adicionar à tela inicial"** do navegador.

---

## Testando localmente antes de publicar (opcional, se algum dia tiver Python)

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

---

## Atualizar via MT5 sem instalar nada no trabalho (ponte GitHub Actions)

Se você **não pode rodar nada no computador do trabalho**, essa é a opção certa: o app continua publicado no Streamlit Cloud (acessível só pelo navegador, como sempre), e um botão dispara a busca de dado real via MT5 no seu **PC de casa**, sob demanda — não fica atualizando sozinho.

```
Você clica "Atualizar via MT5" no app (do trabalho, só navegador)
        │
        ▼
App dispara o workflow do GitHub Actions (via API)
        │
        ▼
GitHub aciona o "runner" instalado no seu PC de casa
        │
        ▼
PC de casa (com MT5 aberto) busca os candles reais,
salva um arquivo e sobe pro GitHub
        │
        ▼
App detecta a atualização e mostra os dados novos
```

Leva entre 10 e 30 segundos do clique até aparecer o resultado — não é 0 segundos, mas já é bem mais rápido que os 15-20 minutos do Yahoo, e **você não instala nada no trabalho**, só usa o navegador.

### Passo 1 — Criar um token do GitHub (uma vez só)

1. No GitHub: **Settings** (da sua conta, não do repositório) → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. Dá um nome, escolhe o repositório do projeto, e em **Permissions** marca:
   - **Actions**: Read and write
   - **Contents**: Read and write
3. Gera o token e **copia** (só aparece uma vez).

### Passo 2 — Configurar o token no Streamlit Cloud

1. No painel do seu app no Streamlit Cloud → **Settings** → **Secrets**.
2. Cola isso, trocando pelos seus dados:
   ```toml
   github_repo = "seu-usuario/nome-do-repositorio"
   github_token = "o-token-que-voce-copiou"
   ```
3. Salva — o app reinicia sozinho.

### Passo 3 — Instalar o runner no PC de casa (uma vez só)

1. No GitHub, no repositório → **Settings** → **Actions** → **Runners** → **New self-hosted runner**.
2. Escolhe **Windows**, e segue exatamente os comandos que o GitHub mostra na tela (baixa um pacote, extrai, roda `config.cmd` com um token temporário que o próprio GitHub gera ali).
3. No final, ele pergunta se quer rodar como serviço — escolhe **sim** (assim ele fica ativo sempre que o PC ligar, sem precisar abrir nada manualmente).
4. Confirma que o MetaTrader 5 está instalado e logado nesse mesmo PC, e que as dependências estão instaladas:
   ```
   pip install -r requirements.txt
   pip install -r requirements-local.txt
   ```

### Passo 4 — Usar

Na barra lateral do app → **Fonte de dados → GitHub (MT5 de casa)** → botão **🔄 Atualizar via MT5 (casa)**. O app mostra a data/hora da última atualização, sempre visível, pra nunca ficar em dúvida se o dado que está vendo é fresco ou antigo.

**O que precisa estar ligado em casa pra funcionar:** o PC ligado, o MetaTrader 5 aberto e logado, e internet funcionando (o runner do GitHub fica esperando em segundo plano, não precisa abrir nada manualmente toda vez).

## MetaTrader 5 direto (rodando local, sem passar pelo GitHub)

*(Se você não pode rodar nada no computador de onde vai acessar — veja a seção "Atualizar via MT5 sem instalar nada no trabalho" logo acima. Esta seção aqui é pra quando você acessa direto do PC que já tem o MT5, sem precisar da ponte via GitHub.)*

Seletor **Fonte de dados** na barra lateral tem 3 opções: **Yahoo Finance** (padrão, ~15-20min de atraso, funciona em qualquer lugar), **MetaTrader 5** (tempo real, direto, mas com uma limitação importante) ou **GitHub (MT5 de casa)** (a ponte descrita acima).

### A limitação do MetaTrader 5 direto

O MT5 não é uma API de internet — é uma DLL que se comunica com o terminal MT5 **aberto na mesma máquina**. Isso significa: **o app não pode usar essa opção rodando no Streamlit Cloud** (servidor Linux, sem MT5 instalado). Pra usar dado real direto, você precisa rodar o app **localmente**, no Windows, na mesma máquina onde o MetaTrader 5 está aberto e logado.

Se você tentar selecionar "MetaTrader 5" enquanto o app está publicado na nuvem, vai aparecer um erro claro explicando isso — não trava o app, só avisa.

### Como rodar localmente com MT5

1. No computador com o MT5 instalado e logado na sua corretora:
   ```
   pip install -r requirements.txt
   pip install -r requirements-local.txt
   streamlit run streamlit_app.py
   ```
2. Abre `http://localhost:8501` no navegador desse mesmo computador — já funciona local.
3. Na barra lateral, troca **Fonte de dados** pra **MetaTrader 5**.

O cache interno também muda sozinho: com MT5 (chamada local, sem limite de requisições), os dados atualizam a cada 3 segundos em vez de 60 — bem mais perto de "instantâneo".

### Acessando do trabalho — deixando o PC de casa ligado

Como o app agora roda no seu PC de casa (não mais na nuvem), acessar de outro lugar (como o trabalho) significa **acessar remotamente essa máquina**, não abrir uma URL pública qualquer. A forma mais simples e seripcelo de fazer isso, sem mexer em configuração de roteador:

**Tailscale** (recomendado — gratuito, criptografado, sem abrir porta no roteador):
1. Instala o [Tailscale](https://tailscale.com/download) no PC de casa e no computador do trabalho (ou no celular).
2. Entra com a mesma conta (Google/Microsoft/etc) nos dois.
3. Isso cria uma rede privada entre os dois dispositivos — o Tailscale te dá um endereço tipo `100.x.x.x` pro PC de casa.
4. No trabalho, acessa `http://100.x.x.x:8501` no navegador — é como se estivesse na mesma rede de casa, mesmo estando longe.

Isso é bem mais simples e seguro que abrir porta no roteador (o que expõe seu PC pra internet toda). Alternativas equivalentes: Cloudflare Tunnel ou ngrok, mas o Tailscale é o mais direto pra esse uso.

**Resumo do que muda:**
- PC de casa: MT5 aberto e logado + `streamlit run streamlit_app.py` rodando + Tailscale instalado.
- PC do trabalho: só precisa do navegador + Tailscale instalado (pra entrar na mesma rede privada).
- Se o PC de casa desligar ou o MT5 fechar, a fonte MT5 para de funcionar — pode trocar de volta pra Yahoo Finance na hora, sem precisar reiniciar nada.

## Filtro de modalidade — qual leitura decide a recomendação

Seletor **Modalidade** na barra lateral, abaixo do Estilo de operação: **Todas as modalidades** (padrão), **Confluência**, **SMC**, **Price Action**, **Médias Móveis** ou **VWAP**.

- Escolhendo uma leitura específica, a confirmação e o Scanner usam só aquela leitura (mesmo comportamento anterior).
- **Escolhendo "Todas as modalidades"**, o sistema calcula um **Score Geral** — a média do score das 5 leituras (Confluência, SMC, Price Action, Médias Móveis, VWAP) — e a **direção geral** por votação majoritária entre elas (empate = NEUTRO). É esse Score Geral que decide a confirmação e ordena o ranking do Scanner. A entrada/stop/alvo mostrados, quando confirmado, ainda vêm do plano de risco da Confluência (só quando ela concorda com a direção geral — senão não há um único plano coerente pra mostrar, só a votação).

## Ranking do Scanner: coluna "Posição" e ordenação por Score Geral

O Scanner agora sempre calcula um **Score Geral** por ativo (média do score nos dois timeframes de confirmação) e ordena por ele — confirmados primeiro, do maior score pro menor. Uma coluna **Posição**, numerada 1, 2, 3... em ordem crescente, mostra o ranking: posição 1 é a melhor colocada.

## Verificação retroativa — "esse sinal teria dado certo?"

Terceiro modo na barra lateral, ao lado de Análise Individual e Scanner. Escolha um ativo, um timeframe e uma data no passado — o app roda a análise usando **só os dados que existiam até aquele fechamento** (sem espiar o futuro), e depois confere o que aconteceu de verdade nos candles seguintes: bateu o Alvo 1, o Alvo 2, o Stop, ou ainda está em aberto.

Isso é diferente do backtest de "rodar em centenas de ativos" — é uma checagem pontual, pra você conferir manualmente um dia específico que te chamou atenção (ex: "o sinal de ontem no fechamento teria dado certo?").

**Limitação real:** como este ambiente de desenvolvimento não tem acesso à internet, essa funcionalidade só pode ser testada de verdade rodando o app publicado (que tem acesso real ao Yahoo Finance). O histórico intraday do Yahoo (M15/H1/H4) cobre só ~60 dias — pra checar datas mais antigas, use Diário ou Semanal.

## Perfis de análise e assertividade

Duas coisas que andam juntas: poder **mexer nos parâmetros** do motor, e
depois **saber se mexer valeu a pena**.

### Perfis (barra lateral → "Perfil de análise")

Cerca de 30 parâmetros do motor que antes eram números cravados no código
viraram campos ajustáveis: período do ATR, largura do swing, bandas de
volatilidade, pesos da confluência (SMC 30 / Price Action 20 / Médias 20 /
VWAP 20), faixas de qualidade, risco/retorno dos alvos, distância mínima do
stop, e por aí. Um **perfil** é um conjunto nomeado desses valores.

O perfil `padrão` reproduz exatamente o comportamento histórico do motor —
não é "quase igual", é idêntico campo a campo, conferido por um script que
roda os dois motores sobre as mesmas séries (`scripts/conferir-refactor-params.py`).
Crie perfis novos ao lado dele; o padrão não pode ser removido.

Com a API do homelab configurada os perfis ficam no banco; sem ela, num
`daytrade_profiles.json` local — mesmo esquema da watchlist.

### Assertividade (quarto modo na barra lateral)

Cada sinal gerado é gravado com o perfil que o gerou, e depois tem o
desfecho conferido contra os candles que vieram de verdade: bateu o Alvo 1,
o Alvo 2, ou o Stop. Daí sai a taxa de acerto **por tipo de análise**
(Confluência, SMC, Price Action, Médias Móveis, VWAP), recortada ainda por
timeframe, ativo, direção, faixa de score e se o sinal estava confirmado no
multi-timeframe. Além do acerto, a **expectativa em R** — porque uma leitura
pode acertar pouco e ainda ser lucrativa, ou o contrário.

De onde vêm os sinais:

- **Automaticamente**, do worker `analyzer` que roda no homelab e varre a
  watchlist inteira a cada vela. É ele que torna isso uma medição: sem o
  worker, só existiriam os sinais que você por acaso olhou, e a taxa sairia
  enviesada pelo seu uso da tela.
- **Manualmente**, pelo botão "💾 Salvar sinal" em qualquer painel de
  leitura. Ficam marcados com origem `manual`, separados dos do worker, e
  clicar duas vezes não cria duas linhas.

**Cuidado ao ler os números:** a taxa de acerto e a expectativa só contam os
sinais **já resolvidos**. Os em aberto aparecem no contador mas ficam de
fora da conta até baterem alvo ou stop — por isso a tabela sempre mostra
"sinais" e "resolvidos" em colunas separadas.

Este modo depende da API do homelab (`ACOES_API_URL`): é onde o histórico
mora. Sem ela, o modo explica isso em vez de mostrar número errado.

## Estilo de operação: Day Trade ou Swing Trade

A barra lateral agora tem um seletor **Estilo de operação**, logo abaixo do modo:

| Estilo | Confirmação obrigatória | Contexto |
|---|---|---|
| **Day Trade** | M15 + H1 concordando | H4 e Diário |
| **Swing Trade** | Diário + Semanal concordando | H4 (pra afinar o timing de entrada dentro da tendência maior) |

O motor por trás é o mesmo — SMC, Price Action, Médias, VWAP e Confluência funcionam exatamente igual em qualquer timeframe, e as fórmulas de stop/alvo (ATR, Fibonacci, estrutura, expectativa estatística) já se ajustam sozinhas à escala do timeframe, sem precisar de nenhuma lógica nova pra isso. Trocar de Day Trade pra Swing Trade só muda **quais dois timeframes precisam concordar** e **qual contexto é mostrado** — todo o resto (Scanner, calculadora de posição, painéis de risco) funciona igual nos dois estilos.

## O que tem agora (além da análise individual)

- **Modo Scanner** — roda a análise em TODOS os ativos da watchlist de uma vez e mostra um ranking por score de confluência, com um botão pra abrir a análise completa de qualquer um deles direto do ranking. É o "Top N" do seu prompt original.
- **Zonas de FVG desenhadas no gráfico** — retângulo sombreado mostrando o gap ainda não preenchido, além da marcação em texto que já existia no painel SMC.
- **Watchlist persistida** — usa as funções `load_symbols`/`save_symbols` que você já tinha; a lista sobrevive a recarregamentos de página enquanto o servidor não reiniciar (ver ressalva abaixo).
- **Atualização automática** — checkbox na análise individual, com intervalo configurável (30s a 5min), usando `st.fragment` do Streamlit pra atualizar só o painel, sem recarregar a página inteira.

**Ressalva sobre a persistência:** no Streamlit Community Cloud, o disco do servidor é resetado a cada novo deploy (quando você edita um arquivo no GitHub) e após longos períodos de inatividade. Ou seja, a lista sobrevive a você só recarregar a página ou vários usuários acessando ao mesmo tempo, mas não sobrevive a um redeploy. Se isso incomodar, a solução definitiva é salvar num banco externo (ex: um Google Sheets ou um banco gratuito tipo Supabase) em vez de arquivo local — posso implementar isso se quiser.

## O que ainda pode ser melhorado

1. **Persistência de verdade entre deploys** — trocar o arquivo local por um banco externo gratuito (Google Sheets via API, ou Supabase), pra lista sobreviver a redeploys.
2. **Alertas por e-mail/Telegram** — disparar uma notificação quando o Scanner encontrar um score acima de um limiar (ex: 80+) em algum ativo da watchlist.
3. ~~Múltiplos timeframes na mesma tela~~ — feito: M15/H1/H4/Diário agora, com confirmação M15+H1 obrigatória (ver seção "Filtro multi-timeframe" abaixo).
4. ~~Histórico de sinais~~ — feito: os sinais são gravados e têm o desfecho conferido automaticamente, com taxa de acerto por tipo de análise (ver "Perfis de análise e assertividade" abaixo).
5. **Times & Trades aproximado** — indicador de pressão compradora/vendedora estimada a partir do candle (ver seção dedicada abaixo) — ainda não implementado, aguardando sua confirmação.

Me avisa qual desses (ou outra ideia) você quer que eu implemente a seguir.

## Filtro multi-timeframe (M15 + H1 obrigatórios, H4 + Diário como contexto)

A recomendação só é considerada **confirmada** quando M15 e H1 concordam na mesma direção — se um diz COMPRA e o outro diz NEUTRO ou VENDA, a recomendação final é NEUTRO, mesmo que M15 sozinho parecesse um sinal forte. Isso vale tanto na Análise Individual (selo ✅/❌ no topo da tela) quanto no Scanner (coluna "Confirmado").

H4 (240 minutos) e Diário aparecem como abas de contexto na Análise Individual — mostram a tendência mais ampla, mas não bloqueiam nem confirmam a recomendação sozinhos. H4 é construído agregando 4 candles de H1 (o Yahoo Finance não tem esse intervalo nativo).

**Custo em chamadas ao Yahoo:** o Scanner agora busca 2 timeframes por ativo (M15 e H1) em vez de 1 — o dobro de chamadas. Se sua watchlist for grande, considere isso ao rodar o scanner com frequência (rate limit do Yahoo).

## Sobre Times & Trades / contagem de agressores

**Isso não está implementado, e é importante entender por quê:** contagem de agressores (quem "bateu" no preço, compra ou venda) é dado de **negócio individual (tick)**, algo que o Yahoo Finance simplesmente não fornece — só dá candles OHLCV agregados. Isso é o mesmo limite que motivou a conversa sobre ProfitDLL/MetaTrader 5 no README acima: dado de tape real exige uma fonte como essas, com terminal rodando localmente.

Existe uma alternativa **aproximada** (não é tape real, é uma estimativa a partir do candle): usar a posição do fechamento dentro da máxima/mínima do candle pra estimar se o volume daquele candle foi majoritariamente comprador ou vendedor (técnica conhecida como Money Flow / Close Location Value). Isso pode ser construído se você quiser, mas **precisa ficar claramente rotulado como estimativa**, não como dado de agressão real — dado o que já aconteceu antes nesse projeto por confusão de dado, prefiro confirmar com você antes de adicionar isso.