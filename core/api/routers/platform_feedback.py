"""Revisión de reportes de clientes por el equipo de SolversHub: `/api/v1/platform/feedback/reports`
(SPEC-54, HU-FB-04, ADR-0213 §6-8).

- `GET /` bandeja entre negocios y `GET /{id}` detalle (con `nota_interna` y adjuntos firmados): quien actúa
  como master (master, o tester en test).
- `POST /{id}/tomar`, `/aprobar` y `/rechazar`: solo `role == "master"`. Aprobar regala créditos con costo
  real y el tester es una cuenta de pruebas (ADR-0213 §8).

El negocio activo de la sesión no se usa: un master sin `X-Tenant-Id` llega con el primero de sus negocios en
el `tenant_scope` de `get_principal`. Los ids y negocios de la bandeja salen de las funciones `SECURITY DEFINER`
(`feedback_bandeja`, `feedback_reporte_tenant`) y el contenido se lee bajo el `tenant_scope` de cada reporte.
Detrás de `FEEDBACK_ENABLED` (404 apagada).
"""

from __future__ import annotations

import base64
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints
from sqlalchemy import select, text

from core.admin.roles import actua_como_master, es_master
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError
from core.config import get_settings
from core.db.session import get_session
from core.feedback import revision
from core.shared.tenant import tenant_scope

router = APIRouter(prefix="/platform/feedback", tags=["plataforma · reportes de clientes"])

Estado = Literal["recibido", "en_revision", "aprobado", "rechazado"]
Tipo = Literal["error", "mejora", "idea"]
MotivoCodigo = Literal["duplicado", "no_reproducible", "fuera_de_alcance", "informacion_insuficiente", "otro"]

LIMITE_POR_DEFECTO = 25
LIMITE_MAXIMO = 50  # se piden limit+1 filas y `feedback_bandeja` recorta a 100


# ------------------------------------------------------------------------------- autorización
def _exigir_bandera() -> None:
    if not get_settings().feedback_enabled:
        raise ApiError.not_found()


def _require_lector(principal: Principal) -> None:
    # Ver la bandeja: master, o tester en test (ADR-0134), como `platform._require_master`.
    _exigir_bandera()
    if not actua_como_master(principal.admin):
        raise ApiError.forbidden("Solo el equipo de SolversHub revisa los reportes.")


def _require_master_estricto(principal: Principal) -> None:
    # Decidir: solo el master. El tester no regala créditos ni decide reportes (ADR-0213 §8).
    _exigir_bandera()
    if not es_master(principal.admin):
        raise ApiError.forbidden("Solo el master decide los reportes.")


def _api_error(exc: revision.ErrorDeRevision) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message, details=exc.details)


# ------------------------------------------------------------------------------------- schemas
class FeedbackPersona(BaseModel):
    id: uuid.UUID
    nombre: str | None
    email: str | None


