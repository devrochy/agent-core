# Postgres del despliegue con pgvector + PostGIS (ADR-0058).
#
# El core usa pgvector para la memoria semántica (RAG) y el pack de cobranza
# necesita PostGIS para geografía (ubicación de clientes, rutas). Van en la
# MISMA base (no se levanta un Postgres geo separado).
#
# La imagen oficial `pgvector/pgvector:pg17` NO trae PostGIS: si falta, las
# migraciones del pack fallan con `Could not open extension control file
# ".../postgis.control"`. Por eso se usa esta imagen propia en Coolify.
#
# Build/push (o construir local y usar el tag en Coolify):
#   docker build -t solvershub-postgres:pg17-postgis docker/postgres
FROM pgvector/pgvector:pg17

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-postgis-3 \
    && rm -rf /var/lib/apt/lists/*
