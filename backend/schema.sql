-- Schema do database `daytrade` no TimescaleDB compartilhado do homelab.
--
-- Aplicado por backend/migrate.py como hook PreSync do ArgoCD, ou seja, a
-- CADA sync — por isso tudo aqui precisa ser idempotente. Até 2026-08 este
-- arquivo era `docker-entrypoint-initdb.d` do Compose, que roda uma vez só
-- num banco vazio, e as guardas abaixo não existiam.

-- Candles: uma linha por (symbol, timeframe, time). A chave primária
-- composta serve tanto de upsert idempotente quanto satisfaz a exigência
-- do Timescale de a coluna de particionamento (time) estar na chave.
CREATE TABLE IF NOT EXISTS candles (
    symbol      TEXT             NOT NULL,
    timeframe   TEXT             NOT NULL,   -- 'M2' | 'M5' | 'M15' | 'H1' | 'H4' | 'D1' | 'W1'
    time        TIMESTAMPTZ      NOT NULL,   -- UTC, mesmo índice que fetch_ohlcv já produz
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL,
    source      TEXT             NOT NULL DEFAULT 'MetaTrader 5',
    ingested_at TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, timeframe, time)
);

SELECT create_hypertable('candles', 'time', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);

-- A janela de 7 dias do create_hypertable acima só vale pra quem cria a
-- tabela do zero; este set_chunk_time_interval é o que reajusta um banco já
-- existente, e vale pra chunks NOVOS (os antigos ficam como estão).
--
-- 7 dias era pequeno demais: o backfill de D1 vai até 2022, o que gerou 213
-- chunks pra 40 mil linhas — ~190 linhas por chunk, quando o Timescale é
-- dimensionado pra chunks de milhões. O preço aparece no planejamento de
-- query, não no disco: em 2026-08-04 o GET /status gastava 150 ms de planning
-- (contra 29 ms de execução) só pra montar um plano de 1.270 linhas sobre
-- todos os chunks.
SELECT set_chunk_time_interval('candles', INTERVAL '90 days');

-- NÃO recrie um índice (symbol, timeframe, time DESC) aqui.
--
-- Ele existiu até 2026-08 com a justificativa de servir o padrão de leitura
-- "últimas N velas de um symbol+timeframe, time DESC". Só que a PRIMARY KEY
-- (symbol, timeframe, time) já resolve isso: um btree é varrido pra trás sem
-- custo extra. Confirmado por EXPLAIN no chunk quente, onde o próprio planner
-- escolheu a PK e ignorou o índice dedicado:
--
--   ->  Index Scan Backward using "1_candles_pkey" on _hyper_1_1_chunk
--         Index Cond: ((symbol = 'VALE3') AND (timeframe = 'M15'))
--
-- O que ele custava: os índices ocupavam 13 MB contra 7,2 MB de heap, e cada
-- update não-HOT pagava a escrita nos dois.
DROP INDEX IF EXISTS candles_symbol_tf_time_desc_idx;

-- Watchlist: fonte única de verdade, substitui o daytrade_symbols.json
-- local quando ACOES_API_URL está configurado. `active` permite que
-- "remover" seja soft-delete em vez de DELETE definitivo.
CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    active   BOOLEAN     NOT NULL DEFAULT true,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO watchlist (symbol) VALUES
    ('VALE3'), ('PETR4'), ('PRIO3'), ('ITUB4'), ('BBAS3'),
    ('BBDC4'), ('B3SA3'), ('WEGE3'), ('ABEV3'), ('MGLU3'), ('BRA50')
ON CONFLICT (symbol) DO NOTHING;

