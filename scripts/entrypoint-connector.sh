#!/usr/bin/env bash
# Entrypoint de la imagen para el ROL connector (canal waweb, ADR-0074).
#
# Reutiliza el entrypoint principal (instala packs/plugins; migra SOLO si el rol
# es `api`) y le pasa el comando del connector. En Coolify se usa con
# `custom_docker_run_options: --entrypoint /app/scripts/entrypoint-connector.sh`.
set -euo pipefail

export AGENT_CORE_ROLE="${AGENT_CORE_ROLE:-connector}"

exec /app/scripts/entrypoint.sh python -m core.waweb.connector
