"""Plataforma (master): controles del alta automática (HU-SH-07, ADR-0125).

`/api/v1/admin/platform/altas-automaticas`. Solo el admin `master`, y solo sobre negocios de
la allowlist `PROVISIONING_SELLER_TENANTS` (cualquier otro id responde 404):

- el estado de cada vendedor: interruptor, documentos legales vigentes y altas en revisión;
- pausar o reanudar el alta automática (el interruptor ante un ataque);
- publicar una versión nueva de términos, privacidad o DPA, y ver las anteriores;
- la cola de altas en revisión: aprobar o descartar. Aprobar no corre el alta acá: la deja
  `en_curso` y emite `provisioning.alta_aprobada` por el outbox; la corre el worker. Ninguna
  ruta de la API llega al consumidor (`tests/test_provisioning_sin_llm.py`).

Nada de esto devuelve datos personales del comprador: ni correo ni teléfono ni documento.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.db.models import ProvisioningRequest, Tenant
from core.db.session import get_session
from core.provisioning import interruptor, legal, revision
from core.provisioning.vendedores import vendedores_configurados
from core.shared.tenant import tenant_scope

router = APIRouter(prefix="/admin/platform/altas-automaticas", tags=["admin · plataforma"])

EstadoAlta = Literal["en_curso", "completado", "revision", "omitido"]


def _require_master(principal: Principal) -> None:
    if principal.admin.role != "master":
        raise ApiError.forbidden("Solo el admin de plataforma puede gestionar el alta automática.")


def _actor(principal: Principal) -> str:
    return f"admin:{principal.admin.email}"


async def _vendedor(vendedor_id: uuid.UUID) -> Tenant:
    """El vendedor, si está en la allowlist y existe. Si no, 404 (sin distinguir)."""
    if vendedor_id not in vendedores_configurados():
        raise ApiError.not_found("Ese negocio no vende negocios.")
    async with get_session() as session:
        tenant = await session.get(Tenant, vendedor_id)
    if tenant is None or tenant.deleted_at is not None:
        raise ApiError.not_found("Ese negocio no vende negocios.")
    return tenant


# ------------------------------------------------------------------ modelos
class Interruptor(BaseModel):
    pausado: bool
    motivo: str | None = None
    por: str | None = None
    en: str | None = None


class DocumentoLegalOut(BaseModel):
    tipo: str
    nombre: str
    version: str
    url: str
    vigente_desde: datetime
    publicado_por: str | None = None


class VendedorAltas(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    activo: bool
    interruptor: Interruptor
    documentos: list[DocumentoLegalOut]
    documentos_faltantes: list[str]
    en_revision: int


class InterruptorIn(BaseModel):
    pausado: bool
    motivo: str | None = Field(default=None, max_length=300)


class DocumentoLegalIn(BaseModel):
    tipo: Literal["terminos", "privacidad", "dpa"]
    version: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=10, max_length=500)


class AltaOut(BaseModel):
    referencia: str
    estado: str
    motivo: str | None
    intentos: int
    plan_id: str | None
    negocio_slug: str | None
    aprobada: bool
    created_at: datetime
    updated_at: datetime


class DescartarIn(BaseModel):
    motivo: str = Field(min_length=3, max_length=300)


def _doc_out(doc: legal.Documento) -> DocumentoLegalOut:
    return DocumentoLegalOut(
        tipo=doc.tipo,
        nombre=doc.nombre,
        version=doc.version,
        url=doc.url,
        vigente_desde=doc.vigente_desde,
        publicado_por=doc.publicado_por,
    )


def _alta_out(fila: ProvisioningRequest) -> AltaOut:
    return AltaOut(
        referencia=fila.referencia,
        estado=fila.estado,
        motivo=fila.motivo,
        intentos=fila.intentos,
        plan_id=fila.plan_id,
        negocio_slug=fila.negocio_slug,
        aprobada=bool((fila.detalle or {}).get("aprobacion")),
        created_at=fila.created_at,
        updated_at=fila.updated_at,
    )


async def _en_revision(vendedor_id: uuid.UUID) -> int:
    async with tenant_scope(vendedor_id):
        async with get_session() as session:
            return int(
                (
                    await session.execute(
                        select(func.count(ProvisioningRequest.id)).where(
                            ProvisioningRequest.tenant_id == vendedor_id,
                            ProvisioningRequest.estado == "revision",
                            ProvisioningRequest.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
            )


async def _resumen(tenant: Tenant) -> VendedorAltas:
    vigentes = await legal.documentos_vigentes(tenant.id)
    return VendedorAltas(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        activo=tenant.status == "active",
        interruptor=Interruptor(**await interruptor.estado(tenant.id)),
        documentos=[_doc_out(vigentes[t]) for t in legal.TIPOS if t in vigentes],
        documentos_faltantes=legal.faltantes(vigentes),
        en_revision=await _en_revision(tenant.id),
    )


# ------------------------------------------------------------------ rutas
@router.get("", response_model=list[VendedorAltas])
async def listar_vendedores(
    principal: Annotated[Principal, Depends(get_principal)],
) -> list[VendedorAltas]:
    """Cada negocio de la allowlist con su interruptor, documentos y altas en revisión."""
    _require_master(principal)
    ids = sorted(vendedores_configurados(), key=str)
    if not ids:
        return []
    async with get_session() as session:
        tenants = (
            await session.execute(select(Tenant).where(Tenant.id.in_(ids), Tenant.deleted_at.is_(None)))
        ).scalars().all()
    return [await _resumen(t) for t in sorted(tenants, key=lambda t: t.slug)]


@router.put("/{vendedor_id}/interruptor", response_model=VendedorAltas)
async def fijar_interruptor(
    vendedor_id: uuid.UUID,
    payload: InterruptorIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> VendedorAltas:
    """Pausa o reanuda el alta automática. Pausada, cada alta nueva queda en revisión."""
    _require_master(principal)
    tenant = await _vendedor(vendedor_id)
    if payload.pausado and not (payload.motivo or "").strip():
        raise ApiError.bad_request("Indica por qué se pausa.", fields={"motivo": "Obligatorio al pausar."})
    await interruptor.fijar(vendedor_id, pausar=payload.pausado, motivo=payload.motivo, por=_actor(principal))
    return await _resumen(tenant)


@router.get("/{vendedor_id}/documentos", response_model=list[DocumentoLegalOut])
async def historial_de_documentos(
    vendedor_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
) -> list[DocumentoLegalOut]:
    """Todas las versiones publicadas, la más nueva primero."""
    _require_master(principal)
    await _vendedor(vendedor_id)
    return [_doc_out(d) for d in await legal.historial(vendedor_id)]


@router.post("/{vendedor_id}/documentos", response_model=DocumentoLegalOut, status_code=201)
async def publicar_documento(
    vendedor_id: uuid.UUID,
    payload: DocumentoLegalIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> DocumentoLegalOut:
    """Publica una versión nueva de un documento legal, vigente desde ya. Las altas que
    se pidan desde ahora aceptan esta versión; las anteriores conservan la suya."""
    _require_master(principal)
    await _vendedor(vendedor_id)
    try:
        doc = await legal.publicar(
            vendedor_id, tipo=payload.tipo, version=payload.version, url=payload.url, por=_actor(principal)
        )
    except legal.VersionRepetida as exc:
        raise ApiError(409, "version_exists", str(exc)) from exc
    except legal.DocumentoInvalido as exc:
        raise ApiError.bad_request(str(exc), "invalid_document") from exc
    return _doc_out(doc)


@router.get("/{vendedor_id}/altas", response_model=list[AltaOut])
async def listar_altas(
    vendedor_id: uuid.UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    estado: Annotated[EstadoAlta | Literal["todas"], Query()] = "revision",
    limite: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[AltaOut]:
    """Altas del vendedor, las más nuevas primero (por defecto, las que están en revisión)."""
    _require_master(principal)
    await _vendedor(vendedor_id)
    consulta = select(ProvisioningRequest).where(
        ProvisioningRequest.tenant_id == vendedor_id, ProvisioningRequest.deleted_at.is_(None)
    )
    if estado != "todas":
        consulta = consulta.where(ProvisioningRequest.estado == estado)
    async with tenant_scope(vendedor_id):
        async with get_session() as session:
            filas = (
                await session.execute(consulta.order_by(ProvisioningRequest.created_at.desc()).limit(limite))
            ).scalars().all()
    return [_alta_out(f) for f in filas]


@router.post("/{vendedor_id}/altas/{referencia}/aprobar", response_model=AltaOut)
async def aprobar(
    vendedor_id: uuid.UUID,
    referencia: str,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AltaOut:
    """Aprueba un alta en revisión: queda `en_curso` y el worker la corre en segundos, sin
    los controles de riesgo que la frenaron (por el outbox, con sus reintentos)."""
    _require_master(principal)
    await _vendedor(vendedor_id)
    try:
        fila = await revision.aprobar_alta(vendedor_id, referencia[:120], por=_actor(principal))
    except ValueError as exc:
        raise ApiError(409, "alta_no_revisable", str(exc)[:180]) from exc
    return _alta_out(fila)


@router.post("/{vendedor_id}/altas/{referencia}/descartar", response_model=AltaOut)
async def descartar(
    vendedor_id: uuid.UUID,
    referencia: str,
    payload: DescartarIn,
    principal: Annotated[Principal, Depends(get_principal)],
) -> AltaOut:
    """Cierra un alta en revisión sin crear el negocio."""
    _require_master(principal)
    await _vendedor(vendedor_id)
    try:
        fila = await revision.descartar_alta(vendedor_id, referencia[:120], payload.motivo, por=_actor(principal))
    except ValueError as exc:
        raise ApiError(409, "alta_no_revisable", str(exc)[:180]) from exc
    return _alta_out(fila)
