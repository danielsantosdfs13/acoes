# Imagem `acoes-backend`: carrega os quatro entrypoints do backend, e cada
# workload no k8s escolhe o seu via `command:` —
#
#   Deployment processor  uvicorn processor:app
#   Deployment api        uvicorn api:app
#   Deployment analyzer   python analyzer.py
#   Job migrate           python migrate.py
#
# Mesmo padrão do fcar, onde `fcar-backend` carrega server, domain-reconciler
# e migrate. Manter a migration na MESMA imagem do app é o que garante que
# schema e código nunca saiam de sincronia entre um deploy e outro.
#
# Build a partir da RAIZ do repo (igual ao Dockerfile.streamlit):
#   docker build -f backend/Dockerfile -t acoes-backend:<tag> .
#
# O contexto é a raiz, e não `backend/`, porque o `analyzer.py` roda o motor
# de análise — e `daytrade_smc.py` mora na raiz, fora de qualquer contexto
# limitado a `backend/`. Ver `.dockerignore` pro que fica de fora.
#
# Sem CMD de propósito: os manifests sempre passam o comando explicitamente,
# e um default aqui só mascararia um manifest incompleto.
FROM python:3.12-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/auth.py backend/db.py backend/models.py \
     backend/processor.py backend/api.py backend/analyzer.py \
     backend/migrate.py backend/schema.sql ./
COPY daytrade_smc.py ./

EXPOSE 8000
