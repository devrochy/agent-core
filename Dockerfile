FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY core ./core
COPY cli ./cli
COPY alembic ./alembic
COPY alembic.ini ./
COPY sdd/templates ./sdd/templates

RUN pip install --no-cache-dir -e .

# Los plugins locales se montan como volumen y se instalan al arrancar si existen
COPY scripts ./scripts
RUN chmod +x scripts/entrypoint.sh || true

EXPOSE 8000
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
