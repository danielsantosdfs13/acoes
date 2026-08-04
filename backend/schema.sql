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

-- O padrão de leitura é sempre "últimas N linhas de um symbol+timeframe,
-- time DESC" — este índice faz isso ser um index scan puro.
CREATE INDEX IF NOT EXISTS candles_symbol_tf_time_desc_idx ON candles (symbol, timeframe, time DESC);

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
