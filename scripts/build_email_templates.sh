#!/usr/bin/env bash
# Compila las plantillas MJML de los correos a HTML listo para enviar.
#
#   core/gateway/email_templates/src/*.mjml  →  core/gateway/email_templates/html/*.html
#
# El HTML generado se versiona: el runtime (y el CI de Python) no necesitan Node.
# Solo se corre al cambiar el diseño. Requiere Node 20+.
set -euo pipefail

cd "$(dirname "$0")/.."

MJML_PACKAGE="${MJML_PACKAGE:-mjml@5}"
SRC_DIR="core/gateway/email_templates/src"
OUT_DIR="core/gateway/email_templates/html"

mkdir -p "$OUT_DIR"

for src in "$SRC_DIR"/*.mjml; do
  name="$(basename "$src" .mjml)"
  echo "→ $name"
  npx --yes "$MJML_PACKAGE" "$src" \
    -o "$OUT_DIR/$name.html" \
    --config.minify=true \
    --config.validationLevel=strict
done

echo "✓ Plantillas de correo compiladas en $OUT_DIR"
