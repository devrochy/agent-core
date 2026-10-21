"""Acciones sobre un segmento de Clientes y consentimiento (HU-CRM-06, ADR-0150).

- `GET  /admin/leads/acciones/plantilla/conexiones`: conexiones de WhatsApp Cloud con sus
  plantillas `MARKETING` aprobadas.
- `POST /admin/leads/acciones/plantilla/vista-previa`: con los mismos filtros de `GET /admin/leads`,
  cuenta destinatarios y excluidos por motivo y guarda el envío en `borrador`.
- `POST /admin/leads/acciones/plantilla/{envio_id}/confirmar` (`Idempotency-Key`): lo confirma.
- `GET  /admin/leads/acciones/plantilla/{envio_id}`: estado del envío.
- `GET/POST /admin/leads/{lead_id}/consentimiento`: consentimiento del cliente para promociones.

El envío es solo del propietario del negocio o un admin de plataforma (D-SPEC-33-8).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from core.admin import envios_segmento as envios
from core.admin.roles import actua_como_master
from core.api.deps import Principal, require, require_owner_or_master
from core.api.errors import ApiError
from core.api.routers.leads import FiltroQuery, _consulta, _providers
from core.db.models import ChannelConnection, CustomerConsent
from core.db.session import get_session

router = APIRouter(prefix="/admin/leads", tags=["admin · leads"])


def _error(exc: envios.EnvioError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def _quien(principal: Principal) -> str:
    # Un admin de plataforma no deja su correo personal a la vista del negocio (como las notas).
    return "Soporte de la plataforma" if actua_como_master(principal.admin) else (principal.admin.email or "")


# ------------------------------------------------------------------ modelos


class PlantillaDeEnvio(BaseModel):
    id: uuid.UUID
    name: str
    language: str
    body: str
    variables: list[str]


class ConexionDeEnvioOut(BaseModel):
    id: uuid.UUID
    label: str
    plantillas: list[PlantillaDeEnvio]
    motivo_pausa: str | None = None


class VariableIn(BaseModel):
    fuente: Literal["fijo", "cliente.nombre"] = "fijo"
    valor: Annotated[str, StringConstraints(max_length=1000)] = ""


class VistaPreviaIn(BaseModel):
    connection_id: uuid.UUID
    template_id: uuid.UUID
    variables: dict[str, VariableIn] = Field(default_factory=dict)


class ExcluidoOut(BaseModel):
    motivo: str
    label: str
    total: int


class VistaPreviaOut(BaseModel):
    envio_id: uuid.UUID | None
    candidatos: int
    elegibles: int
    excluidos: list[ExcluidoOut]
    muestra: list[str]
    texto: str
    costo_creditos: str
    tope: int
    limite_diario: int | None
    bloqueo: Literal["sin_destinatarios", "tope", "envio_en_curso"] | None
    expira: datetime | None


class FallaOut(BaseModel):
    cliente: str
    motivo: str


class EnvioOut(BaseModel):
    id: uuid.UUID
    estado: str
    plantilla: str
    total: int
    creado_por: str
    created_at: datetime
    confirmed_at: datetime | None
    finished_at: datetime | None
    conteo: dict[str, int]
    fallas: list[FallaOut]


class ConsentimientoItem(BaseModel):
    estado: str
    origen: str
    evidencia: str
    quien: str
    created_at: datetime


class ConsentimientoOut(BaseModel):
    estado: Literal["otorgado", "revocado", "sin_registro"]
    historial: list[ConsentimientoItem]


class ConsentimientoIn(BaseModel):
    estado: Literal["otorgado", "revocado"]
    como: Literal["en_el_local", "por_telefono", "por_escrito", "otro"]
    evidencia: Annotated[str, StringConstraints(strip_whitespace=True, max_length=400)] = ""


_COMO = {
    "en_el_local": "En el local",
    "por_telefono": "Por teléfono",
    "por_escrito": "Por escrito",
    "otro": "Otro",
}


# ------------------------------------------------------------------ plantilla a un segmento


@router.get("/acciones/plantilla/conexiones", response_model=list[ConexionDeEnvioOut])
async def conexiones_para_envio(
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> list[ConexionDeEnvioOut]:
    """Conexiones de WhatsApp Cloud del negocio con sus plantillas de marketing aprobadas."""
    from core.gateway.event_notices import render_body, template_placeholders
    from core.gateway.meta_connection_state import motivo_pausa_de

    require_owner_or_master(principal)
    tenant_id = principal.tenant_id
    async with get_session() as session:
        conexiones = (
            await session.execute(
                select(ChannelConnection)
                .where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                    ChannelConnection.channel == "whatsapp",
                    ChannelConnection.meta_connection_id.is_not(None),
                )
                .order_by(ChannelConnection.created_at)
                .limit(50)
            )
        ).scalars().all()
    salida = []
    for c in conexiones:
        assert c.meta_connection_id is not None
        plantillas = await envios.plantillas_de_marketing(tenant_id, c.meta_connection_id)
        salida.append(
            ConexionDeEnvioOut(
                id=c.id,
                label=c.label or c.external_id,
                motivo_pausa=await motivo_pausa_de(c.meta_connection_id),
                plantillas=[
                    PlantillaDeEnvio(
                        id=t.id, name=t.name, language=t.language, body=render_body(t, {}),
                        variables=template_placeholders(t.components),
                    )
                    for t in plantillas
                ],
            )
        )
    return salida


@router.post("/acciones/plantilla/vista-previa", response_model=VistaPreviaOut)
async def vista_previa_de_envio(
    body: VistaPreviaIn,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
    q: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    connection_id: Annotated[uuid.UUID | None, Query()] = None,
    filtro: FiltroQuery = None,
) -> VistaPreviaOut:
    """Cuántos clientes del filtro recibirían la plantilla y por qué quedan fuera los demás.
    Todavía no sale nada: si hay a quién enviar, el envío queda en borrador por 10 minutos."""
    require_owner_or_master(principal)
    tenant_id = principal.tenant_id
    enabled, _ = await _providers()
    consulta = await _consulta(
        tenant_id, enabled=enabled, q=q, channel=channel, connection_id=connection_id, filtro=filtro
    )
    try:
        vista = await envios.vista_previa(
            tenant_id=tenant_id,
            admin_id=principal.admin.id,
            admin_label=_quien(principal),
            consulta=consulta,
            filtro={
                "q": q,
                "channel": channel,
                "connection_id": str(connection_id) if connection_id else None,
                "filtro": filtro or [],
            },
            connection_id=body.connection_id,
            template_id=body.template_id,
            variables={k: v.model_dump() for k, v in body.variables.items()},
        )
    except envios.EnvioError as exc:
        raise _error(exc) from exc
    return VistaPreviaOut(
        envio_id=vista.envio_id,
        candidatos=vista.candidatos,
        elegibles=vista.elegibles,
        excluidos=[
            ExcluidoOut(motivo=m, label=envios.MOTIVOS.get(m, m), total=n)
            for m, n in sorted(vista.excluidos.items(), key=lambda kv: -kv[1])
        ],
        muestra=vista.muestra,
        texto=vista.texto,
        costo_creditos=vista.costo_creditos,
        tope=vista.tope,
        limite_diario=vista.limite_diario,
        bloqueo=vista.bloqueo,  # type: ignore[arg-type]
        expira=vista.expira,
    )


@router.post("/acciones/plantilla/{envio_id}/confirmar", response_model=EnvioOut, status_code=202)
async def confirmar_envio(
    envio_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
) -> EnvioOut:
    """Confirma el envío de la vista previa. Repetir con la misma clave no envía dos veces."""
    require_owner_or_master(principal)
    try:
        await envios.confirmar(
            tenant_id=principal.tenant_id,
            admin_id=principal.admin.id,
            envio_id=envio_id,
            idempotency_key=idempotency_key,
        )
        return await _envio(principal.tenant_id, envio_id)
    except envios.EnvioError as exc:
        raise _error(exc) from exc


@router.get("/acciones/plantilla/{envio_id}", response_model=EnvioOut)
async def ver_envio(
    envio_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> EnvioOut:
    """Estado del envío: pendientes, omitidos y, de los encolados, lo que reporta Meta."""
    require_owner_or_master(principal)
    try:
        return await _envio(principal.tenant_id, envio_id)
    except envios.EnvioError as exc:
        raise _error(exc) from exc


async def _envio(tenant_id: uuid.UUID, envio_id: uuid.UUID) -> EnvioOut:
    envio, conteo, fallas = await envios.detalle(tenant_id, envio_id)
    return EnvioOut(
        id=envio.id,
        estado=envio.estado,
        plantilla=envio.template_name,
        total=envio.total,
        creado_por=envio.admin_label or "el equipo",
        created_at=envio.created_at,
        confirmed_at=envio.confirmed_at,
        finished_at=envio.finished_at,
        conteo=conteo,
        fallas=[FallaOut(cliente=c, motivo=m) for c, m in fallas],
    )


# ------------------------------------------------------------------ consentimiento


@router.get("/{lead_id}/consentimiento", response_model=ConsentimientoOut)
async def ver_consentimiento(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads"))],
) -> ConsentimientoOut:
    """Consentimiento del cliente para recibir promociones y su historial (hasta 20)."""
    tenant_id = principal.tenant_id
    from core.api.routers.leads import _cliente

    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
        filas = (
            await session.execute(
                select(CustomerConsent)
                .where(
                    CustomerConsent.tenant_id == tenant_id,
                    CustomerConsent.customer_id == lead_id,
                    CustomerConsent.proposito == envios.PROPOSITO,
                )
                .order_by(CustomerConsent.created_at.desc(), CustomerConsent.id.desc())
                .limit(20)
            )
        ).scalars().all()
    return ConsentimientoOut(
        estado=filas[0].estado if filas else "sin_registro",  # type: ignore[arg-type]
        historial=[
            ConsentimientoItem(
                estado=f.estado, origen=f.origen, evidencia=f.evidencia,
                quien=f.actor_label or ("el cliente" if f.origen in ("palabra_de_baja", "meta_131050") else "el equipo"),
                created_at=f.created_at,
            )
            for f in filas
        ],
    )


@router.post("/{lead_id}/consentimiento", response_model=ConsentimientoOut, status_code=201)
async def registrar_consentimiento(
    lead_id: uuid.UUID,
    body: ConsentimientoIn,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> ConsentimientoOut:
    """Marca que el cliente aceptó (o ya no acepta) recibir promociones. Se guarda cómo se obtuvo
    y quién lo marcó; nunca se edita una fila anterior (ADR-0150)."""
    evidencia = f"{_COMO[body.como]}" + (f": {body.evidencia}" if body.evidencia else "")
    if body.estado == "otorgado" and body.como == "otro" and not body.evidencia:
        raise ApiError.bad_request("Cuenta cómo lo obtuviste.", "evidencia_requerida")
    try:
        await envios.registrar_consentimiento(
            principal.tenant_id,
            lead_id,
            estado=body.estado,
            origen="ficha",
            evidencia=evidencia,
            actor_admin_id=principal.admin.id,
            actor_label=_quien(principal),
        )
    except envios.EnvioError as exc:
        raise _error(exc) from exc
    return await ver_consentimiento(lead_id, principal)
