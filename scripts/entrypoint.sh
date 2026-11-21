#!/usr/bin/env bash
# Entrypoint del contenedor: instala los plugins que la imagen no trae, aplica
# migraciones y arranca el servicio indicado.
#
# AGENT_CORE_ROLE distingue los tres contenedores que comparten esta imagen:
#   api    -> instala plugins Y aplica migraciones (core + plugins)
#   worker -> instala plugins (necesita importarlos para los crons)
#   beat   -> idem
# Solo `api` migra: tres procesos corriendo `alembic upgrade head` a la vez
# compiten por la tabla de versiones.
set -euo pipefail

ROLE="${AGENT_CORE_ROLE:-api}"
# El lock vive en el bind-mount ./plugins: es el único punto que los tres
# contenedores comparten de verdad (/tmp es privado de cada contenedor).
PLUGIN_LOCK="plugins/.install.lock"
# Sello que escribe el Dockerfile: el sha256 del pyproject.toml de cada pack que
# la imagen ya instaló. Vive en /app, fuera de todo bind-mount, así que en
# compose sigue describiendo la imagen y no el código montado.
PACKS_SELLO="/app/.packs-instalados"

# Duración de cada etapa en el log: el corte de un despliegue es el arranque
# completo (Coolify detiene el contenedor viejo antes de arrancar el nuevo).
ETAPA_INICIO=$(date +%s.%N)
fin_etapa() {
  local ahora
  ahora=$(date +%s.%N)
  echo "[entrypoint] etapa ${1}: $(awk -v a="$ETAPA_INICIO" -v b="$ahora" 'BEGIN { printf "%.1f", b - a }') s"
  ETAPA_INICIO=$ahora
}

# Artefactos por tenant (flow.json, playbooks, catalog.json) — ADR-0085:
# viven en TENANTS_ROOT (volumen persistente); se asegura el directorio.
mkdir -p "${TENANTS_ROOT:-/app/tenants}"

install_plugins() {
  # El lock vive en ./plugins: si el dir no existe (imagen sin bind-mounts,
  # ej. Coolify) hay que crearlo antes de flock.
  mkdir -p plugins
  # Packs (packages/pack-*) y plugins heredados (plugins/)
  for dir in packages/pack-*/ plugins/*/; do
    [ -f "${dir}pyproject.toml" ] || continue
    # La imagen ya lo instaló con este mismo pyproject.toml (dependencias y
    # entry points iguales): reinstalarlo costaba ~29 s por arranque en prod.
    # Solo se instala un pack nuevo o con el pyproject cambiado (bind-mount de
    # compose).
    if [ -f "$PACKS_SELLO" ] && grep -qxF "$(sha256sum "${dir}pyproject.toml")" "$PACKS_SELLO"; then
      echo "[entrypoint] ya instalado en la imagen: ${dir}"
      continue
    fi
    echo "[entrypoint] instalando pack/plugin: ${dir}"
    # Los contenedores comparten el bind-mount ./plugins, y `pip install -e`
    # escribe el .egg-info dentro de él: sin lock se pisan entre ellos.
    if ! flock "$PLUGIN_LOCK" pip install -q -e "${dir}"; then
      echo "[entrypoint] ERROR: falló la instalación de ${dir}" >&2
      exit 1
    fi
  done
}

install_plugins
fin_etapa "packs"

if [ "$ROLE" = "api" ]; then
  echo "[entrypoint] aplicando migraciones (core + plugins)..."
  if ! agent-core db migrate; then
    echo "[entrypoint] ERROR: las migraciones fallaron. El servicio no arranca." >&2
    exit 1
  fi
  fin_etapa "migraciones"
  # Base `agentcore_eval` del gate de pass^k (HU-AOS-21 parte 3b), si está configurada. Nunca frena el
  # arranque: sin ella el gate no mide, pero el negocio sigue sirviendo.
  if [ -n "${EVAL_MIGRATIONS_DATABASE_URL:-}" ]; then
    echo "[entrypoint] migrando la base de eval..."
    agent-core db migrate --eval || echo "[entrypoint] AVISO: la migración de agentcore_eval falló (ver el log)" >&2
    fin_etapa "migraciones de eval"
  fi
  # Versiones de las habilidades en `pack_versions` (HU-AOS-14), con el rol dueño. Nunca frena
  # el arranque: un contenido cambiado sin subir la versión alerta en el log y no se registra.
  echo "[entrypoint] registrando versiones de habilidades..."
  agent-core db sync-packs || echo "[entrypoint] AVISO: sync-packs reportó problemas (ver el log)" >&2
  fin_etapa "sync-packs"
fi

exec "$@"