class FeedbackBandejaItem(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    created_at: datetime
    estado: Estado
    tipo: Tipo
    area: str
    titulo: str
    autor: FeedbackPersona
    revisor: FeedbackPersona | None = Field(description="Quien lo tomó o lo decidió.")
    tomado_en: datetime | None
    creditos_otorgados: int | None
    adjuntos: int = Field(description="Cuántos adjuntos trae el reporte.")


class FeedbackBandeja(BaseModel):
    items: list[FeedbackBandejaItem]
    next_cursor: str | None


class FeedbackAdjuntoMaster(BaseModel):
    id: uuid.UUID
    orden: int
    mimetype: str
    bytes: int
    nombre_original: str | None
    url: str = Field(description="URL firmada de corta duración (`URL_TTL_UI`).")


class FeedbackReporteMaster(BaseModel):
    """El reporte como lo ve el equipo: incluye `nota_interna`, el revisor y a qué reporte duplica."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    tenant_name: str
    autor: FeedbackPersona
    tipo: Tipo
    area: str
    titulo: str
    descripcion: str
    estado: Estado
    revisor: FeedbackPersona | None = Field(description="Quien lo tomó o lo decidió.")
    tomado_en: datetime | None
    decidido_en: datetime | None
    motivo_codigo: MotivoCodigo | None
    motivo: str | None
    nota_interna: str | None
    creditos_otorgados: int | None
    duplicado_de: uuid.UUID | None
    created_at: datetime
    adjuntos: list[FeedbackAdjuntoMaster]


class FeedbackAprobarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Sin `gt=0`: el 0 y los negativos responden 422 `monto_invalido` (código de dominio), no `validation_error`.
    creditos: StrictInt = Field(description="Créditos de regalo para el negocio, entero > 0. Topes en ADR-0213 §4.")


class FeedbackRechazarIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    motivo_codigo: MotivoCodigo
    motivo: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)] = Field(
        description="Visible al cliente."
    )
    nota_interna: str | None = Field(default=None, max_length=2000, description="Solo para el equipo.")
    duplicado_de: uuid.UUID | None = Field(default=None, description="Obligatorio con `motivo_codigo = duplicado`.")


# --------------------------------------------------------------------------------- utilidades
def _cursor(created_at: datetime, report_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{report_id}".encode()).decode()


def _parse_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, _, rid = base64.urlsafe_b64decode(raw.encode()).decode().partition("|")
        return datetime.fromisoformat(ts), uuid.UUID(rid)
    except ValueError as exc:
        raise ApiError(400, "cursor_invalido", "El cursor de paginación no es válido.") from exc


async def _nombres_de_negocio(ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    from core.db.models import Tenant

    if not ids:
        return {}
    async with get_session() as session:
        filas = (await session.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(ids)))).all()
    return {f.id: f.name for f in filas}


async def _autores(ids: set[uuid.UUID]) -> dict[uuid.UUID, FeedbackPersona]:
    from core.db.models import Admin

    if not ids:
        return {}
    async with get_session() as session:
        filas = (await session.execute(select(Admin.id, Admin.nombre, Admin.email).where(Admin.id.in_(ids)))).all()
    return {f.id: FeedbackPersona(id=f.id, nombre=f.nombre, email=f.email) for f in filas}


def _autor(autores: dict[uuid.UUID, FeedbackPersona], admin_id: uuid.UUID) -> FeedbackPersona:
    return autores.get(admin_id) or FeedbackPersona(id=admin_id, nombre=None, email=None)


async def _adjuntos(session, report_id: uuid.UUID) -> list[FeedbackAdjuntoMaster]:  # type: ignore[no-untyped-def]
    from core import almacenamiento

    filas = (
        await session.execute(
            text(
                "SELECT id, orden, storage_key, mimetype, bytes, nombre_original FROM feedback_attachments "
                "WHERE report_id = :id ORDER BY orden"
            ),
            {"id": report_id},
        )
    ).mappings().all()
    return [
        FeedbackAdjuntoMaster(
            id=f["id"],
            orden=f["orden"],
            mimetype=f["mimetype"],
            bytes=f["bytes"],
            nombre_original=f["nombre_original"],
            url=almacenamiento.url(f["storage_key"], almacenamiento.URL_TTL_UI),
        )
        for f in filas
    ]


async def _detalle(report_id: uuid.UUID, reporte: dict[str, Any] | None = None) -> FeedbackReporteMaster:
    tenant_id = await revision.negocio_del_reporte(report_id)
    if tenant_id is None:
        raise ApiError.not_found("No existe ese reporte.")
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if reporte is None:
                reporte = await revision.leer_reporte(session, report_id)
                if reporte is None:
                    raise ApiError.not_found("No existe ese reporte.")
            adjuntos = await _adjuntos(session, report_id)
    nombres = await _nombres_de_negocio({tenant_id})
    autores = await _autores({reporte["autor_admin_id"], *([reporte["revisado_por"]] if reporte["revisado_por"] else [])})
    return FeedbackReporteMaster(
        id=reporte["id"],
        tenant_id=tenant_id,
        tenant_name=nombres.get(tenant_id, ""),
        autor=_autor(autores, reporte["autor_admin_id"]),
        tipo=reporte["tipo"],
        area=reporte["area"],
        titulo=reporte["titulo"],
        descripcion=reporte["descripcion"],
        estado=reporte["estado"],
        revisor=_autor(autores, reporte["revisado_por"]) if reporte["revisado_por"] else None,
        tomado_en=reporte["tomado_en"],
        decidido_en=reporte["decidido_en"],
        motivo_codigo=reporte["motivo_codigo"],
        motivo=reporte["motivo"],
        nota_interna=reporte["nota_interna"],
        creditos_otorgados=reporte["creditos_otorgados"],
        duplicado_de=reporte["duplicado_de"],
        created_at=reporte["created_at"],
        adjuntos=adjuntos,
    )


# ------------------------------------------------------------------------------------- rutas
_COMUNES: dict[int | str, dict[str, Any]] = {
    403: {"description": "`forbidden`: no actúa como master (o, al decidir, no es master estricto)"},
    404: {"description": "`not_found`: el reporte no existe o `FEEDBACK_ENABLED` está apagada"},
}


@router.get(
    "/reports",
    response_model=FeedbackBandeja,
    responses={**_COMUNES, 400: {"description": "`cursor_invalido`"}},
)
async def bandeja(
    principal: Annotated[Principal, Depends(get_principal)],
    estado: Annotated[Estado | None, Query()] = None,
    tipo: Annotated[Tipo | None, Query()] = None,
    tenant_id: Annotated[uuid.UUID | None, Query(description="Solo los reportes de este negocio.")] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=LIMITE_MAXIMO)] = LIMITE_POR_DEFECTO,
) -> FeedbackBandeja:
    """Reportes de todos los negocios, del más reciente al más antiguo, paginados por `(created_at, id)`."""
    _require_lector(principal)
    antes = _parse_cursor(cursor) if cursor else (None, None)
    async with tenant_scope(None):
        async with get_session() as session:
            ids = (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, created_at, estado, tipo "
                        "FROM feedback_bandeja(:e, :t, :n, :cts, :cid, :lim)"
                    ),
                    {"e": estado, "t": tipo, "n": tenant_id, "cts": antes[0], "cid": antes[1], "lim": limit + 1},
                )
            ).all()
    hay_mas = len(ids) > limit
    ids = ids[:limit]

    # Contenido bajo la RLS de cada negocio: una consulta por negocio de la página, no por reporte.
    por_negocio: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for fila in ids:
        por_negocio[fila.tenant_id].append(fila.id)
    contenido: dict[uuid.UUID, dict[str, Any]] = {}
    for negocio, report_ids in por_negocio.items():
        async with tenant_scope(negocio):
            async with get_session() as session:
                filas = (
                    await session.execute(
                        text(
                            "SELECT r.id, r.autor_admin_id, r.revisado_por, r.tomado_en, r.area, r.titulo, r.creditos_otorgados, "
                            "(SELECT count(*) FROM feedback_attachments a WHERE a.report_id = r.id) AS adjuntos "
                            "FROM feedback_reports r WHERE r.id = ANY(:ids)"
                        ),
                        {"ids": report_ids},
                    )
                ).mappings().all()
        contenido.update({f["id"]: dict(f) for f in filas})

    nombres = await _nombres_de_negocio(set(por_negocio))
    autores = await _autores(
        {c["autor_admin_id"] for c in contenido.values()} | {c["revisado_por"] for c in contenido.values() if c["revisado_por"]}
    )
    items = [
        FeedbackBandejaItem(
            id=f.id,
            tenant_id=f.tenant_id,
            tenant_name=nombres.get(f.tenant_id, ""),
            created_at=f.created_at,
            estado=f.estado,
            tipo=f.tipo,
            area=contenido[f.id]["area"],
            titulo=contenido[f.id]["titulo"],
            autor=_autor(autores, contenido[f.id]["autor_admin_id"]),
            revisor=_autor(autores, contenido[f.id]["revisado_por"]) if contenido[f.id]["revisado_por"] else None,
            tomado_en=contenido[f.id]["tomado_en"],
            creditos_otorgados=contenido[f.id]["creditos_otorgados"],
            adjuntos=int(contenido[f.id]["adjuntos"]),
        )
        for f in ids
        if f.id in contenido
    ]
    return FeedbackBandeja(
        items=items, next_cursor=_cursor(ids[-1].created_at, ids[-1].id) if hay_mas and ids else None
    )


@router.get("/reports/{report_id}", response_model=FeedbackReporteMaster, responses=_COMUNES)
async def detalle(report_id: uuid.UUID, principal: Annotated[Principal, Depends(get_principal)]) -> FeedbackReporteMaster:
    _require_lector(principal)
    return await _detalle(report_id)


@router.post(
    "/reports/{report_id}/tomar",
    response_model=FeedbackReporteMaster,
    responses={**_COMUNES, 409: {"description": "`reporte_ya_tomado` (`details.revisado_por`) o `reporte_ya_decidido`"}},
)
async def tomar(report_id: uuid.UUID, principal: Annotated[Principal, Depends(get_principal)]) -> FeedbackReporteMaster:
    """`recibido → en_revision`. Repetirlo el mismo master responde 200; si lo tomó otro, 409."""
    _require_master_estricto(principal)
    try:
        resultado = await revision.tomar(report_id, revisor_id=principal.admin.id, revisor_email=principal.admin.email)
    except revision.ErrorDeRevision as exc:
        raise _api_error(exc) from None
    return await _detalle(report_id, resultado.reporte)


@router.post(
    "/reports/{report_id}/aprobar",
    response_model=FeedbackReporteMaster,
    responses={
        **_COMUNES,
        409: {"description": "`reporte_ya_decidido` (`details.estado`) o `sin_ciclo_de_creditos`"},
        422: {
            "description": "`monto_invalido`, `tope_reporte_excedido` (`details.maximo`) o `tope_mensual_excedido` "
            "(`details.maximo`, `regalado`, `disponible`); `validation_error` si `creditos` no es un entero"
        },
        500: {"description": "`abono_inconsistente`: no entró el abono y se deshizo la aprobación"},
    },
)
async def aprobar(
    report_id: uuid.UUID, payload: FeedbackAprobarIn, principal: Annotated[Principal, Depends(get_principal)]
) -> FeedbackReporteMaster:
    """Aprueba y acredita `creditos` de regalo al negocio, en una sola transacción. El mismo master con el
    mismo monto responde 200 sin volver a acreditar; cualquier otra repetición, 409 `reporte_ya_decidido`."""
    _require_master_estricto(principal)
    try:
        resultado = await revision.aprobar(
            report_id, revisor_id=principal.admin.id, revisor_email=principal.admin.email, creditos=payload.creditos
        )
    except revision.ErrorDeRevision as exc:
        raise _api_error(exc) from None
    return await _detalle(report_id, resultado.reporte)


@router.post(
    "/reports/{report_id}/rechazar",
    response_model=FeedbackReporteMaster,
    responses={
        **_COMUNES,
        409: {"description": "`reporte_ya_decidido` (`details.estado`)"},
        422: {"description": "`duplicado_sin_original`, `duplicado_invalido` o `duplicado_sin_motivo`"},
    },
)
async def rechazar(
    report_id: uuid.UUID, payload: FeedbackRechazarIn, principal: Annotated[Principal, Depends(get_principal)]
) -> FeedbackReporteMaster:
    """Rechaza con un motivo visible al cliente. `duplicado_de` se valida contra cualquier negocio y el cliente
    nunca ve a qué reporte apunta."""
    _require_master_estricto(principal)
    try:
        resultado = await revision.rechazar(
            report_id,
            revisor_id=principal.admin.id,
            revisor_email=principal.admin.email,
            motivo_codigo=payload.motivo_codigo,
            motivo=payload.motivo,
            nota_interna=(payload.nota_interna or "").strip() or None,
            duplicado_de=payload.duplicado_de,
        )
    except revision.ErrorDeRevision as exc:
        raise _api_error(exc) from None
    return await _detalle(report_id, resultado.reporte)


__all__ = ["router"]
