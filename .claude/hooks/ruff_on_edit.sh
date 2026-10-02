#!/usr/bin/env bash
# Hook PostToolUse (H1): corre `ruff check` sobre el archivo .py recién editado y
# devuelve los errores al agente antes del CI. No usa `ruff format` (el repo no
# está formateado de forma uniforme y generaría diffs ruidosos).
set -euo pipefail

input="$(cat)"
file="$(printf '%s' "$input" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("tool_input") or {}).get("file_path",""))' 2>/dev/null || true)"

case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0

# Ubica ruff: venv del proyecto, PATH, o uv.
if [ -x ".venv/bin/ruff" ]; then
  ruff=".venv/bin/ruff"
elif command -v ruff >/dev/null 2>&1; then
  ruff="ruff"
elif command -v uv >/dev/null 2>&1; then
  ruff="uv run ruff"
else
  exit 0
fi

if ! out="$($ruff check "$file" 2>&1)"; then
  echo "ruff encontró problemas en $file:" >&2
  echo "$out" >&2
  exit 2
fi
exit 0
