"""Checagem da X-API-Key, compartilhada pelos dois entrypoints.

`ACOES_API_KEY` vazia pula a checagem — o que era aceitável quando o
serviço só existia dentro da LAN do homelab. Com a `api` publicada em
`acoes-api.dondon.services`, preenchê-la passou a ser o único controle
sobre as rotas de escrita; ver docs/homelab-pipeline.md, seção
"Exposição".
"""

from __future__ import annotations

import os

from fastapi import Header, HTTPException

API_KEY = os.environ.get("ACOES_API_KEY") or None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou ausente.")
