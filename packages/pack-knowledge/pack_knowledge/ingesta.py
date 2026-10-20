"""Ingesta de documentos (ADR-0145 §2 y §6, HU-AOS-49 parte 2).

`pendiente → procesando → listo | fallido`:

1. **Subida** (`crear_documento`, en la API): valida tipo y contenido, aplica el tope del plan,
   guarda el original y deja el documento `pendiente`. Encola `extraer`.
2. **`extraer`** (worker): extrae el texto, trocea (v1), cuenta PII y reemplaza los fragmentos del
   documento en una transacción (los anteriores se marcan borrados y se vacían). Encola `embeber`.
3. **`embeber`** (worker): embebe hasta `MAX_LOTE` fragmentos sin vector y se vuelve a encolar
   mientras queden. Lotes en cadena, nunca en paralelo: no ocupa el worker (que comparte la cola
   con los envíos) más que un lote a la vez. Cuando no queda ninguno, el documento queda `listo`.

Cada tarea recibe `tenant_id` y `document_id`, abre `tenant_scope(tenant_id)` y lee el documento
con RLS y filtro explícito: si no existe para ese negocio, no hace nada. El payload lleva solo ids;
los logs y `error_detalle`, códigos y conteos, nunca texto del documento.

Una entrega repetida es inofensiva: `extraer` solo corre con el documento `pendiente` o
`procesando`, y `embeber` solo llena vectores vacíos.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from core.db.models import AgentRecord
from core.db.session import get_owned, get_session
from core.llm.embeddings import DIMENSION, MAX_LOTE, EmbeddingsError, embeber_conocimiento
from core.memory.vector import contar_pii
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope, tenant_slug
from core.tasks.queue import task_queue
from pack_knowledge import archivos
from pack_knowledge.extraccion import ExtraccionError, extraer_texto
from pack_knowledge.limites import exigir_cupo
from pack_knowledge.models import CHUNKER_VERSION, KnowledgeChunk, KnowledgeDocument
from pack_knowledge.troceado import DemasiadosFragmentos, trocear

log = get_logger(__name__)

PLUGIN_ID = "pack_knowledge"
TAREA_EXTRAER = f"{PLUGIN_ID}.extraer"
TAREA_EMBEBER = f"{PLUGIN_ID}.embeber"
MAX_TITULO = 200


class AgenteNoEncontrado(LookupError):
    pass


class DocumentoNoEncontrado(LookupError):
    pass


class DocumentoDuplicado(Exception):
    def __init__(self, document_id: uuid.UUID) -> None:
        self.document_id = document_id
        super().__init__("documento duplicado")


def _ahora() -> datetime:
    return datetime.now(UTC)


def _tenant() -> uuid.UUID:
    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("la ingesta requiere un tenant_scope")
    return tenant_id


async def _slug(tenant_id: uuid.UUID) -> str:
    slug = await tenant_slug(tenant_id)
    if slug is None:
        raise RuntimeError("negocio sin slug")
    return slug


async def _agente(session: Any, agent_id: uuid.UUID) -> AgentRecord:
    """Especialista vivo del negocio del scope; otro negocio, el Coordinador o un borrado: no existe."""
    agente = await get_owned(session, AgentRecord, agent_id)
    if agente is None or agente.deleted_at is not None or agente.kind != "specialist":
        raise AgenteNoEncontrado
    return agente


def _vivos(agent_id: uuid.UUID | None = None) -> list[Any]:
    filtros: list[Any] = [KnowledgeDocument.tenant_id == _tenant(), KnowledgeDocument.deleted_at.is_(None)]
    if agent_id is not None:
        filtros.append(KnowledgeDocument.agent_id == agent_id)
    return filtros


async def _documento(session: Any, agent_id: uuid.UUID, document_id: uuid.UUID) -> KnowledgeDocument:
    doc = await get_owned(session, KnowledgeDocument, document_id)
    if doc is None or doc.deleted_at is not None or doc.agent_id != agent_id:
        raise DocumentoNoEncontrado
    return doc


def _encolar(tarea: str, tenant_id: uuid.UUID, document_id: uuid.UUID, **extra: str) -> None:
    task_queue.enqueue(tarea, tenant_id=str(tenant_id), document_id=str(document_id), **extra)


def titulo_de(titulo: str | None, nombre_archivo: str | None) -> str:
    base = (titulo or "").strip() or (nombre_archivo or "").rsplit("/", 1)[-1].strip() or "Documento"
    return " ".join(base.split())[:MAX_TITULO]


# ------------------------------------------------------------------ API
async def listar(agent_id: uuid.UUID) -> list[KnowledgeDocument]:
    async with get_session() as session:
        await _agente(session, agent_id)
        return list(
            (
                await session.execute(
                    select(KnowledgeDocument)
                    .where(*_vivos(agent_id))
                    .order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id)
                )
            ).scalars()
        )


async def crear_documento(
    *,
    agent_id: uuid.UUID,
    titulo: str,
    tipo: str,
    contenido: bytes,
    created_by: uuid.UUID | None,
) -> KnowledgeDocument:
    """Valida, aplica el tope del plan, guarda el original y deja el documento `pendiente`.

    Levanta `ArchivoInvalido`, `AgenteNoEncontrado`, `DocumentoDuplicado` o `QuotaExceeded`. Si la
    transacción falla, el original escrito se borra."""
    tenant_id = _tenant()
    archivos.validar(tipo, contenido)
    slug = await _slug(tenant_id)
    sha = hashlib.sha256(contenido).hexdigest()
    doc_id = uuid.uuid4()
    ruta: str | None = None
    try:
        async with get_session() as session:
            await _agente(session, agent_id)
            existente = (
                await session.execute(
                    select(KnowledgeDocument.id).where(*_vivos(agent_id), KnowledgeDocument.sha256 == sha)
                )
            ).scalar_one_or_none()
            if existente is not None:
                raise DocumentoDuplicado(existente)
            await exigir_cupo(session, bytes_nuevos=len(contenido))
            ruta = archivos.guardar(slug, doc_id, tipo, contenido)
            doc = KnowledgeDocument(
                id=doc_id,
                agent_id=agent_id,
                titulo=titulo,
                origen="archivo",
                mime=tipo,
                bytes=len(contenido),
                sha256=sha,
                ruta_archivo=ruta,
                estado="pendiente",
                created_by=created_by,
            )
            session.add(doc)
            await session.flush()
    except IntegrityError:
        # Dos subidas del mismo archivo a la vez: el índice único parcial decide.
        if ruta is not None:
            archivos.borrar(slug, ruta)
        raise DocumentoDuplicado(doc_id) from None
    except BaseException:
        if ruta is not None:
            archivos.borrar(slug, ruta)
        raise
    _encolar(TAREA_EXTRAER, tenant_id, doc_id)
    log.info("knowledge_documento_subido", document_id=str(doc_id), bytes=len(contenido), mime=tipo)
    return doc


async def borrar_documento(agent_id: uuid.UUID, document_id: uuid.UUID) -> None:
    """Borrado lógico (invariante nº8) que vacía el contenido: el documento y sus fragmentos
    quedan con `deleted_at`, los fragmentos sin texto ni vector, y el original se borra del disco
    después del commit (ADR-0145 §6)."""
    tenant_id = _tenant()
    ahora = _ahora()
    async with get_session() as session:
        doc = await _documento(session, agent_id, document_id)
        doc.deleted_at = ahora
        await _vaciar_fragmentos(session, tenant_id, document_id, ahora)
        ruta = doc.ruta_archivo
    archivos.borrar(await _slug(tenant_id), ruta)
    log.info("knowledge_documento_borrado", document_id=str(document_id))


async def reprocesar(agent_id: uuid.UUID, document_id: uuid.UUID) -> KnowledgeDocument:
    """Vuelve a `pendiente` y encola la extracción (documento fallido o con un troceado viejo)."""
    tenant_id = _tenant()
    async with get_session() as session:
        doc = await _documento(session, agent_id, document_id)
        doc.estado = "pendiente"
        doc.error_codigo = None
        doc.error_detalle = None
    _encolar(TAREA_EXTRAER, tenant_id, document_id)
    return doc


async def _vaciar_fragmentos(session: Any, tenant_id: uuid.UUID, document_id: uuid.UUID, ahora: datetime) -> None:
    await session.execute(
        update(KnowledgeChunk)
        .where(
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeChunk.document_id == document_id,
            KnowledgeChunk.deleted_at.is_(None),
        )
        .values(deleted_at=ahora, contenido=None, embedding=None)
    )


# ------------------------------------------------------------------ worker
async def _cargar_para_tarea(session: Any, tenant_id: uuid.UUID, document_id: uuid.UUID) -> KnowledgeDocument | None:
    doc = (
        await session.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id, KnowledgeDocument.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if doc is None:
        log.warning("knowledge_tarea_sin_documento", document_id=str(document_id))
        return None
    if doc.deleted_at is not None:
        return None
    return doc


async def _fallar(tenant_id: uuid.UUID, document_id: uuid.UUID, codigo: str, detalle: str | None) -> None:
    async with get_session() as session:
        doc = await _cargar_para_tarea(session, tenant_id, document_id)
        if doc is None:
            return
        doc.estado = "fallido"
        doc.error_codigo = codigo
        doc.error_detalle = detalle
        doc.processed_at = _ahora()
    log.warning("knowledge_documento_fallido", document_id=str(document_id), codigo=codigo, detalle=detalle)


def _ids(tenant_id: str, document_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.UUID(tenant_id), uuid.UUID(document_id)


async def tarea_extraer(*, tenant_id: str, document_id: str) -> None:
    tid, did = _ids(tenant_id, document_id)
    async with tenant_scope(tid):
        try:
            await _extraer(tid, did)
        except Exception as exc:
            # Un error inesperado no deja el documento en `procesando`. Solo la clase: un error de
            # SQLAlchemy trae los parámetros (el texto de los fragmentos).
            await _fallar(tid, did, "interno", type(exc).__name__)


async def _extraer(tid: uuid.UUID, did: uuid.UUID) -> None:
    async with get_session() as session:
        doc = await _cargar_para_tarea(session, tid, did)
        if doc is None or doc.estado not in ("pendiente", "procesando"):
            return
        doc.estado = "procesando"
        mime, sha, ruta = doc.mime, doc.sha256, doc.ruta_archivo

    ubicacion = archivos.resolver(await _slug(tid), ruta)
    if ubicacion is None:
        await _fallar(tid, did, "formato", "archivo_ausente")
        return
    contenido = await asyncio.to_thread(ubicacion.read_bytes)
    if hashlib.sha256(contenido).hexdigest() != sha:
        await _fallar(tid, did, "formato", "archivo_cambiado")
        return
    try:
        extraido = await asyncio.to_thread(extraer_texto, mime, contenido)
        fragmentos = trocear(extraido.bloques)
    except ExtraccionError as exc:
        await _fallar(tid, did, exc.codigo, exc.detalle)
        return
    except DemasiadosFragmentos:
        await _fallar(tid, did, "muy_grande", "fragmentos")
        return
    pii = sum(contar_pii(f.contenido) for f in fragmentos)

    async with get_session() as session:
        doc = await _cargar_para_tarea(session, tid, did)
        if doc is None or doc.estado != "procesando" or doc.sha256 != sha:
            return
        await _vaciar_fragmentos(session, tid, did, _ahora())
        session.add_all(
            KnowledgeChunk(
                document_id=did,
                ordinal=f.ordinal,
                contenido=f.contenido,
                seccion=f.seccion,
                pagina_desde=f.pagina_desde,
                pagina_hasta=f.pagina_hasta,
                tokens=f.tokens,
            )
            for f in fragmentos
        )
        doc.paginas = extraido.paginas
        doc.num_fragmentos = len(fragmentos)
        doc.pii_detectada = pii
        doc.chunker_version = CHUNKER_VERSION
        doc.tokens_embebidos = 0
        doc.error_codigo = None
        doc.error_detalle = None
    log.info("knowledge_documento_extraido", document_id=str(did), fragmentos=len(fragmentos), pii=pii)
    _encolar(TAREA_EMBEBER, tid, did, sha256=sha)


async def tarea_embeber(*, tenant_id: str, document_id: str, sha256: str) -> None:
    tid, did = _ids(tenant_id, document_id)
    async with tenant_scope(tid):
        try:
            await _embeber(tid, did, sha256)
        except Exception as exc:
            await _fallar(tid, did, "interno", type(exc).__name__)


async def _embeber(tid: uuid.UUID, did: uuid.UUID, sha256: str) -> None:
    async with get_session() as session:
        doc = (
            await session.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == did, KnowledgeDocument.tenant_id == tid)
            )
        ).scalar_one_or_none()
        if doc is None or doc.deleted_at is not None or doc.estado != "procesando" or doc.sha256 != sha256:
            return
        lote = (
            await session.execute(
                select(KnowledgeChunk.id, KnowledgeChunk.contenido)
                .where(*_sin_vector(tid, did))
                .order_by(KnowledgeChunk.ordinal)
                .limit(MAX_LOTE)
            )
        ).all()

    if lote:
        try:
            embebidos = await embeber_conocimiento([c.contenido for c in lote], proposito="ingesta", document_id=did)
        except EmbeddingsError as exc:
            await _fallar(tid, did, "proveedor", exc.codigo)
            return

    async with get_session() as session:
        doc = await _cargar_para_tarea(session, tid, did)
        if doc is None or doc.estado != "procesando" or doc.sha256 != sha256:
            return
        if lote:
            for (chunk_id, _), vector in zip(lote, embebidos.vectores, strict=True):
                await session.execute(
                    update(KnowledgeChunk)
                    .where(
                        KnowledgeChunk.id == chunk_id,
                        KnowledgeChunk.tenant_id == tid,
                        KnowledgeChunk.deleted_at.is_(None),
                        KnowledgeChunk.embedding.is_(None),
                    )
                    .values(embedding=vector)
                )
            doc.tokens_embebidos = (doc.tokens_embebidos or 0) + embebidos.tokens
            doc.embedding_model = embebidos.modelo
            doc.embedding_dim = DIMENSION
        quedan = int(
            (await session.execute(select(func.count()).select_from(KnowledgeChunk).where(*_sin_vector(tid, did))))
            .scalar_one()
        )
        if quedan == 0:
            doc.estado = "listo"
            doc.processed_at = _ahora()
    if quedan:
        _encolar(TAREA_EMBEBER, tid, did, sha256=sha256)
    else:
        log.info("knowledge_documento_listo", document_id=str(did))


def _sin_vector(tenant_id: uuid.UUID, document_id: uuid.UUID) -> list[Any]:
    return [
        KnowledgeChunk.tenant_id == tenant_id,
        KnowledgeChunk.document_id == document_id,
        KnowledgeChunk.deleted_at.is_(None),
        KnowledgeChunk.embedding.is_(None),
    ]
