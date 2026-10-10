#!/usr/bin/env bash
# Gate de evals del CI (ADR-0120, adenda 2026-10-10): los MISMOS argumentos en las tres formas
# de correrlo, para que lo grabado y lo reproducido sean el mismo gate.
#
#   bash scripts/gate_evals.sh replay   # PR a develop: desde evals/cassettes/, sin red ni LLM_API_KEY
#   bash scripts/gate_evals.sh record   # regrabar el gate: LLM real (~9 llamadas, ~0,01 USD), con OK del dueño
#   bash scripts/gate_evals.sh off      # en vivo, como el push a develop
#
# Requiere una base RECIÉN migrada y sembrada, igual que el job `evals-replay` de `ci.yml`
# (`agent-core db migrate` + `ADMIN_PASSWORD=admin123 python scripts/init_db.py`). Con una base ya
# usada el prompt cambia (por ejemplo, las tools `sub_*` en el flujo) y el replay no coincide.
set -euo pipefail

modo="${1:?uso: bash scripts/gate_evals.sh off|record|replay}"
case "$modo" in off | record | replay) ;; *) echo "modo inválido: $modo" >&2; exit 2 ;; esac
export LLM_CASSETTE="$modo"
if [ "$modo" = "replay" ]; then
  export LLM_API_KEY=""  # replay nunca usa la clave: si algo la pidiera, falla
fi

GOLDEN="06,07,08,21,22,23,24,25,26"
JOURNEYS="01,05"

if [ "$modo" = "record" ]; then
  # Grabar gasta saldo. Un escenario que falla por la respuesta del modelo (exit 1) igual queda
  # grabado y se sigue; cualquier otra salida (3: error de grabación) corta sin más llamadas.
  estado=0
  python evals/runner.py "$GOLDEN" || estado=$?
  if [ "$estado" -ne 0 ] && [ "$estado" -ne 1 ]; then
    echo "Grabación cortada en el golden (exit $estado): no se graban los journeys." >&2
    exit "$estado"
  fi
  python evals/journeys.py "$JOURNEYS" --umbral 1.0 || { e=$?; [ "$estado" -eq 0 ] && estado=$e; }
  exit "$estado"
fi

estado=0
python evals/runner.py "$GOLDEN" || estado=$?
python evals/journeys.py "$JOURNEYS" --umbral 1.0 || { e=$?; [ "$estado" -eq 0 ] && estado=$e; }
exit "$estado"
