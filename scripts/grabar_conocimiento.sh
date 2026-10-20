#!/usr/bin/env bash
# Graba los embeddings del golden set de recuperación del pack de conocimiento (ADR-0145 §7).
#
# Es una llamada REAL a OpenAI `text-embedding-3-small`: ≈ 50.000 tokens ≈ US$0,001. Solo con el OK
# del dueño (memoria del proyecto: costo de LLM). Levanta un Postgres y un Redis efímeros con la
# imagen del CI, migra, corre `evals/conocimiento.py --grabar` y los borra al salir. Deja
# `evals/conocimiento/grabacion.json`, que se commitea: desde ahí el CI y develop corren en replay.
#
# Uso:
#   OPENAI_API_KEY=sk-... bash scripts/grabar_conocimiento.sh
set -euo pipefail

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "Falta OPENAI_API_KEY (la llave de embeddings; no se guarda en ningún archivo)." >&2
  exit 2
fi

IMAGEN="${IMAGEN_POSTGRES:-ghcr.io/devrochy/solvershub-postgres:pg17-postgis}"
SUFIJO="$(date +%s)"
PG="kb-grabar-pg-${SUFIJO}"
RD="kb-grabar-redis-${SUFIJO}"
TENANTS="$(mktemp -d)"

limpiar() {
  docker rm -f "$PG" "$RD" >/dev/null 2>&1 || true
  rm -rf "$TENANTS"
}
trap limpiar EXIT

docker run -d --name "$PG" -e POSTGRES_USER=agent_core -e POSTGRES_PASSWORD=agent_core \
  -e POSTGRES_DB=agent_core -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data "$IMAGEN" >/dev/null
docker run -d --name "$RD" -p 127.0.0.1::6379 redis:7-alpine >/dev/null
PUERTO_PG="$(docker port "$PG" 5432/tcp | head -1 | cut -d: -f2)"
PUERTO_RD="$(docker port "$RD" 6379/tcp | head -1 | cut -d: -f2)"
until docker exec "$PG" pg_isready -U agent_core >/dev/null 2>&1; do sleep 1; done

# Mismas claves de prueba que el CI (`.github/workflows/ci.yml`): no son secretos reales.
export APP_ENV=development
export DATABASE_URL="postgresql+asyncpg://app_runtime:app_runtime@localhost:${PUERTO_PG}/agent_core"
export MIGRATIONS_DATABASE_URL="postgresql+asyncpg://agent_core:agent_core@localhost:${PUERTO_PG}/agent_core"
export REDIS_URL="redis://localhost:${PUERTO_RD}/0"
export MASTER_ENCRYPTION_KEY="GpsiZ2sgMd4SvLHGp4Bs5xdaQLlQ1cByyK6DCNxVwL4="
export TOKEN_MASTER_KEYS='{"k1":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}'
export TOKEN_ACTIVE_KEY_ID=k1
export APP_SECRET_KEY=ci-only-not-a-secret-app-key
export WHATSAPP_APP_SECRET=ci-meta-app-secret-not-real
export ADMIN_PASSWORD=admin123
export TENANTS_ROOT="$TENANTS"
export LLM_API_KEY=""

PY="${PYTHON:-python}"
"$PY" -m cli.main db migrate >/dev/null
"$PY" scripts/init_db.py >/dev/null
"$PY" evals/conocimiento.py --grabar
echo "Grabación lista: evals/conocimiento/grabacion.json (commitearla)."
