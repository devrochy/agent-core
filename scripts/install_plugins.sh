#!/usr/bin/env bash
# Instala en modo editable los packs locales (packages/pack-*) y plugins heredados (plugins/).
set -euo pipefail

for dir in packages/pack-*/ plugins/*/; do
  [ -f "${dir}pyproject.toml" ] || continue
  echo "→ pip install -e ${dir}"
  pip install -e "${dir}"
done
echo "✓ Packs y plugins locales instalados"