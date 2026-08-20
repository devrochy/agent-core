#!/usr/bin/env bash
# Entrypoint del contenedor: instala plugins montados y arranca el servicio dado.
set -euo pipefail

if compgen -G "plugins/*/pyproject.toml" > /dev/null 2>&1; then
  for dir in plugins/*/; do
    [ -f "${dir}pyproject.toml" ] && pip install -q -e "${dir}" || true
  done
fi

exec "$@"
