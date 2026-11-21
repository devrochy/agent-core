FROM python:3.12-slim

WORKDIR /app

# Sin la consulta de versión de pip (y sus avisos en el log) en cada install.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

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
COPY evals ./evals
COPY sdd/templates ./sdd/templates

# Packs (packages/pack-*) y plugins (plugins/) horneados en la imagen:
# en el despliegue (Coolify) no hay volúmenes de bind-mount como en compose.
# El sello /app/.packs-instalados guarda el sha256 de cada pyproject.toml
# instalado: el entrypoint se salta esos packs y solo instala los que cambien
# en un bind-mount (scripts/entrypoint.sh, install_plugins).
RUN pip install --no-cache-dir -e packages/contracts \
    && pip install --no-cache-dir -e ".[waweb]" \
    && for d in packages/pack-*/ packages/channel-*/ plugins/*/; do \
         if [ -f "${d}pyproject.toml" ]; then \
           pip install --no-cache-dir -e "${d}" || exit 1; \
           sha256sum "${d}pyproject.toml" >> /app/.packs-instalados; \
         fi; \
       done

COPY scripts ./scripts
RUN chmod +x scripts/entrypoint.sh scripts/entrypoint-worker.sh scripts/entrypoint-beat.sh || true

# El usuario `app` necesita escribir artefactos de tenant (TENANTS_ROOT) y
# temporales. Se le da la propiedad de /app tras copiar todo.
RUN chown -R app:app /app
USER app

EXPOSE 8000

# El entrypoint instala los plugins que la imagen no trae, aplica migraciones (rol api) y
# ejecuta el CMD. En docker-compose se fuerza igual, pero en Coolify no hay
# bind-mounts ni override de entrypoint: sin ENTRYPOINT las migraciones
# no corren y el arranque falla (app_runtime no existe).
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]