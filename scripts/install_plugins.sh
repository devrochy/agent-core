#!/usr/bin/env bash
# Instala en modo editable los packs locales (packages/pack-*), los canales
# (packages/channel-*, p. ej. WhatsApp Web) y los plugins heredados (plugins/).
set -euo pipefail

for dir in packages/pack-*/ packages/channel-*/ plugins/*/; do
  [ -f "${dir}pyproject.toml" ] || continue
  echo "→ ${PIP:-pip} install -e ${dir}"
  # El CI exporta PIP="uv pip"; en local queda el pip del venv activo.
  ${PIP:-pip} install -e "${dir}"
done
echo "✓ Packs y plugins locales instalados"