-- Perfis de análise: conjunto NOMEADO de parâmetros do motor
-- (`daytrade_smc.AnalysisParams`). Espelha o papel da `watchlist` acima —
-- fonte única de verdade quando ACOES_API_URL está configurada, com
-- fallback pro `daytrade_profiles.json` local senão.
--
-- `params` guarda SÓ os campos diferentes do default. Por isso o perfil
-- semeado abaixo é `{}`, e não o dicionário completo: quando um parâmetro
-- novo é acrescentado ao motor, o perfil 'padrão' continua significando
-- "todos os defaults de hoje", sem precisar de migration de dados.
--
-- `ativo` em vez de DELETE pelo mesmo motivo da watchlist, mas com um
-- agravante: um perfil que já gerou sinais NÃO pode sumir, senão a
-- comparação histórica entre calibragens (que é o ponto de existir
-- perfil) quebra. A FK lá embaixo em `signals.perfil` é RESTRICT.
--
-- ATENÇÃO: `params_hash` aqui é CONSULTIVO — é só o que o cliente que
-- gravou disse. Quem manda numa comparação é o `params_hash` copiado em
-- cada linha de `signals` no momento em que o sinal foi gerado.
CREATE TABLE IF NOT EXISTS analysis_profiles (
    nome        TEXT PRIMARY KEY,
    params      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    params_hash TEXT        NOT NULL DEFAULT '',
    descricao   TEXT        NOT NULL DEFAULT '',
    ativo       BOOLEAN     NOT NULL DEFAULT true,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    alterado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO analysis_profiles (nome, params, params_hash, descricao) VALUES
    ('padrão', '{}'::jsonb, 'defaults',
     'Comportamento histórico do motor — nenhum parâmetro alterado.')
ON CONFLICT (nome) DO NOTHING;

-- Sinais gerados, uma linha por (ativo, timeframe, modalidade, vela).
-- Três produtores, distinguidos por `origem`: o worker `analyzer.py`
-- varrendo a watchlist a cada vela ('worker'), o botão "salvar sinal" do
-- Streamlit ('manual') e o passe único que reconstrói o histórico já
-- guardado, `analyzer.py --backfill` ('backfill').
--
-- `origem` entra no índice de dedup mais abaixo justamente pra que os três
-- possam descrever a MESMA vela sem colidir — e pra que a assertividade
-- medida em tempo real continue separável da reconstruída.
--
-- O DESFECHO mora aqui, em coluna, e não numa tabela separada: um sinal
-- tem exatamente um desfecho, nunca um histórico deles. Uma tabela 1:1
-- custaria um join em TODA consulta de assertividade (o caminho quente
-- do painel) pra ganhar só o fato de esta tabela ser append-only. Nesse
-- volume, não paga.
--
-- `r_alvo_1`/`r_alvo_2` não são decoração. Como `rr_alvo_1`/`rr_alvo_2`
-- viraram parâmetros ajustáveis por perfil, um `CASE resultado WHEN
-- 'ALVO_2' THEN 3.0` cravado no SQL de agregação mentiria pra qualquer
-- perfil que os tivesse mudado. O R realizado fica gravado por linha.
--
-- NÃO é hypertable, de propósito. O volume é de ~2 mil linhas por pregão
-- (11 símbolos × 4 timeframes × 6 modalidades, uma linha por VELA e não
-- por varredura), ~110 MB por ano. Chunk do Timescale é dimensionado pra
-- milhões de linhas — a lição de 2026-08 na `candles` logo acima foi
-- exatamente essa: 213 chunks pra 40 mil linhas custaram 150 ms de
-- planning. Além disso o caminho de escrita depende de ON CONFLICT sobre
-- índice único e o de desfecho depende de UPDATE, os dois mais simples
-- numa tabela comum. Se um dia virar dezenas de milhões de linhas,
-- `create_hypertable(..., migrate_data => TRUE)` continua disponível.
--
-- Sem CHECK nas colunas tipo-enum (`direcao`, `origem`, `modalidade`,
-- `resultado`): omissão consciente, no mesmo estilo do resto do schema.
-- Os valores vêm de `Direction`, `MODALITIES` e `evaluate_signal_outcome`
-- no cliente, e um CHECK aqui viraria uma segunda fonte de verdade pra
-- manter em sincronia a cada valor novo.
CREATE TABLE IF NOT EXISTS signals (
    id                BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol            TEXT        NOT NULL,
    timeframe         TEXT        NOT NULL,
    modalidade        TEXT        NOT NULL,   -- Confluência | SMC | Price Action | Médias Móveis | VWAP | IFR
    candle_time       TIMESTAMPTZ NOT NULL,   -- UTC, abertura da vela FECHADA que gerou o sinal
    perfil            TEXT        NOT NULL REFERENCES analysis_profiles(nome),
    params_hash       TEXT        NOT NULL,
    origem            TEXT        NOT NULL,   -- 'worker' | 'manual' | 'backfill'
    direcao           TEXT        NOT NULL,   -- COMPRA | VENDA | NEUTRO
    score             DOUBLE PRECISION NOT NULL,
    confianca         DOUBLE PRECISION NOT NULL,
    setup             TEXT        NOT NULL,
    mtf_confirmado    BOOLEAN     NOT NULL DEFAULT false,
    mtf_direcao       TEXT        NOT NULL DEFAULT 'NEUTRO',
    entrada           DOUBLE PRECISION,
    stop              DOUBLE PRECISION,
    alvo_1            DOUBLE PRECISION,
    alvo_2            DOUBLE PRECISION,
    r_alvo_1          DOUBLE PRECISION,       -- R realizado se o alvo 1 bater
    r_alvo_2          DOUBLE PRECISION,       -- idem, alvo 2
    stop_basis        TEXT        NOT NULL DEFAULT '',
    detalhes          JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- motivos, alertas, alvos alternativos
    criado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Desfecho, preenchido pelo segundo passe do analyzer.
    resultado             TEXT,               -- ALVO_1 | ALVO_2 | STOP | EM_ABERTO | SEM_SINAL | SEM_ENTRADA
    resultado_detalhe     TEXT,
    candles_ate_resultado INT,
    avaliado_em           TIMESTAMPTZ,
    avaliado_ate          TIMESTAMPTZ,        -- até que vela o desfecho foi conferido
    -- Execução real (2026-08-06). `entrada` é o FECHAMENTO da vela do sinal;
    -- `preco_fill` é a ABERTURA da vela seguinte, que é onde uma ordem a
    -- mercado disparada pelo sinal realmente executaria. Os dois divergem
    -- exatamente nos gaps — e era ali que o modelo antigo se dava um preço
    -- que ninguém conseguiu. `r_realizado` já vem líquido de custo.
    preco_fill            DOUBLE PRECISION,
    r_realizado           DOUBLE PRECISION
);

-- Colunas novas em base já existente: o schema é aplicado por `migrate.py` a
-- cada deploy e precisa ser idempotente, então nada de recriar a tabela.
ALTER TABLE signals ADD COLUMN IF NOT EXISTS preco_fill  DOUBLE PRECISION;
ALTER TABLE signals ADD COLUMN IF NOT EXISTS r_realizado DOUBLE PRECISION;

-- ÚNICO índice obrigatório: é a chave de deduplicação E o alvo do
-- ON CONFLICT do worker. Sem ele, (a) o worker recriaria a mesma linha a
-- cada varredura e a cada reinício de pod, e (b) dois cliques no botão
-- "salvar sinal" do Streamlit virariam dois registros — nos dois casos
-- inflando em silêncio toda taxa de acerto calculada depois.
--
-- `origem` entra na chave de propósito: um sinal salvo à mão NÃO deve
-- colidir com o do worker pra mesma vela (distinguir os dois é
-- requisito), mas dois cliques na mesma leitura continuam deduplicando.
CREATE UNIQUE INDEX IF NOT EXISTS signals_dedup_idx
    ON signals (symbol, timeframe, modalidade, candle_time, perfil, origem);

-- Índice PARCIAL pro segundo passe (avaliação de desfecho), cuja consulta
-- é "todo sinal ainda sem desfecho final". Esse conjunto é uma fração
-- mínima da tabela e ENCOLHE com o tempo — a linha sai do índice assim
-- que o desfecho é gravado. Sem ele, o passe vira um seq scan que cresce
-- pra sempre.
CREATE INDEX IF NOT EXISTS signals_pendentes_idx
    ON signals (candle_time)
    WHERE resultado IS NULL OR resultado = 'EM_ABERTO';

-- ------------------------------------------------------------------
-- Feedback do usuario sobre sinais (ACOMPANHAR, OPERAR, IGNORAR, etc).
-- Origem pode ser web (Streamlit), whatsapp ou telegram (webhooks).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS signal_feedback (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    signal_id   BIGINT NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
    acao        TEXT NOT NULL,
    origem      TEXT NOT NULL DEFAULT 'web',
    nota        TEXT,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT signal_feedback_acao_chk
        CHECK (acao IN ('ACOMPANHAR', 'OPERAR', 'IGNORAR', 'OPEREI', 'CANCELEI')),
    CONSTRAINT signal_feedback_origem_chk
        CHECK (origem IN ('web', 'whatsapp', 'telegram', 'auto'))
);

-- `auto` chegou depois, com o worker aplicando as regras de
-- `auto_acompanhamento`. O CREATE TABLE acima só vale pra banco novo — em
-- banco existente a constraint antiga continua lá e rejeitaria a escrita do
-- worker, então ela é recriada explicitamente. Mesma razão dos ALTER de
-- `signals` mais acima.
--
-- A distinção importa e não é burocracia: um ACOMPANHAR que o worker gerou
-- por regra NÃO é alguém tendo escolhido acompanhar aquele sinal. Misturar
-- os dois na mesma origem destruiria justamente a medição que o feedback
-- existe pra permitir ("acertei mais no que EU escolhi seguir?"), do mesmo
-- jeito que `origem='consulta'` existe em `signals` pra não deixar uma
-- consulta passar por medição.
ALTER TABLE signal_feedback DROP CONSTRAINT IF EXISTS signal_feedback_origem_chk;
ALTER TABLE signal_feedback ADD CONSTRAINT signal_feedback_origem_chk
    CHECK (origem IN ('web', 'whatsapp', 'telegram', 'auto'));

CREATE INDEX IF NOT EXISTS signal_feedback_signal_id_idx ON signal_feedback(signal_id);
CREATE INDEX IF NOT EXISTS signal_feedback_acao_idx ON signal_feedback(acao);
CREATE INDEX IF NOT EXISTS signal_feedback_criado_idx ON signal_feedback(criado_em DESC);

-- NÃO acrescente índice pras consultas de assertividade sem medir antes.
--
-- Elas agregam a tabela inteira com um recorte de data e quebram por
-- modalidade, timeframe, símbolo, direção, faixa de score e confirmação
-- MTF. Em 10⁴–10⁵ linhas isso é seq scan + hash aggregate na casa dos
-- milissegundos; um índice por recorte seriam SEIS índices pagos em toda
-- escrita pra economizar nada mensurável. Foi exatamente um índice não
-- medido que custou 13 MB e uma escrita extra por update na `candles`
-- (ver o comentário lá em cima).

-- Acesso de leitura e escrita pra role `fcar`, que vive no mesmo TimescaleDB
-- compartilhado e consome estes dados. Fica aqui, e não só aplicado à mão no
-- banco, porque senão sumiria em silêncio num recreate do database — e o
-- sintoma seria "permission denied" numa aplicação que não tem nada a ver com
-- este repo.
--
-- O DO block existe porque a role é de OUTRA aplicação: num cluster onde o
-- fcar não exista, a migration não pode falhar por causa disso.
--
-- O ALTER DEFAULT PRIVILEGES é o que impede a próxima tabela criada aqui de
-- nascer inacessível pro fcar. Sem ele, cada tabela nova exigiria um GRANT
-- manual que ninguém lembraria de fazer.
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'fcar') THEN
    GRANT USAGE ON SCHEMA public TO fcar;
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fcar;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fcar;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fcar;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
      GRANT USAGE, SELECT ON SEQUENCES TO fcar;
  END IF;
