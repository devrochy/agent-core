#!/usr/bin/env bash
# Reproduce un webhook de Meta contra un servidor local, sin depender de que
# Meta lo dispare de verdad. Requiere `openssl` y el server corriendo
# (`agent-core dev` / `make dev`).
#
# Uso:
#   ./scripts/replay_webhook.sh tests/fixtures/webhooks/wa_text_message.json
#   ./scripts/replay_webhook.sh tests/fixtures/webhooks/wa_text_message.json /webhooks/whatsapp/mi-tenant
#
# El App Secret sale de $META_APP_SECRET o $WHATSAPP_APP_SECRET (mismo
# criterio de fallback que usa el endpoint genérico, core/gateway/webhooks.py).
# Si ninguno está seteado, se envía sin firma (el endpoint no la exige en ese caso).
set -euo pipefail

FIXTURE="${1:?Uso: $0 <fixture.json> [ruta]}"
ROUTE="${2:-/webhooks/whatsapp}"
BASE_URL="${BASE_URL:-http://localhost:8000}"
APP_SECRET="${META_APP_SECRET:-${WHATSAPP_APP_SECRET:-}}"

BODY=$(cat "$FIXTURE" | tr -d '\n')

if [ -n "$APP_SECRET" ]; then
  SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$APP_SECRET" | awk '{print $2}')
  echo "Firmando con el App Secret configurado..."
  curl -sS -X POST "${BASE_URL}${ROUTE}" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=$SIG" \
    --data-raw "$BODY" | tee /dev/stderr
else
  echo "META_APP_SECRET/WHATSAPP_APP_SECRET no seteados -- enviando sin firma."
  curl -sS -X POST "${BASE_URL}${ROUTE}" \
    -H "Content-Type: application/json" \
    --data-raw "$BODY" | tee /dev/stderr
fi
echo
