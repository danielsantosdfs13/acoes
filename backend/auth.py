"""Checagem da X-API-Key, compartilhada pelos dois entrypoints.

`ACOES_API_KEY` vazia pula a checagem — o que era aceitável quando o
serviço só existia dentro da LAN do homelab. Com a `api` publicada em
`acoes-api.dondon.services`, preenchê-la passou a ser o único controle
sobre as rotas de escrita; ver docs/homelab-pipeline.md, seção
"Exposição".
"""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader

API_KEY = os.environ.get("ACOES_API_KEY") or None

# `APIKeyHeader` e não `Header(...)` cru: os dois leem o mesmo cabeçalho e se
# comportam igual em runtime, mas só este aparece como `securityScheme` no
# `/openapi.json`. Desde 2026-08-10 essa spec alimenta a conversão OpenAPI→MCP
# do agentgateway (ver o bloco grande no topo de `api.py`), e um esquema de
# autenticação ausente da spec vira uma tool que o gateway acha que é pública.
#
# `auto_error=False` é obrigatório aqui: com o default `True` o FastAPI
# responderia 403 sozinho quando o cabeçalho falta, e isso passaria por cima do
# caso "ACOES_API_KEY vazia = checagem desligada", que é o que mantém o serviço
# utilizável na LAN sem segredo configurado.
_cabecalho_api_key = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(x_api_key: str | None = Depends(_cabecalho_api_key)) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou ausente.")