END
$$;

-- ------------------------------------------------------------------
-- Regras de auto-acompanhamento. O worker-acoes consulta esta tabela
-- e gera feedback automático (`acao = 'ACOMPANHAR'`) para os sinais
-- cujo perfil + modalidade batem com uma regra ativa.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auto_acompanhamento (
    perfil      TEXT NOT NULL,
    modalidade  TEXT NOT NULL,
    ativo       BOOLEAN NOT NULL DEFAULT true,
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (perfil, modalidade)
);

-- ------------------------------------------------------------------
-- Regras de ordem automática. Mesma forma de `auto_acompanhamento`:
-- "para este perfil + modalidade + timeframe, mande ordem".
--
-- `timeframe` faz parte da chave, e não é detalhe: uma regra só por
-- (perfil, modalidade) casaria com o MESMO sinal em M15, H1, H4 e D1, e
-- abriria quatro posições no mesmo ativo achando que abriu uma.
--
-- `risco_maximo` em REAIS, não quantidade: a quantidade sai da distância
-- até o stop DO SINAL, então toda operação arrisca o mesmo valor
-- independente da volatilidade do papel. Guardar quantidade fixa faria o
-- risco variar de 35 a 120 reais entre ativos sem ninguém escolher isso.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auto_ordem (
    perfil        TEXT NOT NULL,
    modalidade    TEXT NOT NULL,
    timeframe     TEXT NOT NULL,
    risco_maximo  NUMERIC NOT NULL,
    ativo         BOOLEAN NOT NULL DEFAULT true,
    criado_em     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (perfil, modalidade, timeframe),
    CONSTRAINT auto_ordem_risco_chk CHECK (risco_maximo > 0)
);

