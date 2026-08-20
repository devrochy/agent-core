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

import uuid
from typing import Any, Protocol

import httpx

from core.config import get_settings
from core.shared.logger import get_logger

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
    """Guarda un hecho con su embedding. No-op si la capa está desactivada."""
    provider = get_embedding_provider()
    if provider is None:
        return
    try:
        embedding = await provider.embed(content)
        from core.db.models import MemoryVector
        from core.db.session import get_session

        async with get_session() as session:
            session.add(
                MemoryVector(
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                    content=content,
                    embedding=embedding,
                    meta=meta or {},
                )
            )
    except Exception:
        log.exception("vector_store_failed")


async def recall(
    query: str, customer_id: uuid.UUID | None = None, top_k: int = 3, max_distance: float = 0.6
) -> list[str]:
    """Recupera los hechos más relevantes (distancia coseno). [] si está desactivada."""
    provider = get_embedding_provider()
    if provider is None:
        return []
    try:
        query_embedding = await provider.embed(query)
        from sqlalchemy import select

        from core.db.models import MemoryVector
        from core.db.session import get_session

        async with get_session() as session:
            distance = MemoryVector.embedding.cosine_distance(query_embedding)
            stmt = select(MemoryVector.content, distance.label("d")).order_by(distance).limit(top_k)
            if customer_id is not None:
                stmt = stmt.where(MemoryVector.customer_id == customer_id)
            rows = (await session.execute(stmt)).all()
            return [content for content, d in rows if d is not None and float(d) <= max_distance]
    except Exception:
        log.exception("vector_recall_failed")
        return []
