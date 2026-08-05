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
    timeframe   TEXT             NOT NULL,   -- 'M15' | 'H1' | 'H4' | 'D1' | 'W1'
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
-- (11 símbolos × 4 timeframes × 5 modalidades, uma linha por VELA e não
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
    modalidade        TEXT        NOT NULL,   -- Confluência | SMC | Price Action | Médias Móveis | VWAP
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
    resultado             TEXT,               -- ALVO_1 | ALVO_2 | STOP | EM_ABERTO | SEM_SINAL
    resultado_detalhe     TEXT,
    candles_ate_resultado INT,
    avaliado_em           TIMESTAMPTZ,
    avaliado_ate          TIMESTAMPTZ         -- até que vela o desfecho foi conferido
);

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