-- ------------------------------------------------------------------
-- Auditoria de ordens enviadas. É também o mecanismo de "não manda duas
-- vezes": `signal_id` é ÚNICO, e o executor RESERVA a linha antes de
-- mandar a ordem.
--
-- A ordem das operações é o ponto. Gravar depois de enviar deixaria a
-- janela em que a ordem foi executada e a linha não existe — e um reinício
-- ali dentro mandaria a segunda ordem para o mesmo sinal. Reservando
-- antes, um `duplicado` já responde "alguém está cuidando disso", e o pior
-- caso vira uma linha ENVIANDO órfã (visível) em vez de posição dobrada
-- (invisível até o extrato).
--
-- Por isso `status` não tem DEFAULT 'ENVIADA': o estado inicial é a
-- reserva, e a transição para ENVIADA/FALHOU é o segundo passo.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ordens (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    signal_id         BIGINT NOT NULL UNIQUE REFERENCES signals(id) ON DELETE CASCADE,
    symbol            TEXT NOT NULL,
    direcao           TEXT NOT NULL,
    volume            NUMERIC,
    risco_maximo      NUMERIC,
    preco_pedido      DOUBLE PRECISION,
    preco_executado   DOUBLE PRECISION,
    stop              DOUBLE PRECISION,
    alvo              DOUBLE PRECISION,
    conta             BIGINT,
    servidor          TEXT,
    tipo_conta        TEXT,
    ticket            BIGINT,
    status            TEXT NOT NULL,
    retcode           INTEGER,
    mensagem          TEXT,
    criado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
    enviado_em        TIMESTAMPTZ,
    CONSTRAINT ordens_status_chk
        CHECK (status IN ('ENVIANDO', 'ENVIADA', 'FALHOU', 'RECUSADA'))
);

