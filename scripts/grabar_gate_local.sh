#!/usr/bin/env bash
# Graba el gate de evals del CI contra el LLM REAL y comprueba el replay (adenda de ADR-0120).
#
#   export LLM_API_KEY=...                      # en la terminal; el script nunca la imprime
#   VENV=/ruta/al/.venv bash scripts/grabar_gate_local.sh
#
# Gasta saldo (~9 llamadas, ~0,01 USD): solo con OK del dueño. Pasos, cada uno en un stack
# efímero NUEVO (Postgres pg17 + PostGIS y Redis en Docker, puertos 55571/56511), con las mismas
# variables de prueba que `ci.yml` y la base migrada y sembrada como el job `evals-replay`:
#   1. `scripts/gate_evals.sh record` → grabaciones en evals/cassettes/.
#   2. Dos veces `scripts/gate_evals.sh replay` sin LLM_API_KEY, cada una en una base nueva.
#   3. Revisa que ninguna grabación ni el log tengan la clave o las claves de prueba.
#
# Ensayos sin LLM real (no gastan, graban en un directorio temporal, no en el repo):
#   bash scripts/grabar_gate_local.sh --ensayo        # proveedor apuntando a 127.0.0.1:9 (cerrado): debe cortar
#   bash scripts/grabar_gate_local.sh --ensayo-demo   # FakeProvider de DEMO_MODE: recorre el camino completo
#
# Sale con 0 si grabó y los dos replays coinciden con la grabación. Corta ante cualquier
# `excepción:`/Traceback de la grabación (exit 4) o si no quedó ninguna grabación (exit 5).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
ENSAYO="${1:-}"
case "$ENSAYO" in "" | --ensayo | --ensayo-demo) ;; *) echo "uso: $0 [--ensayo|--ensayo-demo]" >&2; exit 2 ;; esac

PG_PORT="${GATE_PG_PORT:-55571}"
RD_PORT="${GATE_RD_PORT:-56511}"
PG_IMAGEN="${GATE_PG_IMAGEN:-solvershub-postgres:pg17-postgis}"
# Nombres por corrida: `limpiar` nunca borra los contenedores de otra corrida u otra sesión.
PG="grabar-gate-pg-$$"
RD="grabar-gate-redis-$$"
TRABAJO="$(mktemp -d -t grabar-gate-XXXXXX)"
LOG="$TRABAJO/grabacion.log"

# ---------------------------------------------------------------- entorno idéntico al CI
# Un `.env` en el directorio cambiaría la config (modelo, demo, proveedor) frente al CI y las
# grabaciones no coincidirían: se corre desde un checkout o worktree sin `.env`.
if [ -e .env ]; then
  echo "Hay un .env en $REPO: córrelo desde un worktree sin .env (sus valores cambiarían el prompt frente al CI)." >&2
  exit 2
fi
CLAVE="${LLM_API_KEY:-}"
# Fuera todo lo que pueda cambiar el request frente al CI (LLM_*, demo, embeddings, tenants).
for v in $(env | sed -n 's/^\(LLM_[A-Z_]*\|DEMO_MODE\|EMBEDDING_[A-Z_]*\|OPENAI_API_KEY\|ANTHROPIC_API_KEY\|TENANTS_ROOT\)=.*/\1/p'); do
  unset "$v"
done
case "$ENSAYO" in
  "")
    if [ -z "$CLAVE" ]; then
      echo "Falta LLM_API_KEY en esta terminal: export LLM_API_KEY=... y vuelve a correr." >&2
      exit 2
    fi
    export LLM_API_KEY="$CLAVE"
    ;;
  --ensayo)
    export LLM_API_KEY="dummy-no-real" LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://127.0.0.1:9
    export LLM_CASSETTE_DIR="$TRABAJO/cassettes"
    ;;
  --ensayo-demo)
    export LLM_API_KEY="dummy-no-real" DEMO_MODE=true LLM_PROVIDER=openai_compatible LLM_BASE_URL=http://127.0.0.1:9
    export LLM_CASSETTE_DIR="$TRABAJO/cassettes"
    ;;
esac
unset CLAVE

if [ -n "${VENV:-}" ]; then export PATH="$VENV/bin:$PATH"; fi
export PYTHONPATH="$REPO:$(ls -d "$REPO"/packages/*/ | tr '\n' :)"
export DATABASE_URL="postgresql+asyncpg://app_runtime:app_runtime@localhost:${PG_PORT}/agent_core"
export MIGRATIONS_DATABASE_URL="postgresql+asyncpg://agent_core:agent_core@localhost:${PG_PORT}/agent_core"
export REDIS_URL="redis://localhost:${RD_PORT}/0"
# Las mismas claves de PRUEBA que el bloque `env` de ci.yml (no son secretos reales).
export MASTER_ENCRYPTION_KEY='GpsiZ2sgMd4SvLHGp4Bs5xdaQLlQ1cByyK6DCNxVwL4='
export TOKEN_MASTER_KEYS='{"k1":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}'
export TOKEN_ACTIVE_KEY_ID='k1'
export APP_SECRET_KEY='ci-only-not-a-secret-app-key'
export WHATSAPP_APP_SECRET='ci-meta-app-secret-not-real'
export APP_ENV='development'

python -c 'import pack_commerce, core, sys; sys.exit(0 if pack_commerce.__file__.startswith(sys.argv[1]) else 1)' "$REPO" || {
  echo "Python no importa el código de $REPO (¿VENV correcto?)." >&2
  exit 2
}

