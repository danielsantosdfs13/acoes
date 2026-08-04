#!/usr/bin/env bash
# Provisionamento idempotente do database `daytrade` no TimescaleDB
# compartilhado (StatefulSet default/timescaledb no k3s).
#
# Adaptado de platform-fcar/scripts/init-tenant-db.sh — mesma instância,
# mesmo padrão de isolamento (um database por app, role dedicada).
#
# Fica FORA do GitOps de propósito: criar role e database exige superusuário,
# e o `pg_hba` da instância bloqueia `postgres` remoto justamente pra isso não
# virar rotina. Roda por `kubectl exec`, pelo socket local do pod.
#
# Uso: scripts/init-tenant-db.sh <db_name> <db_user> <db_password>
set -euo pipefail

DB_NAME="${1:?usage: init-tenant-db.sh <db_name> <db_user> <db_password>}"
DB_USER="${2:?usage: init-tenant-db.sh <db_name> <db_user> <db_password>}"
DB_PASSWORD="${3:?usage: init-tenant-db.sh <db_name> <db_user> <db_password>}"

NAMESPACE=default
POD=timescaledb-0

# A senha é aplicada SEMPRE, não só na criação. A versão original (herdada do
# platform-fcar) só fazia CREATE dentro de um IF NOT EXISTS, então rodar o
# script com uma senha nova contra uma role já existente não dava erro nenhum
# e mesmo assim não trocava nada — o Secret e o banco ficavam divergentes, e o
# sintoma só aparecia depois, como "password authentication failed" no pod.
echo "==> Garantindo a role '${DB_USER}' (e sincronizando a senha)"
kubectl exec -n "${NAMESPACE}" "${POD}" -- psql -U postgres -v ON_ERROR_STOP=1 -c "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';
  ELSE
    ALTER ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;"

echo "==> Garantindo o database '${DB_NAME}'"
DB_EXISTS=$(kubectl exec -n "${NAMESPACE}" "${POD}" -- psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '${DB_NAME}'")
if [ "${DB_EXISTS}" != "1" ]; then
  kubectl exec -n "${NAMESPACE}" "${POD}" -- psql -U postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER};"
else
  echo "    já existe, pulando"
fi

# A extensão precisa existir ANTES do migrate.py, que chama create_hypertable().
echo "==> Garantindo a extensão timescaledb em '${DB_NAME}'"
kubectl exec -n "${NAMESPACE}" "${POD}" -- psql -U postgres -d "${DB_NAME}" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo "==> Pronto: database '${DB_NAME}' disponível para a role '${DB_USER}'"
