"""Modelos de pack-knowledge — SQLAlchemy sobre su PROPIA Base (invariante nº5 de CLAUDE.md).

Dos tablas (ADR-0145 §1), ambas con `tenant_id`, RLS forzada e índices que empiezan por
`tenant_id`:

- `KnowledgeDocument`: un archivo subido a un agente, con su estado de ingesta. `agent_id` es una
  FK lógica hacia `agents.id` (tabla del core), como el resto de los packs; la API la resuelve
  dentro del tenant de la sesión.
- `KnowledgeChunk`: un fragmento del documento con su embedding y su `tsvector` en español.
  FK compuesta `(tenant_id, document_id)` → `(tenant_id, id)`: un fragmento nunca apunta al
  documento de otro negocio.

La dimensión del vector es fija (`DIMENSION`), sin atarla a `EMBEDDING_DIMENSIONS` (que es de
la memoria): cambiar de modelo es una migración más un re-embebido.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.llm.embeddings import DIMENSION
from core.shared.tenant import get_current_tenant_id

# Embeddings del conocimiento (ADR-0145 §3): la dimensión es la del servicio del core (1536).
# Troceado vigente (ADR-0145 §2). Subirlo obliga a reprocesar los documentos.
CHUNKER_VERSION = 1
# Configuración de texto creada por la migración 0001 (`spanish` + `unaccent`).
TS_CONFIG = "public.es_unaccent"

ESTADOS = ("pendiente", "procesando", "listo", "fallido")
ORIGENES = ("archivo", "url")
ERRORES = ("sin_texto", "muy_grande", "formato", "url_inaccesible", "limite_plan", "proveedor", "interno")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _tenant_or_raise() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("tenant_id requerido: usar dentro de tenant_scope()")
    return tenant_id


class TenantMixin:
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, default=_tenant_or_raise)


class Base(DeclarativeBase):
    pass


class KnowledgeDocument(TenantMixin, Base):
    """Un documento de un agente: `pendiente → procesando → listo | fallido` (ADR-0145 §2)."""

    __tablename__ = "pack_knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_pack_knowledge_documents_tenant_id"),
        Index(
            "ix_pack_knowledge_documents_tenant_agent_estado",
            "tenant_id",
            "agent_id",
            "estado",
            postgresql_where=text("deleted_at is null"),
        ),
        # El mismo archivo no se sube dos veces al mismo agente.
        Index(
            "uq_pack_knowledge_documents_tenant_agent_sha",
            "tenant_id",
            "agent_id",
            "sha256",
            unique=True,
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    origen: Mapped[str] = mapped_column(Text, nullable=False, default="archivo")
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    url_origen: Mapped[str | None] = mapped_column(Text)
    # Relativa a `TENANTS_ROOT/<slug>/conocimiento/`; la calcula el servidor, nunca el usuario.
    ruta_archivo: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(Text, nullable=False, default="pendiente")
    error_codigo: Mapped[str | None] = mapped_column(Text)
    # Códigos, ids y conteos: nunca texto del documento (ADR-0145 §2).
    error_detalle: Mapped[str | None] = mapped_column(Text)
    paginas: Mapped[int | None] = mapped_column(Integer)
    num_fragmentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_embebidos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(Text)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    chunker_version: Mapped[int | None] = mapped_column(Integer)
    # Coincidencias del detector de PII (aviso en el panel, ADR-0145 §6).
    pii_detectada: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(TenantMixin, Base):
    """Un fragmento del documento. Al borrar el documento se vacían `contenido` y `embedding`."""

    __tablename__ = "pack_knowledge_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["pack_knowledge_documents.tenant_id", "pack_knowledge_documents.id"],
            name="fk_pack_knowledge_chunks_document",
        ),
        Index("ix_pack_knowledge_chunks_tenant_doc_ordinal", "tenant_id", "document_id", "ordinal"),
        Index(
            "ix_pack_knowledge_chunks_tsv",
            "tsv",
            postgresql_using="gin",
            postgresql_where=text("deleted_at is null"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    contenido: Mapped[str | None] = mapped_column(Text)
    seccion: Mapped[str | None] = mapped_column(Text)
    pagina_desde: Mapped[int | None] = mapped_column(Integer)
    pagina_hasta: Mapped[int | None] = mapped_column(Integer)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[Any] = mapped_column(Vector(DIMENSION))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(f"to_tsvector('{TS_CONFIG}'::regconfig, coalesce(contenido, ''))", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
