# Postgres del despliegue con pgvector + PostGIS (ADR-0058).
#
# El core usa pgvector para la memoria semántica (RAG) y el pack de cobranza
# necesita PostGIS para geografía (ubicación de clientes, rutas). Van en la
# MISMA base: no se levanta un Postgres geo separado.
FROM pgvector/pgvector:pg16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-postgis-3 \
    && rm -rf /var/lib/apt/lists/*
