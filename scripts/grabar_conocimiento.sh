#!/usr/bin/env bash
# Graba los embeddings del golden set de recuperación del pack de conocimiento (ADR-0145 §7).
#
# Es una llamada REAL a OpenAI `text-embedding-3-small`: ≈ 50.000 tokens ≈ US$0,001. Solo con el OK
# del dueño (memoria del proyecto: costo de LLM). Levanta un Postgres y un Redis efímeros con la
# imagen del CI, migra, corre `evals/conocimiento.py --grabar` y los borra al salir. Deja
# `evals/conocimiento/grabacion.json`, que se commitea: desde ahí el CI y develop corren en replay.
#
# La llave es `OPENAI_API_KEY`: del entorno o del `.env` del checkout, como `LLM_API_KEY` en
# `scripts/gate_evals.sh record`. DeepSeek (`LLM_API_KEY`) no tiene API de embeddings.
#
# Uso (desde la raíz del repo, donde está el .env):
#   bash scripts/grabar_conocimiento.sh
# Desde un worktree sin .env, con el .env de otro checkout (el código sigue siendo el de este):
#   ENV_DIR=../solvershub-backend bash scripts/grabar_conocimiento.sh
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
# El código de ESTE checkout primero (core, contratos y packs), aunque el venv sea de otro checkout.
# Los packs que ese venv no tiene instalados (p. ej. pack_knowledge) se registran con un dist-info
# temporal, para que `plugin_manager.scan()` los encuentre y corra sus migraciones.
METADATOS="$(mktemp -d)"
trap 'limpiar; rm -rf "$METADATOS"' EXIT
PAQUETES="$(ls -d "$RAIZ"/packages/*/ | tr '\n' ':')"
export PYTHONPATH="${RAIZ}:${PAQUETES}${METADATOS}${PYTHONPATH:+:$PYTHONPATH}"
"$PY" - "$RAIZ" "$METADATOS" <<'PYEOF'
import importlib.metadata as md
import pathlib
import sys
import tomllib

raiz, destino = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
for pyproject in sorted(raiz.glob("packages/pack-*/pyproject.toml")):
    proyecto = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
    try:
        md.distribution(proyecto["name"])
        continue
    except md.PackageNotFoundError:
        pass
    puntos = proyecto.get("entry-points", {}).get("agent_core.plugins", {})
    info = destino / f"{proyecto['name'].replace('-', '_')}-{proyecto['version']}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {proyecto['name']}\nVersion: {proyecto['version']}\n")
    (info / "entry_points.txt").write_text("[agent_core.plugins]\n" + "".join(f"{k} = {v}\n" for k, v in puntos.items()))
    (info / "RECORD").write_text("")
PYEOF
# `get_settings()` lee el `.env` del directorio actual: el de ENV_DIR si se indica.
cd "${ENV_DIR:-$RAIZ}"
echo "Migrando la base efímera…"
"$PY" -m cli.main db migrate >"$METADATOS/migrar.log" 2>&1 || { cat "$METADATOS/migrar.log"; exit 1; }
"$PY" "$RAIZ/scripts/init_db.py" >"$METADATOS/init.log" 2>&1 || { cat "$METADATOS/init.log"; exit 1; }
echo "Grabando los embeddings del golden set…"
"$PY" "$RAIZ/evals/conocimiento.py" --grabar
echo "Grabación lista: $RAIZ/evals/conocimiento/grabacion.json (commitearla)."
