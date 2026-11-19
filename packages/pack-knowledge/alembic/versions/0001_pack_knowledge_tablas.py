"""pack_knowledge: documentos y fragmentos por agente con pgvector (ADR-0145 §1, HU-AOS-49).

Greenfield (sin datos previos). Cada tabla nace con `tenant_id NOT NULL`, RLS habilitada y
forzada + policy de aislamiento e índices que empiezan por `tenant_id` (invariante nº1). FORCE va
acá mismo: las migraciones de un pack corren después de la 0029 del core, que no las alcanza.

- `pack_knowledge_documents`: `UNIQUE (tenant_id, id)` como destino de la FK compuesta.
- `pack_knowledge_chunks`: FK compuesta `(tenant_id, document_id)`, `embedding vector(1536)`
  nullable (se vacía al borrar el documento) y `tsv` generada con `public.es_unaccent`.
- Sin índice HNSW: la búsqueda es exacta y prefiltrada por tenant y agente. HNSW entra solo si se
  cumple la condición de revisión de ADR-0145.
- Exige pgvector ≥ 0.8 (`iterative_scan`, ADR-0145 §1). La extensión se creó en la 0001 del
  core y no sube sola al cambiar la imagen: se intenta `ALTER EXTENSION vector UPDATE` y, si no
  alcanza, falla con un mensaje claro.
- `app_runtime` tiene solo SELECT, INSERT y UPDATE: el borrado es lógico (invariante nº8).
- Crea `unaccent` (extensión confiable desde PG 13) y la configuración de texto
  `public.es_unaccent` (`spanish` + `unaccent`): «envio» encuentra «envío».

Revision ID: pack_knowledge_0001
Revises:
Create Date: 2026-10-20
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID

from alembic import op

revision = "pack_knowledge_0001"
down_revision = None
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
TABLAS = ("pack_knowledge_documents", "pack_knowledge_chunks")
VERSION_MINIMA_PGVECTOR = (0, 8)

_ES_UNACCENT = """
DO $do$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_ts_config c JOIN pg_namespace n ON n.oid = c.cfgnamespace
        WHERE n.nspname = 'public' AND c.cfgname = 'es_unaccent'
    ) THEN
        CREATE TEXT SEARCH CONFIGURATION public.es_unaccent (COPY = pg_catalog.spanish);
        ALTER TEXT SEARCH CONFIGURATION public.es_unaccent
            ALTER MAPPING FOR hword, hword_part, word WITH public.unaccent, pg_catalog.spanish_stem;
    END IF;
