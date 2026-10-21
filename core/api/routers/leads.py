"""Clientes (leads): `/api/v1/admin/leads`.

Un lead es un `Customer` (ADR-0022); cada pack habilitado aporta columnas y filtros vía
`LeadProvider` (ADR-0056). El core no importa packs: se combinan los providers registrados.

HU-UX-16: ficha del cliente (`GET /{id}`) con lo que el agente aprendió y su origen, corrección
de datos (`PATCH /{id}`, que escribe **solo** por `core/memory/facts.py` con `source="human"`),
pausa del agente por cliente (`POST /{id}/toggle-bot`, sobre `Customer.bot_paused`, que el
kernel ya respeta), filtros por canal, conexión y los de los packs, y exportación CSV.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Select, exists, func, or_, select

from core.admin.lead_registry import LeadProvider, lead_registry
from core.agents.spec import enabled_packs, load_active_spec
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.api.pagination import PageParams, page_params
from core.db.models import ChannelConnection, Conversation, Customer, MemoryFact
from core.db.session import get_session
from core.memory import facts
from core.shared.logger import get_logger
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

router = APIRouter(prefix="/admin/leads", tags=["admin · leads"])

MAX_CSV = 5000
# Lo que una persona puede escribir desde la ficha (los mismos límites que al agente).
_MAX_VALOR = 500
_MAX_CLAVES = 20


class LeadColumn(BaseModel):
    key: str
    label: str


class LeadFilterOption(BaseModel):
    value: str
    label: str


class LeadFilter(BaseModel):
    key: str
    label: str
    options: list[LeadFilterOption]


class LeadRow(BaseModel):
    id: uuid.UUID
    cliente: str
    canal: str
    telefono: str | None = None
    bot_paused: bool = False
    alta: str
    extras: dict[str, Any]


class LeadsResponse(BaseModel):
    columns: list[LeadColumn]
    filters: list[LeadFilter] = Field(default_factory=list)
    items: list[LeadRow]
    total: int
    page: int
    page_size: int
    has_more: bool


class LeadConnection(BaseModel):
    id: uuid.UUID
    label: str
    channel: str


class LeadFact(BaseModel):
    namespace: str
    key: str
    value: Any
    source: str  # tool | agent | human | import
    data_class: str
    valid_from: datetime


class LeadDetail(BaseModel):
    id: uuid.UUID
    cliente: str
    nombre: str | None
    canal: str
    identificador: str
    telefono: str | None
    bot_paused: bool
    alta: datetime | None
    conexiones: list[LeadConnection]
    ultima_conversacion_id: uuid.UUID | None
    datos: list[LeadFact]
    extras: dict[str, Any]
    columns: list[LeadColumn]


class LeadUpdate(BaseModel):
    """Datos del perfil a corregir. `name` también cambia el nombre visible del cliente."""

    datos: dict[str, str | int | float | bool] = Field(default_factory=dict)


class BotState(BaseModel):
    bot_paused: bool


# ------------------------------------------------------------------ helpers


async def _providers() -> tuple[set[str], list[LeadProvider]]:
    try:
        enabled = enabled_packs(await load_active_spec())
    except Exception:
        enabled = set()
    return enabled, lead_registry.providers_for(enabled)


def _filtros_de(providers: list[LeadProvider]) -> list[LeadFilter]:
    return [
        LeadFilter(
            key=f.key,
            label=f.label,
            options=[LeadFilterOption(value=v, label=lbl) for v, lbl in f.options],
        )
        for p in providers
        if p.filter_query is not None
        for f in p.filters
    ]


async def _consulta(
    tenant_id: uuid.UUID,
    *,
    enabled: set[str],
    q: str | None,
    channel: str | None,
    connection_id: uuid.UUID | None,
    filtro: list[str] | None,
) -> Select[Any]:
    query = select(Customer).where(
        Customer.tenant_id == tenant_id,
        Customer.deleted_at.is_(None),
        Customer.channel != TEST_CHANNEL,
    )
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(Customer.name.ilike(like), Customer.external_id.ilike(like), Customer.contact_phone.ilike(like))
        )
    if channel:
        query = query.where(Customer.channel == channel)
    if connection_id is not None:
        query = query.where(
            exists().where(
                Conversation.tenant_id == tenant_id,
                Conversation.customer_id == Customer.id,
                Conversation.connection_id == connection_id,
                Conversation.deleted_at.is_(None),
            )
        )
    for item in filtro or []:
        key, sep, value = item.partition(":")
        encontrado = lead_registry.filter_for(enabled, key) if sep else None
        if encontrado is None:
            raise ApiError.bad_request(f"Filtro desconocido: {key!r}.", code="filtro_invalido")
        provider, definicion = encontrado
        if value not in {v for v, _ in definicion.options}:
            raise ApiError.bad_request(f"Valor no válido para {key!r}.", code="filtro_invalido")
        assert provider.filter_query is not None
        sub = await provider.filter_query(key, value)
        if sub is None:
            raise ApiError.bad_request(f"Filtro desconocido: {key!r}.", code="filtro_invalido")
        query = query.where(Customer.id.in_(sub))
    return query


async def _valores_de_packs(
    providers: list[LeadProvider], customer_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    valores: dict[uuid.UUID, dict[str, Any]] = {}
    for provider in providers:
        if provider.resolver is None:
            continue
        try:
            result = await provider.resolver(customer_ids)
        except Exception:
            log.warning("lead_provider_failed", plugin_id=provider.plugin_id, exc_info=True)
            continue
        for cid, values in (result or {}).items():
            valores.setdefault(cid, {}).update(values)
    return valores


def _columnas(providers: list[LeadProvider]) -> list[LeadColumn]:
    return [LeadColumn(key=col.key, label=col.label) for p in providers for col in p.columns]


async def _cliente(session: Any, tenant_id: uuid.UUID, lead_id: uuid.UUID) -> Customer:
    cliente = (
        await session.execute(
            select(Customer).where(
                Customer.id == lead_id,
                Customer.tenant_id == tenant_id,
                Customer.deleted_at.is_(None),
                Customer.channel != TEST_CHANNEL,
            )
        )
    ).scalar_one_or_none()
    if cliente is None:
        raise ApiError.not_found("Cliente no encontrado.")
    return cliente


FiltroQuery = Annotated[
    list[str] | None,
    Query(description="Filtro de un pack como `clave:valor` (ver `filters` de la respuesta)."),
]


# ------------------------------------------------------------------ rutas


@router.get("", response_model=LeadsResponse)
async def list_leads(
    principal: Annotated[Principal, Depends(require("leads"))],
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(description="Busca por nombre, identificador o teléfono.")] = None,
    channel: Annotated[str | None, Query(description="Solo los clientes de este canal.")] = None,
    connection_id: Annotated[
        uuid.UUID | None, Query(description="Solo los clientes que escribieron por esta conexión.")
    ] = None,
    filtro: FiltroQuery = None,
) -> LeadsResponse:
    tenant_id = principal.tenant_id
    enabled, providers = await _providers()
    query = await _consulta(
        tenant_id, enabled=enabled, q=q, channel=channel, connection_id=connection_id, filtro=filtro
    )

    async with get_session() as session:
        total = (
            await session.execute(select(func.count()).select_from(query.subquery()))
        ).scalar() or 0
        customers = (
            (
                await session.execute(
                    query.order_by(Customer.created_at.desc(), Customer.id)
                    .offset(params.offset)
                    .limit(params.page_size)
                )
            )
            .scalars()
            .all()
        )

    pack_values = await _valores_de_packs(providers, [c.id for c in customers])
    items = [
        LeadRow(
            id=c.id,
            cliente=c.name or c.external_id,
            canal=c.channel,
            telefono=c.contact_phone,
            bot_paused=bool(c.bot_paused),
            alta=c.created_at.strftime("%Y-%m-%d") if c.created_at else "—",
            extras=pack_values.get(c.id, {}),
        )
        for c in customers
    ]
    return LeadsResponse(
        columns=_columnas(providers),
        filters=_filtros_de(providers),
        items=items,
        total=int(total),
        page=params.page,
        page_size=params.page_size,
        has_more=params.page * params.page_size < total,
    )


@router.get("/export.csv", response_class=Response)
async def export_leads_csv(
    principal: Annotated[Principal, Depends(require("leads"))],
    q: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
    connection_id: Annotated[uuid.UUID | None, Query()] = None,
    filtro: FiltroQuery = None,
) -> Response:
    """La lista filtrada en CSV (hasta 5000 filas, las más recientes)."""
    tenant_id = principal.tenant_id
    enabled, providers = await _providers()
    query = await _consulta(
        tenant_id, enabled=enabled, q=q, channel=channel, connection_id=connection_id, filtro=filtro
    )
    async with get_session() as session:
        customers = (
            (await session.execute(query.order_by(Customer.created_at.desc(), Customer.id).limit(MAX_CSV)))
            .scalars()
            .all()
        )
    pack_values = await _valores_de_packs(providers, [c.id for c in customers])
    columnas = _columnas(providers)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Cliente", "Canal", "Teléfono", "Agente", "Alta", *[c.label for c in columnas]])
    for c in customers:
        extras = pack_values.get(c.id, {})
        writer.writerow(
            [
                _celda(c.name or c.external_id),
                c.channel,
                _celda(c.contact_phone or ""),
                "pausado" if c.bot_paused else "activo",
                c.created_at.strftime("%Y-%m-%d") if c.created_at else "",
                *[_celda(extras.get(col.key)) for col in columnas],
            ]
        )
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="clientes.csv"'},
    )


def _celda(valor: Any) -> str:
    """Texto de una celda, neutralizando fórmulas (inyección CSV en hojas de cálculo)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


