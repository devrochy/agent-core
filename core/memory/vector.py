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
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from core.config import get_settings
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

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
    from core.llm import cassette

    if cassette.modo_activo() != "off":
        # Evals en record/replay: sin red y sin recuerdo que cambie el prompt (adenda de ADR-0119).
        return None
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


KINDS = frozenset({"exchange", "episode", "fact", "document"})  # = ck_mv_kind (0080)


def _embedding_model() -> str:
    settings = get_settings()
    return settings.embedding_model if settings.embedding_provider == "local" else "text-embedding-3-small"


async def store_memory(
    content: str,
    customer_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
    kind: str | None = None,
) -> None:
    """Guarda un recuerdo con su embedding (scope `customer`). No-op si la capa está desactivada.

    El contenido se redacta (PII) antes de embeber (ADR-0053): la fuente de verdad
    (el mensaje) queda intacta en `messages`; lo que se guarda acá es la versión sin
    datos sensibles evidentes.

    Sin `customer_id` (el loop guarda así cada intercambio), el cliente y la conexión salen de
    la conversación, con filtro explícito de negocio: así el recuerdo cumple `ck_mv_keys` y lo
    cubre la purga del cliente (HU-AOS-36). Sin cliente resoluble no se guarda nada.
    `kind` por defecto conserva la conducta de antes: con cliente explícito es un recuerdo que el
    recall devuelve (`fact`); sin él, un intercambio que el recall no mira (`exchange`).
    Los hechos estructurados (`memory_facts`) nunca pasan por aquí.
    """
    provider = get_embedding_provider()
    if provider is None:
        return
    if kind is None:
        kind = "fact" if customer_id is not None else "exchange"
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r}")
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        log.warning("vector_store_missing_tenant_context")
        return
    try:
        # Se redacta ANTES de persistir y de embeber: `memory_vectors` no debe
        # guardar PII. La fuente de verdad (el mensaje sin redactar) sigue en
        # `messages`, que es donde se audita.
        redacted = redact_pii(content)
        embedding = await provider.embed(redacted)
        from sqlalchemy import text

        from core.db.models import MemoryVector
        from core.db.session import get_session

        async with get_session() as session:
            connection_id: uuid.UUID | None = None
            if conversation_id is not None:
                fila = (
                    await session.execute(
                        text(
                            "SELECT c.customer_id, cc.id FROM conversations c "
                            "LEFT JOIN channel_connections cc ON cc.tenant_id = c.tenant_id AND cc.id = c.connection_id "
                            "WHERE c.tenant_id = :t AND c.id = :c"
                        ),
                        {"t": tenant_id, "c": conversation_id},
                    )
                ).first()
                if fila is None or (customer_id is not None and fila[0] != customer_id):
                    log.warning("vector_store_conversation_not_owned", conversation_id=str(conversation_id))
                    return
                customer_id, connection_id = fila[0], fila[1]
            elif customer_id is not None:
                # Sin conversación, el cliente tiene que ser de este negocio (la FK es simple).
                propio = (
                    await session.execute(
                        text("SELECT 1 FROM customers WHERE tenant_id = :t AND id = :c"),
                        {"t": tenant_id, "c": customer_id},
                    )
                ).first()
                if propio is None:
                    log.warning("vector_store_customer_not_owned")
                    return
            if customer_id is None:
                log.warning("vector_store_without_customer")
                return
            session.add(
                MemoryVector(
                    tenant_id=tenant_id,
                    customer_id=customer_id,
                    conversation_id=conversation_id,
                    connection_id=connection_id,
                    scope="customer",
                    kind=kind,
                    embedding_model=_embedding_model(),
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
    """Olvido (lógico) de la memoria de un cliente o de una conversación (ADR-0053, ADR-0114 §4).

    - Por cliente (solo `customer_id`): sus recuerdos de `memory_vectors` (también los
      intercambios que el loop guardó), sus hechos de `memory_facts` y su proyección en
      `customers.attributes`, en UNA transacción; después del commit, la memoria de trabajo de
      Redis de TODAS sus conversaciones. Sus tareas y mensajes quedan: son registro del negocio.
    - Por conversación: sus recuerdos y su memoria de trabajo, como antes (con `customer_id`
      también, solo los recuerdos de ese cliente en esa conversación).

    Devuelve cuántos registros de memoria borró (recuerdos + hechos). Exige al menos un
    criterio para no borrar todo el negocio por accidente. Si Redis falla después del commit,
    la purga no se revierte (se registra; la memoria de trabajo vence sola en 24 h).
    """
    if customer_id is None and conversation_id is None:
        raise ValueError("purge_memory requiere customer_id o conversation_id")
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise ValueError("purge_memory requiere contexto de tenant")

    from sqlalchemy import or_, select, update

    from core.db.models import Conversation, MemoryVector, utcnow
    from core.db.session import get_session
    from core.memory import facts

    ahora = utcnow()
    conversaciones: list[uuid.UUID] = []
    hechos = 0
    async with get_session() as session:
        if conversation_id is None and customer_id is not None:
            # Primero el cliente (orden de locks de `core/memory/facts.py`).
            hechos = await facts.purgar_de_cliente(
                session, tenant_id=tenant_id, customer_id=customer_id, now=ahora
            )
            conversaciones = list(
                (
                    await session.execute(
                        select(Conversation.id).where(
                            Conversation.tenant_id == tenant_id, Conversation.customer_id == customer_id
                        )
                    )
                ).scalars()
            )
        else:
            # Solo si la conversación es de este negocio (y de ese cliente, si viene): la clave
            # legada `wm:{conv}` no lleva negocio y no se borra la de una conversación ajena.
            filtro = select(Conversation.id).where(
                Conversation.tenant_id == tenant_id, Conversation.id == conversation_id
            )
            if customer_id is not None:
                filtro = filtro.where(Conversation.customer_id == customer_id)
            conversaciones = list((await session.execute(filtro)).scalars())
            if not conversaciones:
                return 0
        stmt = (
            update(MemoryVector)
            .where(MemoryVector.tenant_id == tenant_id, MemoryVector.deleted_at.is_(None))
            .values(deleted_at=ahora)
        )
        if customer_id is not None and conversation_id is None:
            # También lo que quedó sin cliente en sus conversaciones (p. ej. intercambios que el
            # código anterior guardó durante el deploy).
            stmt = stmt.where(
                or_(MemoryVector.customer_id == customer_id, MemoryVector.conversation_id.in_(conversaciones))
                if conversaciones
                else MemoryVector.customer_id == customer_id
            )
        elif customer_id is not None:
            stmt = stmt.where(MemoryVector.customer_id == customer_id)
        if conversation_id is not None:
            stmt = stmt.where(MemoryVector.conversation_id == conversation_id)
        resultado = cast("CursorResult[Any]", await session.execute(stmt))
        recuerdos = int(resultado.rowcount or 0)

    if conversaciones:
        from core.memory import working as working_memory

        await working_memory.clear_many(conversaciones)
    return recuerdos + hechos


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
    if customer_id is None:
        # Sin cliente no hay recall: antes devolvía memoria de cualquier cliente del negocio.
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
            # Los intercambios del loop no se recuerdan (antes no tenían cliente y el filtro
            # por cliente los dejaba fuera; HU-AOS-36 conserva esa conducta) y lo olvidado
            # tampoco vuelve.
            stmt = (
                select(MemoryVector.content, distance.label("d"))
                .where(
                    MemoryVector.tenant_id == tenant_id,
                    MemoryVector.scope == "customer",
                    MemoryVector.customer_id == customer_id,
                    MemoryVector.kind != "exchange",
                    MemoryVector.deleted_at.is_(None),
                )
                .order_by(distance)
                .limit(top_k)
            )
            rows = (await session.execute(stmt)).all()
            return [content for content, d in rows if d is not None and float(d) <= max_distance]
    except Exception:
        log.exception("vector_recall_failed")
        return []
