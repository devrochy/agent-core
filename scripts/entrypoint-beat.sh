#!/usr/bin/env bash
# Entrypoint de la imagen para el ROL beat (Celery beat).
#
# Gemelo de `entrypoint-worker.sh`: Coolify (build pack Dockerfile) ignora
# `start_command`, así que el comando se fija por `--entrypoint`. Reutiliza el
# entrypoint principal y le pasa el comando de beat.
set -euo pipefail

export AGENT_CORE_ROLE="${AGENT_CORE_ROLE:-beat}"

exec /app/scripts/entrypoint.sh celery -A core.tasks.celery_app:celery beat --loglevel=info