@router.get("/{lead_id}", response_model=LeadDetail)
async def get_lead(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads"))],
) -> LeadDetail:
    """Ficha del cliente: datos del canal, lo que se sabe de él (con origen y fecha) y los packs."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        cliente = await _cliente(session, tenant_id, lead_id)
        conexiones = (
            await session.execute(
                select(ChannelConnection.id, ChannelConnection.label, ChannelConnection.channel)
                .where(
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.id.in_(
                        select(Conversation.connection_id).where(
                            Conversation.tenant_id == tenant_id,
                            Conversation.customer_id == lead_id,
                            Conversation.deleted_at.is_(None),
                        )
                    ),
                )
                .order_by(ChannelConnection.label)
            )
        ).all()
        ultima = (
            await session.execute(
                select(Conversation.id)
                .where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.customer_id == lead_id,
                    Conversation.deleted_at.is_(None),
                )
                .order_by(Conversation.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        hechos = (
            (
                await session.execute(
                    select(MemoryFact)
                    .where(
                        MemoryFact.tenant_id == tenant_id,
                        MemoryFact.customer_id == lead_id,
                        MemoryFact.superseded_at.is_(None),
                        MemoryFact.deleted_at.is_(None),
                    )
                    .order_by(MemoryFact.namespace, MemoryFact.key)
                )
            )
            .scalars()
            .all()
        )

    _enabled, providers = await _providers()
    extras = (await _valores_de_packs(providers, [cliente.id])).get(cliente.id, {})
    return LeadDetail(
        id=cliente.id,
        cliente=cliente.name or cliente.external_id,
        nombre=cliente.name,
        canal=cliente.channel,
        identificador=cliente.external_id,
        telefono=cliente.contact_phone,
        bot_paused=bool(cliente.bot_paused),
        alta=cliente.created_at,
        conexiones=[LeadConnection(id=cid, label=label or channel, channel=channel) for cid, label, channel in conexiones],
        ultima_conversacion_id=ultima,
        datos=[
            LeadFact(
                namespace=h.namespace,
                key=h.key,
                value=h.value,
                source=h.source,
                data_class=h.data_class,
                valid_from=h.valid_from,
            )
            for h in hechos
        ],
        extras=extras,
        columns=_columnas(providers),
    )


@router.patch("/{lead_id}", response_model=LeadDetail)
async def update_lead(
    lead_id: uuid.UUID,
    body: LeadUpdate,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> LeadDetail:
    """Corrige datos del perfil. Quedan como dato de una persona (`source="human"`) y el agente
    los usa desde la siguiente conversación. Solo escribe `core/memory/facts.py`."""
    datos = {k.strip(): v for k, v in body.datos.items()}
    if not datos:
        raise ApiError.bad_request("No hay datos para guardar.")
    if len(datos) > _MAX_CLAVES:
        raise ApiError.bad_request(f"Máximo {_MAX_CLAVES} datos por vez.")
    errores: dict[str, str] = {}
    for clave, valor in datos.items():
        if not clave or len(clave) > 80:
            errores[clave or "(vacía)"] = "La clave debe tener entre 1 y 80 caracteres."
        elif clave in facts.CLAVES_DEL_SISTEMA:
            errores[clave] = "Este dato lo administra el sistema."
        elif facts.clasificar(facts.PERFIL, clave) == "sensitive":
            # Salud, pago y credenciales no se guardan como memoria del cliente (facts.py).
            errores[clave] = "Este tipo de dato no se guarda en la ficha del cliente."
        elif isinstance(valor, str) and len(valor) > _MAX_VALOR:
            errores[clave] = f"Máximo {_MAX_VALOR} caracteres."
    if "name" in datos and not str(datos["name"]).strip():
        errores["name"] = "El nombre no puede quedar vacío."
    if errores:
        raise ApiError.bad_request("Revisa los datos.", code="datos_invalidos", fields=errores)

    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
        await facts.escribir_hechos(
            session,
            tenant_id=tenant_id,
            customer_id=lead_id,
            namespace=facts.PERFIL,
            valores=datos,
            source="human",
        )
    log.info("lead_actualizado", customer_id=str(lead_id), claves=sorted(datos), admin_id=str(principal.admin.id))
    return await get_lead(lead_id, principal)


@router.post("/{lead_id}/toggle-bot", response_model=BotState)
async def toggle_lead_bot(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> BotState:
    """Pausa o reanuda el agente para este cliente en todas sus conversaciones."""
    async with get_session() as session:
        cliente = await _cliente(session, principal.tenant_id, lead_id)
        cliente.bot_paused = not cliente.bot_paused
        paused = cliente.bot_paused
    log.info("lead_bot_toggled", customer_id=str(lead_id), bot_paused=paused, admin_id=str(principal.admin.id))
    return BotState(bot_paused=paused)