CREATE INDEX IF NOT EXISTS ordens_criado_idx ON ordens(criado_em DESC);
CREATE INDEX IF NOT EXISTS ordens_symbol_idx ON ordens(symbol, criado_em DESC);

-- ------------------------------------------------------------------
-- Desfecho da posição (2026-08-12). Preenchido pela reconciliação do
-- `executor`, que lê o MetaTrader 5 — a ÚNICA fonte que é dinheiro.
--
-- Por que não derivar de `signals.resultado`: aquilo é o desfecho do
-- SINAL, com entrada modelada em `preco_fill` (a abertura da vela
-- seguinte). A ordem executou noutro preço, com quantidade arredondada ao
-- lote, e pode ter sido fechada à mão, parcialmente, ou com slippage.
-- Calcular "saiu exato no stop/alvo" inventaria os três casos.
--
-- `status` NÃO ganha valor novo, e isso é deliberado: `status` é o ciclo de
-- vida do ENVIO (reservou / saiu / a corretora recusou / as travas
-- barraram), enquanto aberta-fechada é ortogonal — uma ordem fechada
-- continua tendo sido ENVIADA. Enfiar FECHADA no mesmo campo obrigaria
-- toda consulta de auditoria de envio a listar dois valores para dizer "saiu
-- da máquina".
--
-- ⚠️ `resultado_reais` MUDA DE SENTIDO com `fechado_em`: enquanto ele for
-- NULL, é o resultado NÃO REALIZADO da posição aberta, reescrito a cada
-- passada da reconciliação; depois, é o valor final, líquido de corretagem
-- e swap. Um conceito só ("lucro da posição"), e `fechado_em` diz se
-- acabou — mas quem AGREGA tem que separar os dois, ou soma dinheiro que
-- ainda pode virar prejuízo. Ver `GET /ordens/stats`, que devolve
-- `resultado_reais` e `aberto_reais` em campos distintos.
-- ------------------------------------------------------------------
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS fechado_em      TIMESTAMPTZ;
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS preco_saida     DOUBLE PRECISION;
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS volume_saida    NUMERIC;
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS resultado_reais NUMERIC;
-- STOP | ALVO | MANUAL | EXPERT | MARGEM | OUTRO — traduzido do
-- `DEAL_REASON_*` do deal de saída. Distinguir MANUAL é o ponto: uma regra
-- cujo resultado veio de fechamento à mão não está sendo medida, está sendo
-- pilotada, e misturar as duas coisas corrompe a comparação entre regras.
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS motivo_saida    TEXT;
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS conciliado_em   TIMESTAMPTZ;

