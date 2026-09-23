"""Capa 3 — Semantic Memory (PostgreSQL + pgvector).

Embeddings de hechos/interacciones pasadas. El servicio de Recall consulta
esta capa antes de cada turno para inyectar contexto de largo plazo (RAG).

DeepSeek NO tiene API de embeddings (gap detectado en agent-sale, donde
pgvector quedó instalado pero sin uso). Proveedores soportados:
  local  -> sentence-transformers (all-MiniLM-L6-v2, 384 dims, gratis, in-process)
  openai -> text-embedding-3-small (1536 dims — requiere ajustar EMBEDDING_DIMENSIONS
            y la columna vector de la migración)
  none   -> capa desactivada; recall devuelve [] y store es no-op.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

import httpx

from core.config import get_settings
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)


class EmbeddingProvider(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class LocalEmbeddings:
    """sentence-transformers in-process. Requiere: pip install 'agent-core[embeddings-local]'"""

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer  # import perezoso (dep pesada)

        self._model = SentenceTransformer(model_name)

    async def embed(self, text: str) -> list[float]:
        import asyncio

        loop = asyncio.get_running_loop()
        vector = await loop.run_in_executor(None, self._model.encode, text)
        return [float(x) for x in vector]


class OpenAIEmbeddings:
    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key
        self._model = model

    async def embed(self, text: str) -> list[float]:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                json={"model": self._model, "input": text},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            embedding = response.json()["data"][0]["embedding"]
            return [float(x) for x in embedding]


_provider: EmbeddingProvider | None = None
_provider_resolved = False


def get_embedding_provider() -> EmbeddingProvider | None:
    global _provider, _provider_resolved
    if _provider_resolved:
        return _provider
    _provider_resolved = True
    settings = get_settings()
    if settings.embedding_provider == "local":
        try:
            _provider = LocalEmbeddings(settings.embedding_model)
        except ImportError:
            log.error("sentence_transformers_not_installed",
                      hint="pip install 'agent-core[embeddings-local]'")
    elif settings.embedding_provider == "openai":
        if settings.openai_api_key:
            _provider = OpenAIEmbeddings(settings.openai_api_key)
        else:
            log.error("openai_api_key_missing_for_embeddings")
    return _provider


async def store_memory(
    content: str,
    customer_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Guarda un hecho con su embedding. No-op si la capa está desactivada.

    El contenido se redacta (PII) antes de embeber (ADR-0053): la fuente de verdad
    (el mensaje) queda intacta en `messages`; lo que se guarda acá es la versión sin
    datos sensibles evidentes.
    """
    provider = get_embedding_provider()
    if provider is None:
        return
    try:
        # Se redacta ANTES de persistir y de embeber: `memory_vectors` no debe
        # guardar PII. La fuente de verdad (el mensaje sin redactar) sigue en
        # `messages`, que es donde se audita.
        redacted = redact_pii(content)
        embedding = await provider.embed(redacted)
        from core.db.models import MemoryVector
        from core.db.session import get_session

        async with get_session() as session:
            session.add(
                MemoryVector(
                    tenant_id=get_current_tenant_id(),
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                    content=redacted,
                    embedding=embedding,
                    meta=meta or {},
                )
            )
    except Exception:
        log.exception("vector_store_failed")


# Patrones amplios de PII (ADR-0053). Sin regex catastrófica: clases acotadas.
_PII_PATTERNS = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)"), "[teléfono]"),
    (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), "[número]"),
)


def redact_pii(text: str) -> str:
    """Enmascara emails, teléfonos y secuencias tipo tarjeta (best-effort)."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


async def purge_memory(
    customer_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> int:
    """Borra (lógico) la memoria semántica de un cliente/conversación (ADR-0053).

    Devuelve cuántas filas afectó. Exige al menos un criterio para no borrar todo
    el tenant por accidente. Limpia también la working memory (Redis) si es por
    conversación.
    """
    if customer_id is None and conversation_id is None:
        raise ValueError("purge_memory requiere customer_id o conversation_id")
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise ValueError("purge_memory requiere contexto de tenant")

    from sqlalchemy import select, update

    from core.db.models import MemoryVector, utcnow
    from core.db.session import get_session

    async with get_session() as session:
        stmt = select(MemoryVector).where(
            MemoryVector.tenant_id == tenant_id, MemoryVector.deleted_at.is_(None)
        )
        if customer_id is not None:
            stmt = stmt.where(MemoryVector.customer_id == customer_id)
        if conversation_id is not None:
            stmt = stmt.where(MemoryVector.conversation_id == conversation_id)
        ids = (await session.execute(stmt)).scalars().all()
        if ids:
            await session.execute(
                update(MemoryVector)
                .where(MemoryVector.id.in_([row.id for row in ids]))
                .values(deleted_at=utcnow())
            )

    if conversation_id is not None:
        from core.memory import working as working_memory

        await working_memory.clear(conversation_id)
    return len(ids)


async def recall(
    query: str,
    customer_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    top_k: int = 3,
    max_distance: float = 0.6,
) -> list[str]:
    """Recupera los hechos más relevantes (distancia coseno). [] si está desactivada.

    Filtra `MemoryVector` por `tenant_id` de forma EXPLÍCITA (defensa en
    profundidad, ADR-0012) además de RLS -- no depende solo de que el
    `set_config` de sesión haya corrido. `conversation_id`, si viene, se
    valida contra `get_owned()` como cinturón adicional: no restringe el
    recall a esa conversación puntual (sigue siendo cross-conversación por
    `customer_id`), solo confirma que pertenece al tenant/cliente correctos
    antes de ejecutar la búsqueda por similaridad.
    """
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        log.warning("recall_missing_tenant_context")
        return []
    provider = get_embedding_provider()
    if provider is None:
        return []
    try:
        query_embedding = await provider.embed(query)
        from sqlalchemy import select

        from core.db.models import Conversation, MemoryVector
        from core.db.session import get_owned, get_session

        async with get_session() as session:
            if conversation_id is not None:
                conversation = await get_owned(session, Conversation, conversation_id, tenant_id=tenant_id)
                if conversation is None or (
                    customer_id is not None and conversation.customer_id != customer_id
                ):
                    log.warning("recall_conversation_not_owned", conversation_id=str(conversation_id))
                    return []

            distance = MemoryVector.embedding.cosine_distance(query_embedding)
            stmt = (
                select(MemoryVector.content, distance.label("d"))
                .where(MemoryVector.tenant_id == tenant_id)
                .order_by(distance)
                .limit(top_k)
            )
            if customer_id is not None:
                stmt = stmt.where(MemoryVector.customer_id == customer_id)
            rows = (await session.execute(stmt)).all()
            return [content for content, d in rows if d is not None and float(d) <= max_distance]
    except Exception:
        log.exception("vector_recall_failed")
        return []
