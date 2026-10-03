FROM python:3.12-slim

WORKDIR /app

# curl para el healthcheck que genera Coolify (docker healthcheck).
# libmagic1 y ffmpeg: requeridos por Neonize (canal waweb, ADR-0074) para
# detectar tipos de archivo y procesar media.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev curl libmagic1 ffmpeg && rm -rf /var/lib/apt/lists/*

# HU-ARCH-24: la imagen corre como usuario SIN privilegios. Se crea antes de
# copiar el código y se le dan permisos sobre /app.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app

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
    && pip install --no-cache-dir -e ".[waweb]" \
    && for d in packages/pack-*/ packages/channel-*/ plugins/*/; do \
         if [ -f "${d}pyproject.toml" ]; then \
           pip install --no-cache-dir -e "${d}" || exit 1; \
         fi; \
       done

COPY scripts ./scripts
RUN chmod +x scripts/entrypoint.sh scripts/entrypoint-worker.sh scripts/entrypoint-beat.sh || true

# El usuario `app` necesita escribir artefactos de tenant (TENANTS_ROOT) y
# temporales. Se le da la propiedad de /app tras copiar todo.
RUN chown -R app:app /app
USER app

EXPOSE 8000

# El entrypoint instala plugins (si faltan), aplica migraciones (rol api) y
# ejecuta el CMD. En docker-compose se fuerza igual, pero en Coolify no hay
# bind-mounts ni override de entrypoint: sin ENTRYPOINT las migraciones
# no corren y el arranque falla (app_runtime no existe).
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]