-- Ordem de VALIDAÇÃO: saiu de verdade, executou de verdade, e mesmo assim
-- não mede regra nenhuma (2026-08-12).
--
-- Marcar em vez de apagar, no mesmo estilo de `origem='consulta'` nos
-- payloads do /analisar e de `feedback_origem='auto'`: o repositório
-- consistentemente ROTULA o que não deve entrar na conta em vez de esconder.
-- A primeira ordem de teste foi apagada na mão, e apagar de uma tabela de
-- auditoria some com um evento que aconteceu — além de exigir que alguém se
-- lembre de fazer a limpeza toda vez.
--
-- Quem grava é quem envia fora de regra (ver `executor._processar(teste=)`).
-- `GET /ordens` e `GET /ordens/stats` filtram estas linhas POR PADRÃO; quem
-- quiser vê-las pede `incluir_testes=true`.
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS teste BOOLEAN NOT NULL DEFAULT false;

-- Quanto o preenchimento andou em relação à `entrada` modelada do sinal,
-- medido em R planejado (2026-08-12). Positivo = preencheu pior.
--
-- É a variável que explicou o prejuízo das 39 primeiras ordens e que não
-- existia em coluna nenhuma: -R$ 1.369 e 31% de acerto nas ordens contra
-- +0,17R e 61% nos MESMOS recortes da tabela `signals`. A diferença é que a
-- ordem sai minutos depois do fechamento da vela, a outro preço, com os
-- níveis calculados sobre o preço antigo — e as duas caudas do desvio matam
-- de jeitos opostos: para um lado o stop fica dentro do ruído (6 ordens
-- abaixo de 0,6 ATR, 6 stops, -1,00R cada), para o outro o alvo fica a um
-- centavo e a ordem fecha como "ALVO" pagando +0,02R.
--
-- Em R, e não em reais nem em porcentagem, porque é a única unidade
-- comparável entre um MGLU3 de R$ 4 e um VALE3 de R$ 74. Descobrir isso
-- exigiu cruzar `ordens` com `signals` à mão; com a coluna, um recorte de
-- `/ordens/stats` responde.
ALTER TABLE ordens ADD COLUMN IF NOT EXISTS desvio_entrada_r DOUBLE PRECISION;

-- Índice PARCIAL pelo mesmo motivo do `signals_pendentes_idx`: o alvo da
-- reconciliação é "o que saiu e ainda não fechou", um conjunto que ENCOLHE
-- sozinho — a linha sai do índice assim que o fechamento é gravado.
CREATE INDEX IF NOT EXISTS ordens_abertas_idx ON ordens (id)
    WHERE status = 'ENVIADA' AND fechado_em IS NULL;
