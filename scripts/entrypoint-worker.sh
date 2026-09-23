#!/usr/bin/env bash
# Entrypoint de la imagen para el ROL worker (Celery).
#
# Contexto: en Coolify el build pack es `Dockerfile` (un contenedor por app) y
# Coolify IGNORA `start_command` para ese build pack (solo lo usa en Nixpacks).
# La vía soportada de cambiar el proceso es
# `custom_docker_run_options: --entrypoint /app/scripts/entrypoint-worker.sh`.
#
# Reutiliza el entrypoint principal (que instala los packs/plugins y aplica
# migraciones solo si el rol es `api`) y le pasa el comando de celery como
# argumento -- así el worker arranca Celery en vez de uvicorn.
set -euo pipefail

export AGENT_CORE_ROLE="${AGENT_CORE_ROLE:-worker}"

exec /app/scripts/entrypoint.sh celery -A core.tasks.celery_app:celery worker --loglevel=info