END
$do$;
"""


def _version(texto: str) -> tuple[int, ...]:
    return tuple(int(p) for p in texto.split(".")[:2] if p.isdigit())


def _exigir_pgvector() -> None:
    conn = op.get_bind()
    actual = conn.execute(sa.text("select extversion from pg_extension where extname = 'vector'")).scalar()
    if actual is None:
        raise RuntimeError("pack_knowledge: falta la extensión vector (la crea la 0001 del core)")
    if _version(actual) < VERSION_MINIMA_PGVECTOR:
        conn.execute(sa.text("alter extension vector update"))
        actual = conn.execute(sa.text("select extversion from pg_extension where extname = 'vector'")).scalar()
    if _version(str(actual)) < VERSION_MINIMA_PGVECTOR:
        raise RuntimeError(
            f"pack_knowledge necesita pgvector >= 0.8 y la base tiene {actual}: "
            "actualiza la imagen de Postgres (docker/postgres/Dockerfile) y vuelve a migrar"
        )


def upgrade() -> None:
    _exigir_pgvector()
    op.execute("create extension if not exists unaccent with schema public")
    op.execute(_ES_UNACCENT)

    op.create_table(
        "pack_knowledge_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        # FK lógica hacia agents.id (tabla del core, ADR-0145 §1).
        sa.Column("agent_id", UUID(as_uuid=True), nullable=False),
        sa.Column("titulo", sa.Text(), nullable=False),
        sa.Column("origen", sa.Text(), nullable=False, server_default="archivo"),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("url_origen", sa.Text(), nullable=True),
        sa.Column("ruta_archivo", sa.Text(), nullable=True),
        sa.Column("estado", sa.Text(), nullable=False, server_default="pendiente"),
        sa.Column("error_codigo", sa.Text(), nullable=True),
        sa.Column("error_detalle", sa.Text(), nullable=True),
        sa.Column("paginas", sa.Integer(), nullable=True),
        sa.Column("num_fragmentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_embebidos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        sa.Column("chunker_version", sa.Integer(), nullable=True),
        sa.Column("pii_detectada", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "id", name="uq_pack_knowledge_documents_tenant_id"),
        sa.CheckConstraint(
            "estado in ('pendiente', 'procesando', 'listo', 'fallido')", name="ck_pack_knowledge_documents_estado"
        ),
        sa.CheckConstraint("origen in ('archivo', 'url')", name="ck_pack_knowledge_documents_origen"),
        sa.CheckConstraint(
            "error_codigo is null or error_codigo in "
            "('sin_texto', 'muy_grande', 'formato', 'url_inaccesible', 'limite_plan', 'proveedor')",
            name="ck_pack_knowledge_documents_error",
        ),
        sa.CheckConstraint("bytes >= 0", name="ck_pack_knowledge_documents_bytes"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_pack_knowledge_documents_sha256"),
    )
    op.create_index(
        "ix_pack_knowledge_documents_tenant_agent_estado",
        "pack_knowledge_documents",
        ["tenant_id", "agent_id", "estado"],
        postgresql_where=sa.text("deleted_at is null"),
    )
    op.create_index(
        "uq_pack_knowledge_documents_tenant_agent_sha",
        "pack_knowledge_documents",
        ["tenant_id", "agent_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("deleted_at is null"),
    )

    op.create_table(
        "pack_knowledge_chunks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("contenido", sa.Text(), nullable=True),
        sa.Column("seccion", sa.Text(), nullable=True),
        sa.Column("pagina_desde", sa.Integer(), nullable=True),
        sa.Column("pagina_hasta", sa.Integer(), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('public.es_unaccent'::regconfig, coalesce(contenido, ''))", persisted=True),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["pack_knowledge_documents.tenant_id", "pack_knowledge_documents.id"],
            name="fk_pack_knowledge_chunks_document",
        ),
        sa.CheckConstraint("ordinal >= 0", name="ck_pack_knowledge_chunks_ordinal"),
    )
    op.create_index(
        "ix_pack_knowledge_chunks_tenant_doc_ordinal",
        "pack_knowledge_chunks",
        ["tenant_id", "document_id", "ordinal"],
    )
    op.create_index(
        "ix_pack_knowledge_chunks_tsv",
        "pack_knowledge_chunks",
        ["tsv"],
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at is null"),
    )

    for tabla in TABLAS:
        # Permisos explícitos (los default privileges de la 0003 solo valen si migra el mismo rol
        # y traen DELETE): sin borrado físico, invariante nº8; la purga va por `purge_tenant`.
        op.execute(f"revoke all on {tabla} from app_runtime")
        op.execute(f"grant select, insert, update on {tabla} to app_runtime")
        op.execute(f"alter table {tabla} enable row level security")
        op.execute(f"alter table {tabla} force row level security")
        op.execute(
            f"create policy tenant_isolation on {tabla} using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
        )


def downgrade() -> None:
    for tabla in reversed(TABLAS):
        op.execute(f"drop policy if exists tenant_isolation on {tabla}")
    op.drop_table("pack_knowledge_chunks")
    op.drop_table("pack_knowledge_documents")
    op.execute("drop text search configuration if exists public.es_unaccent")
    # `unaccent` se deja: otra migración o el negocio pueden usarla.
