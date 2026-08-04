# Imagem `acoes-backend`: carrega os três entrypoints do backend, e cada
# workload no k8s escolhe o seu via `command:` —
#
#   Deployment processor  uvicorn processor:app
#   Deployment api        uvicorn api:app
#   Job migrate           python migrate.py
#
# Mesmo padrão do fcar, onde `fcar-backend` carrega server, domain-reconciler
# e migrate. Manter a migration na MESMA imagem do app é o que garante que
# schema e código nunca saiam de sincronia entre um deploy e outro.
#
# Sem CMD de propósito: os manifests sempre passam o comando explicitamente,
# e um default aqui só mascararia um manifest incompleto.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY auth.py db.py models.py processor.py api.py migrate.py schema.sql ./

EXPOSE 8000
