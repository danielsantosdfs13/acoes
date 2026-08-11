"""Pool de conexão com o Postgres/TimescaleDB — usado por todas as rotas do processor."""

from __future__ import annotations

import logging
import os

from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

DATABASE_URL = os.environ["DATABASE_URL"]

TIMEZONE = "America/Sao_Paulo"


def _configure_conn(conn):
    """Configura uma conexão nova antes de entrar no pool."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET timezone TO 'America/Sao_Paulo'")
    except Exception:
        log.warning("Não foi possível definir timezone para %s", TIMEZONE, exc_info=True)


pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    open=True,
    configure=_configure_conn,
    # Toda conexão nova recebe SET timezone antes de ser emprestada.
)


def get_conn():
    with pool.connection() as conn:
        yield conn