# Último filtro: aunque nada la imprime, la salida pasa por un reemplazo de la clave.
tapar() {
  python -u -c 'import os, sys
k = os.environ.get("LLM_API_KEY") or "\0"
for linea in sys.stdin:
    sys.stdout.write(linea.replace(k, "**********"))
    sys.stdout.flush()'
}

# ---------------------------------------------------------------- stack efímero
# Una sola grabación a la vez en la máquina: dos corridas sobre los mismos puertos se cambian
# la base entre sí. La base nueva todavía no tiene el rol `app_runtime` y Postgres lo reporta
# como `password authentication failed for user "app_runtime"`.
exec 9>"${TMPDIR:-/tmp}/grabar-gate-local.lock"
if ! flock -n 9; then
  echo "Hay otra grabación del gate en curso en esta máquina: espera a que termine." >&2
  exit 2
fi
limpiar() { docker rm -f "$PG" "$RD" >/dev/null 2>&1 || true; }
trap limpiar EXIT

puerto_libre() {
  ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

stack_nuevo() {
  limpiar
  # Otro stack en los mismos puertos (otra sesión) cambiaría la base en medio de la corrida.
  for p in "$PG_PORT" "$RD_PORT"; do
    if ! puerto_libre "$p"; then
      echo "El puerto $p está ocupado por otro proceso o contenedor: libéralo o usa GATE_PG_PORT/GATE_RD_PORT." >&2
      exit 2
    fi
  done
  docker run -d --name "$PG" -p "127.0.0.1:${PG_PORT}:5432" \
    -e POSTGRES_USER=agent_core -e POSTGRES_PASSWORD=agent_core -e POSTGRES_DB=agent_core "$PG_IMAGEN" >/dev/null
  docker run -d --name "$RD" -p "127.0.0.1:${RD_PORT}:6379" redis:7-alpine >/dev/null
  # La imagen inicializa con un servidor temporal y reinicia: esperar el SEGUNDO "ready".
  for _ in $(seq 1 90); do
    [ "$(docker logs "$PG" 2>&1 | grep -c 'ready to accept connections')" -ge 2 ] && break
    sleep 1
  done
  until docker exec "$PG" pg_isready -U agent_core -h 127.0.0.1 >/dev/null 2>&1; do sleep 1; done
  until docker exec "$RD" redis-cli ping >/dev/null 2>&1; do sleep 1; done
  agent-core db migrate >/dev/null
  ADMIN_PASSWORD=admin123 python scripts/init_db.py >/dev/null
}

resultados() { grep -E '^\s+[✓✗]' "$1" || true; }

# ---------------------------------------------------------------- 1. grabación
echo "== 1/3 Base nueva y grabación (${ENSAYO:-LLM REAL}) — log: $LOG"
stack_nuevo
touch "$TRABAJO/marca"  # solo cuentan las grabaciones escritas por esta corrida
estado=0
bash scripts/gate_evals.sh record 2>&1 | tapar | tee "$LOG" || estado=${PIPESTATUS[0]}
echo "Grabación terminó con código $estado (1 = algún escenario falló por la respuesta del modelo; igual quedó grabado)."
if grep -qE "excepción:|Traceback" "$LOG"; then
  echo "La grabación tuvo excepciones (ver $LOG): no sigo." >&2
  exit 4
fi
if [ "$estado" -ne 0 ] && [ "$estado" -ne 1 ]; then
  echo "Error de grabación (exit $estado): no sigo." >&2
  exit "$estado"
fi
DESTINO="${LLM_CASSETTE_DIR:-$REPO/evals/cassettes}"
if [ -z "$(find "$DESTINO" -name '*.json' -newer "$TRABAJO/marca" 2>/dev/null)" ]; then
  echo "No quedó ninguna grabación en $DESTINO: no sigo." >&2
  exit 5
fi

# ---------------------------------------------------------------- 2. dos replays sin clave
for i in 1 2; do
  echo "== $((i + 1))/3 Base nueva y replay sin LLM_API_KEY (corrida $i)"
  stack_nuevo
  e=0
  env -u LLM_API_KEY LLM_API_KEY= bash scripts/gate_evals.sh replay > "$TRABAJO/replay$i.log" 2>&1 || e=$?
  if diff <(resultados "$LOG") <(resultados "$TRABAJO/replay$i.log") >/dev/null && [ "$e" -eq "$estado" ]; then
    echo "   replay $i: mismos resultados que la grabación (exit $e)"
  else
    echo "   replay $i: NO coincide con la grabación (exit $e). Log: $TRABAJO/replay$i.log" >&2
    exit 6
  fi
done

# ---------------------------------------------------------------- 3. secretos
echo "== Revisión de secretos en las grabaciones y los logs"
python - "$DESTINO" "$TRABAJO" <<'PY'
import os, pathlib, sys
valores = [os.environ.get("LLM_API_KEY", ""), os.environ["MASTER_ENCRYPTION_KEY"], os.environ["APP_SECRET_KEY"],
           os.environ["WHATSAPP_APP_SECRET"], "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY"]
archivos = [p for d in sys.argv[1:] for p in pathlib.Path(d).rglob("*") if p.is_file() and p.suffix in (".json", ".log")]
malos = [str(p) for p in archivos if any(v and len(v) >= 8 and v in p.read_text(encoding="utf-8", errors="replace") for v in valores)]
if malos:
    print("Aparece una clave en:", *malos, sep="\n  ")
    sys.exit(7)
print(f"   sin claves en {len(archivos)} archivos")
PY
echo "== Listo. Grabaciones en $DESTINO:"
find "$DESTINO" -name '*.json' | sed "s|^$REPO/||" | sort
