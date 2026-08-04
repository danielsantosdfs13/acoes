"""Pool de conexão com o Postgres/TimescaleDB — usado por todas as rotas do processor."""

from __future__ import annotations

import os

from psycopg_pool import ConnectionPool

DATABASE_URL = os.environ["DATABASE_URL"]

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=True)


def get_conn():
    with pool.connection() as conn:
        yield conn
