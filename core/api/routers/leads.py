"""Clientes (leads): `/api/v1/admin/leads`.

Un lead es un `Customer` (ADR-0022); cada pack habilitado aporta columnas y filtros vía
`LeadProvider` (ADR-0056). El core no importa packs: se combinan los providers registrados.

HU-UX-16: ficha del cliente (`GET /{id}`) con lo que el agente aprendió y su origen, corrección
de datos (`PATCH /{id}`, que escribe **solo** por `core/memory/facts.py` con `source="human"`),
pausa del agente por cliente (`POST /{id}/toggle-bot`, sobre `Customer.bot_paused`, que el
kernel ya respeta), filtros por canal, conexión y los de los packs, y exportación CSV.

ADR-0149: los packs exponen campos editables (`LeadEdicion`, p. ej. el estado y el interés del
lead de venta). La ficha los trae en `editables` y se cambian con `PATCH /{id}/pack/{plugin_id}`,
aparte del PATCH del perfil para que este siga escribiendo solo por `facts.py`.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import Select, exists, func, or_, select

from core.admin.lead_registry import LeadProvider, lead_registry
from core.admin.roles import actua_como_master
from core.agents.spec import enabled_packs, load_active_spec
from core.api.deps import Principal, es_owner_o_master, require
from core.api.errors import ApiError
from core.api.pagination import PageParams, page_params
from core.db.models import ChannelConnection, Conversation, Customer, CustomerNote, MemoryFact
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


class LeadEditable(BaseModel):
    """Un campo de un pack que se edita desde la ficha (ADR-0149)."""

    plugin_id: str
    key: str
    label: str
    tipo: Literal["opciones", "texto"]
    options: list[LeadFilterOption] = Field(default_factory=list)
    max_length: int
    value: str | None = None


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
    editables: list[LeadEditable] = Field(default_factory=list)


class LeadUpdate(BaseModel):
    """Datos del perfil a corregir. `name` también cambia el nombre visible del cliente."""

    datos: dict[str, str | int | float | bool] = Field(default_factory=dict)


class LeadPackUpdate(BaseModel):
    """Valores de los campos editables de un pack (las claves de `editables` de ese pack)."""

    valores: dict[str, str] = Field(default_factory=dict)


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


async def _editables(providers: list[LeadProvider], customer_id: uuid.UUID) -> list[LeadEditable]:
    """Campos editables de los packs con su valor actual. Un pack que falla al leer se omite."""
    salida: list[LeadEditable] = []
    for provider in providers:
        if provider.edicion is None:
            continue
        try:
            valores = await provider.edicion.leer(customer_id) or {}
        except Exception:
            log.warning("lead_provider_edicion_failed", plugin_id=provider.plugin_id, exc_info=True)
            continue
        for campo in provider.edicion.campos:
            valor = valores.get(campo.key)
            salida.append(
                LeadEditable(
                    plugin_id=provider.plugin_id,
                    key=campo.key,
                    label=campo.label,
                    tipo=campo.tipo,
                    options=[LeadFilterOption(value=v, label=lbl) for v, lbl in campo.options],
                    max_length=campo.max_length,
                    value=None if valor is None else str(valor),
                )
            )
    return salida


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
        editables=await _editables(providers, cliente.id),
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


@router.patch("/{lead_id}/pack/{plugin_id}", response_model=LeadDetail)
async def update_lead_pack(
    lead_id: uuid.UUID,
    plugin_id: str,
    body: LeadPackUpdate,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> LeadDetail:
    """Cambia campos que un pack deja editar desde la ficha (p. ej. el estado del lead de venta).
    El core valida claves y valores; el pack escribe en sus tablas (ADR-0149)."""
    _enabled, providers = await _providers()
    provider = next((p for p in providers if p.plugin_id == plugin_id and p.edicion is not None), None)
    if provider is None or provider.edicion is None:
        # Sin distinguir «no existe» de «no habilitado»: no se revela qué packs hay.
        raise ApiError.not_found("No hay datos editables de ese tipo.")
    campos = {c.key: c for c in provider.edicion.campos}
    valores = {k.strip(): v.strip() for k, v in body.valores.items()}
    if not valores:
        raise ApiError.bad_request("No hay datos para guardar.")
    errores: dict[str, str] = {}
    for clave, valor in valores.items():
        campo = campos.get(clave)
        if campo is None:
            errores[clave or "(vacía)"] = "Este dato no se puede editar aquí."
        elif campo.tipo == "opciones" and valor not in {v for v, _ in campo.options}:
            errores[clave] = "Elige una de las opciones."
        elif len(valor) > campo.max_length:
            errores[clave] = f"Máximo {campo.max_length} caracteres."
    if errores:
        raise ApiError.bad_request("Revisa los datos.", code="datos_invalidos", fields=errores)

    async with get_session() as session:
        await _cliente(session, principal.tenant_id, lead_id)
    # Fuera de la sesión de la validación: el pack abre la suya (como la línea de tiempo).
    await provider.edicion.escribir(lead_id, valores)
    log.info(
        "lead_pack_editado",
        customer_id=str(lead_id),
        plugin_id=plugin_id,
        claves=sorted(valores),
        admin_id=str(principal.admin.id),
    )
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


# ------------------------------------------------------------------ notas y línea de tiempo (HU-CRM-04)


class LeadNote(BaseModel):
    id: uuid.UUID
    text: str
    author: str
    created_at: datetime
    puede_borrar: bool


class LeadNoteCreate(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class TimelineItem(BaseModel):
    at: datetime
    kind: str
    title: str
    detail: str | None = None
    link_kind: str | None = None
    link_id: str | None = None


class TimelineResponse(BaseModel):
    items: list[TimelineItem]
    next_cursor: str | None
    partial: bool


def _nota(n: CustomerNote, principal: Principal) -> LeadNote:
    return LeadNote(
        id=n.id,
        text=n.text,
        author=n.author_label or "el equipo",
        created_at=n.created_at,
        puede_borrar=n.author_admin_id == principal.admin.id or es_owner_o_master(principal),
    )


@router.get("/{lead_id}/notes", response_model=list[LeadNote])
async def list_lead_notes(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads"))],
) -> list[LeadNote]:
    """Notas del equipo sobre el cliente, de la más nueva a la más vieja (hasta 100)."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
        notas = (
            await session.execute(
                select(CustomerNote)
                .where(
                    CustomerNote.tenant_id == tenant_id,
                    CustomerNote.customer_id == lead_id,
                    CustomerNote.deleted_at.is_(None),
                )
                .order_by(CustomerNote.created_at.desc(), CustomerNote.id.desc())
                .limit(100)
            )
        ).scalars().all()
    return [_nota(n, principal) for n in notas]


