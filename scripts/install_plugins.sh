#!/usr/bin/env bash
# Instala en modo editable todos los plugins locales de ./plugins/
set -euo pipefail

for dir in plugins/*/; do
  if [ -f "${dir}pyproject.toml" ]; then
    echo "→ pip install -e ${dir}"
    pip install -e "${dir}"
  fi
done
echo "✓ Plugins locales instalados"
