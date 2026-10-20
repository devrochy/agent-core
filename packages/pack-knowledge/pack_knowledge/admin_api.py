"""API admin del pack de conocimiento (ADR-0079, ADR-0145 §2; HU-AOS-49 parte 2).

Montada en `/api/v1/pack/pack_knowledge` (prefijo obligatorio de los packs):

- `GET    /agents/{agent_id}/documents` — documentos del agente con su estado.
- `POST   /agents/{agent_id}/documents` — sube un PDF, DOCX, TXT o Markdown (multipart, ≤ 20 MB).
  Responde 202: el documento queda `pendiente` y el worker lo procesa.
- `DELETE /agents/{agent_id}/documents/{document_id}` — borrado lógico que vacía el contenido.
- `POST   /agents/{agent_id}/documents/{document_id}/reprocess` — vuelve a procesarlo.

Permisos: sección `agents` (ADR-0140), escritura para subir, borrar y reprocesar. El agente se
resuelve dentro del negocio de la sesión: un id de otro negocio, el Coordinador o un documento de
otro agente responden 404, sin eco del id.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import BaseModel

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.plan_limits import plan_limit_error, plan_limit_responses
from core.billing.enforcement import QuotaExceeded
from pack_knowledge import archivos, ingesta
from pack_knowledge.models import CHUNKER_VERSION, KnowledgeDocument

router = APIRouter(prefix="/api/v1/pack/pack_knowledge", tags=["pack · conocimiento"])

_NO_AGENTE = "Agente no encontrado."
_NO_DOCUMENTO = "Documento no encontrado."


class DocumentoOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    titulo: str
    mime: str
    bytes: int
    estado: str
    # Código corto (`sin_texto`, `muy_grande`, `formato`, `limite_plan`, `proveedor`, `interno`) o null.
    error_codigo: str | None
    paginas: int | None
    num_fragmentos: int
    # Coincidencias del detector de datos personales: el panel avisa, no bloquea (ADR-0145 §6).
    pii_detectada: int
    # Procesado con un troceado anterior al vigente: conviene reprocesarlo.
    desactualizado: bool
    created_at: datetime
    processed_at: datetime | None


def _out(doc: KnowledgeDocument) -> DocumentoOut:
    return DocumentoOut(
        id=doc.id,
        agent_id=doc.agent_id,
        titulo=doc.titulo,
        mime=doc.mime,
        bytes=doc.bytes,
        estado=doc.estado,
        error_codigo=doc.error_codigo,
        paginas=doc.paginas,
        num_fragmentos=doc.num_fragmentos or 0,
        pii_detectada=doc.pii_detectada or 0,
        desactualizado=doc.estado == "listo" and doc.chunker_version != CHUNKER_VERSION,
        created_at=doc.created_at,
        processed_at=doc.processed_at,
    )


@router.get("/agents/{agent_id}/documents", response_model=list[DocumentoOut])
async def listar_documentos(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents"))],
) -> list[DocumentoOut]:
    try:
        return [_out(d) for d in await ingesta.listar(agent_id)]
    except ingesta.AgenteNoEncontrado:
        raise ApiError.not_found(_NO_AGENTE) from None


@router.post(
    "/agents/{agent_id}/documents",
    response_model=DocumentoOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        **plan_limit_responses("documentos"),
        413: {"description": "El archivo supera 20 MB (`error.code = muy_grande`)."},
        422: {"description": "Tipo o contenido no admitido (`error.code = formato | vacio`)."},
    },
)
async def subir_documento(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
    archivo: Annotated[UploadFile, File()],
    titulo: Annotated[str | None, Form(max_length=ingesta.MAX_TITULO)] = None,
) -> DocumentoOut:
    """Sube un documento al agente. Queda `pendiente` y el worker lo procesa."""
    contenido = await archivo.read(archivos.MAX_BYTES + 1)
    try:
        tipo = archivos.tipo_de(archivo.content_type, archivo.filename)
        doc = await ingesta.crear_documento(
            agent_id=agent_id,
            titulo=ingesta.titulo_de(titulo, archivo.filename),
            tipo=tipo,
            contenido=contenido,
            created_by=principal.admin.id,
        )
    except archivos.ArchivoInvalido as exc:
        status_code = 413 if exc.codigo == "muy_grande" else 422
        raise ApiError(status_code, exc.codigo, str(exc)) from None
    except ingesta.AgenteNoEncontrado:
        raise ApiError.not_found(_NO_AGENTE) from None
    except ingesta.DocumentoDuplicado as exc:
        raise ApiError(
            409,
            "documento_duplicado",
            "Este archivo ya está subido para este agente.",
            details={"document_id": str(exc.document_id)},
        ) from None
    except QuotaExceeded as exc:
        raise plan_limit_error(exc) from None
    return _out(doc)


@router.delete("/agents/{agent_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def borrar_documento(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> None:
    try:
        await ingesta.borrar_documento(agent_id, document_id)
    except ingesta.DocumentoNoEncontrado:
        raise ApiError.not_found(_NO_DOCUMENTO) from None


@router.post(
    "/agents/{agent_id}/documents/{document_id}/reprocess",
    response_model=DocumentoOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprocesar_documento(
    agent_id: uuid.UUID,
    document_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("agents", write=True))],
) -> DocumentoOut:
    try:
        return _out(await ingesta.reprocesar(agent_id, document_id))
    except ingesta.DocumentoNoEncontrado:
        raise ApiError.not_found(_NO_DOCUMENTO) from None
