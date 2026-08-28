#!/usr/bin/env bash
# Entrypoint del contenedor: instala plugins montados, aplica migraciones y
# arranca el servicio indicado.
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

install_plugins() {
  # Packs (packages/pack-*) y plugins heredados (plugins/)
  for dir in packages/pack-*/ plugins/*/; do
    [ -f "${dir}pyproject.toml" ] || continue
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

if [ "$ROLE" = "api" ]; then
  echo "[entrypoint] aplicando migraciones (core + plugins)..."
  if ! agent-core db migrate; then
    echo "[entrypoint] ERROR: las migraciones fallaron. El servicio no arranca." >&2
    exit 1
  fi
fi

exec "$@"