@router.post("/{lead_id}/notes", response_model=LeadNote, status_code=201)
async def create_lead_note(
    lead_id: uuid.UUID,
    body: LeadNoteCreate,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> LeadNote:
    """Nota del equipo sobre el cliente. La ve todo el equipo y el agente la tiene en cuenta como
    dato (nunca como instrucción) en las siguientes conversaciones (ADR-0147)."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
        nota = CustomerNote(
            tenant_id=tenant_id,
            customer_id=lead_id,
            text=body.text,
            author_admin_id=principal.admin.id,
            # Un admin de plataforma no deja su correo personal a la vista del negocio.
            author_label="Soporte de la plataforma" if actua_como_master(principal.admin) else (principal.admin.email or ""),
        )
        session.add(nota)
        await session.flush()
        salida = _nota(nota, principal)
    log.info("lead_nota_creada", customer_id=str(lead_id), admin_id=str(principal.admin.id))
    return salida


@router.delete("/{lead_id}/notes/{note_id}", status_code=204)
async def delete_lead_note(
    lead_id: uuid.UUID,
    note_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> Response:
    """Borra (lógico) una nota. Solo su autor, el propietario del negocio o un admin de plataforma."""
    tenant_id = principal.tenant_id
    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
        nota = (
            await session.execute(
                select(CustomerNote).where(
                    CustomerNote.id == note_id,
                    CustomerNote.tenant_id == tenant_id,
                    CustomerNote.customer_id == lead_id,
                    CustomerNote.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if nota is None:
            raise ApiError.not_found("Nota no encontrada.")
        if nota.author_admin_id != principal.admin.id and not es_owner_o_master(principal):
            raise ApiError.forbidden("Solo quien escribió la nota o el propietario pueden borrarla.")
        nota.deleted_at = func.now()
    log.info("lead_nota_borrada", customer_id=str(lead_id), admin_id=str(principal.admin.id))
    return Response(status_code=204)


@router.get("/{lead_id}/timeline", response_model=TimelineResponse)
async def lead_timeline(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads"))],
    cursor: Annotated[str | None, Query(description="`next_cursor` de la página anterior.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> TimelineResponse:
    """Historia del cliente, del más reciente al más antiguo: conversaciones, escalaciones, datos
    aprendidos, notas y lo que aporten los packs (pedidos, citas…). `partial` si un pack falló."""
    from core.admin import linea_de_tiempo

    tenant_id = principal.tenant_id
    try:
        posicion = linea_de_tiempo.decodificar_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise ApiError.bad_request("Cursor inválido.", code="cursor_invalido") from exc
    async with get_session() as session:
        await _cliente(session, tenant_id, lead_id)
    _enabled, providers = await _providers()
    eventos, siguiente, parcial = await linea_de_tiempo.armar(tenant_id, lead_id, providers, posicion, limit)
    return TimelineResponse(
        items=[
            TimelineItem(
                at=e.at, kind=e.kind, title=e.title, detail=e.detail, link_kind=e.link_kind, link_id=e.link_id
            )
            for e in eventos
        ],
        next_cursor=siguiente,
        partial=parcial,
    )
