FROM python:3.12-slim

WORKDIR /app

# curl para el healthcheck que genera Coolify (docker healthcheck).
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY packages ./packages
COPY core ./core
COPY cli ./cli
COPY alembic ./alembic
COPY alembic.ini ./
COPY sdd/templates ./sdd/templates

# Packs (packages/pack-*) y plugins (plugins/) horneados en la imagen:
# en el despliegue (Coolify) no hay volúmenes de bind-mount como en compose.
RUN pip install --no-cache-dir -e packages/contracts \
    && pip install --no-cache-dir -e . \
    && for d in packages/pack-*/ plugins/*/; do \
         if [ -f "${d}pyproject.toml" ]; then \
           pip install --no-cache-dir -e "${d}" || exit 1; \
         fi; \
       done

COPY scripts ./scripts
RUN chmod +x scripts/entrypoint.sh || true

EXPOSE 8000

# El entrypoint instala plugins (si faltan), aplica migraciones (rol api) y
# ejecuta el CMD. En docker-compose se fuerza igual, pero en Coolify no hay
# bind-mounts ni override de entrypoint: sin ENTRYPOINT las migraciones
# no corren y el arranque falla (app_runtime no existe).
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]