"""
backend/migrate.py

Aplica `schema.sql` no database `daytrade`. Roda como hook PreSync do
ArgoCD (ver homelab/applications/acoes/job-migrate.yaml), usando a MESMA
imagem dos Deployments `processor` e `api` — só muda o `command:`, mesmo
padrão do fcar, onde `fcar-backend` carrega app e migration juntos.

Como o hook dispara a cada sync, `schema.sql` é escrito para ser
idempotente. Este script não guarda versão nem histórico: o arquivo
inteiro é o estado desejado, e reaplicá-lo é um no-op.

Uso: python migrate.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERRO: DATABASE_URL não definida.", file=sys.stderr)
        return 1

    sql = SCHEMA.read_text(encoding="utf-8")
    print(f">> aplicando {SCHEMA.name} ({len(sql)} bytes)")

    # Uma transação só: ou o schema inteiro entra, ou nada entra. O commit
    # é implícito na saída do context manager da conexão (psycopg3).
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.cursor() as cur:
        cur.execute(sql)

    print(">> schema aplicado")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
