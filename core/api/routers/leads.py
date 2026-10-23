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
from core.db.models import (
    AgentRecord,
    ChannelConnection,
    Conversation,
    Customer,
    CustomerNote,
    MemoryFact,
)
from core.db.session import get_session
from core.geocoding import GeocodingError
from core.geocoding import consumir_cuota as consumir_cuota_geocoding
from core.geocoding import disponible as geocoding_disponible
from core.geocoding import geocodificar as geocodificar_direccion
from core.memory import facts
from core.memory import perfil as perfil_cliente
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


class LeadPerfilOut(BaseModel):
    """Datos base del cliente. Las claves son las canónicas de `core.memory.perfil`."""

    name: str | None = None
    apellido: str | None = None
    alias: str | None = None
    avatar_url: str | None = None
    nombre_visible: str
    phone: str | None = None
    phone_e164: str | None = None
    phone_indicativo: str | None = None
    phone_local: str | None = None
    pais_telefono: str | None = None
    email: str | None = None
    tipo_documento: str | None = None
    id_document: str | None = None
    address: str | None = None
    municipio: str | None = None
    departamento: str | None = None
    pais: str | None = None
    latitud: float | None = None
    longitud: float | None = None
    direccion_completa: str | None = None
    direccion_normalizada: str | None = None
    geocodificado_en: str | None = None
    geocodificacion_precision: str | None = None
    geocodificacion_proveedor: str | None = None
    mapa_url: str | None = None
    clasificacion: str | None = None
    clasificacion_label: str | None = None


class LeadAgentOut(BaseModel):
    """Un agente del negocio y las columnas que aporta a la lista de clientes."""

    key: str
    name: str
    icon: str | None = None
    pack_id: str | None = None
    columns: list[LeadColumn]


class LeadRow(BaseModel):
    id: uuid.UUID
    cliente: str
    canal: str
    telefono: str | None = None
    bot_paused: bool = False
    alta: str
    extras: dict[str, Any]
    perfil: LeadPerfilOut
    agentes: list[str]


class LeadsResponse(BaseModel):
    columns: list[LeadColumn]
    filters: list[LeadFilter] = Field(default_factory=list)
    agents: list[LeadAgentOut]
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


class LeadCampoPerfil(BaseModel):
    """Metadatos de un campo editable del perfil (los comparte la ficha y la API)."""

    key: str
    label: str
    tipo: Literal["texto", "email", "telefono", "opciones", "numero", "url"]
    max_length: int
    options: list[LeadFilterOption]
    group: str
    help: str | None = None
    required: bool = False


class LeadColumnValue(BaseModel):
    key: str
    label: str
    value: Any = None


class LeadAgentData(BaseModel):
    """La data de un agente para la ficha: columnas con su valor y campos editables."""

    key: str
    name: str
    icon: str | None = None
    pack_id: str | None = None
    columns: list[LeadColumnValue]
    editables: list[LeadEditable]


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
    perfil: LeadPerfilOut
    agentes: list[LeadAgentData]
    campos_perfil: list[LeadCampoPerfil]
    editables: list[LeadEditable]


class LeadUpdate(BaseModel):
    """Datos del perfil a corregir. `name` también cambia el nombre visible del cliente."""

    datos: dict[str, str | int | float | bool] = Field(default_factory=dict)


class LeadGeocodificacion(BaseModel):
    """Resultado de geocodificar la dirección y el perfil actualizado."""

    latitud: float
    longitud: float
    direccion_normalizada: str | None = None
    precision: str | None = None
    proveedor: str
    perfil: LeadPerfilOut


class LeadPerfilUpdate(BaseModel):
    """Campos canónicos del perfil que se cambian desde la pestaña General.

    `otros` conserva la posibilidad de guardar un dato libre (una preferencia, por ejemplo)
    sin saltarse las reglas de `facts.py`: no admite claves del sistema ni datos sensibles.
    """

    valores: dict[str, Any] = Field(default_factory=dict)
    otros: dict[str, str] = Field(default_factory=dict)


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


def _perfil_de(cliente: Customer) -> LeadPerfilOut:
    return LeadPerfilOut(
        **perfil_cliente.leer_perfil(
            cliente.attributes or {},
            telefono=cliente.contact_phone,
            nombre_cliente=cliente.name,
            external_id=cliente.external_id,
        )
    )


async def _registros_de_agentes(tenant_id: uuid.UUID) -> list[AgentRecord]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(AgentRecord)
                    .where(
                        AgentRecord.tenant_id == tenant_id,
                        AgentRecord.deleted_at.is_(None),
                        AgentRecord.kind != "core",
                    )
                    .order_by(AgentRecord.created_at, AgentRecord.id)
                )
            )
            .scalars()
            .all()
        )


def _clave_de_agente(provider: LeadProvider) -> str:
    return provider.agent_key or provider.plugin_id


def _meta_de_agentes(providers: list[LeadProvider], registros: list[AgentRecord]) -> list[LeadAgentOut]:
    """Agentes publicados + los que aportan data, agrupados por clave (General primero)."""
    por_key: dict[str, LeadAgentOut] = {}
    general = next((r for r in registros if r.key == "general"), None)
    por_key["general"] = LeadAgentOut(
        key="general",
        name=general.name if general is not None else "General",
        icon="bot",
        columns=[],
    )
    for provider in providers:
        key = _clave_de_agente(provider)
        agente = next((r for r in registros if r.key == key), None)
        item = por_key.get(key)
        if item is None:
            item = LeadAgentOut(
                key=key,
                name=provider.agent_name or (agente.name if agente is not None else key.replace("_", " ").title()),
                icon=provider.icon or "bot",
                pack_id=provider.plugin_id,
                columns=[],
            )
            por_key[key] = item
        item.columns.extend(LeadColumn(key=c.key, label=c.label) for c in provider.columns)
    for registro in registros:
        if registro.key == "coordinador" or registro.key in por_key:
            continue
        por_key[registro.key] = LeadAgentOut(key=registro.key, name=registro.name, icon="bot", columns=[])
    return list(por_key.values())


def _agentes_de_la_fila(
    providers: list[LeadProvider], extras: dict[str, Any], meta: list[LeadAgentOut]
) -> list[str]:
    """Claves de los agentes que tienen algún dato de este cliente (General siempre)."""
    presentes = {"general"}
    for provider in providers:
        if any(extras.get(c.key) not in (None, "", [], {}) for c in provider.columns):
            presentes.add(_clave_de_agente(provider))
    return [a.key for a in meta if a.key in presentes]


def _agentes_data(
    providers: list[LeadProvider],
    extras: dict[str, Any],
    editables: list[LeadEditable],
    meta: list[LeadAgentOut],
) -> list[LeadAgentData]:
    """Data de cada agente para las pestañas de la ficha. General va en `perfil`."""
    salida: list[LeadAgentData] = []
    for agente in meta:
        if agente.key == "general":
            continue
        propios = [p for p in providers if _clave_de_agente(p) == agente.key]
        columnas = [
            LeadColumnValue(key=c.key, label=c.label, value=extras.get(c.key))
            for p in propios
            for c in p.columns
        ]
        plugin_ids = {p.plugin_id for p in propios}
        campos = [e for e in editables if e.plugin_id in plugin_ids]
        salida.append(
            LeadAgentData(
                key=agente.key,
                name=agente.name,
                icon=agente.icon,
                pack_id=agente.pack_id,
                columns=columnas,
                editables=campos,
            )
        )
    return salida


def _etiqueta_opcion(clave: str, valor: Any) -> str:
    if valor in (None, ""):
        return ""
    return dict(perfil_cliente.opciones(clave)).get(str(valor), str(valor))


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
    customer_id: uuid.UUID | None = None,
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
    if customer_id is not None:
        query = query.where(Customer.id == customer_id)
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
    meta = _meta_de_agentes(providers, await _registros_de_agentes(tenant_id))
    items = []
    for cliente in customers:
        extras = pack_values.get(cliente.id, {})
        perfil_cliente_ = _perfil_de(cliente)
        items.append(
            LeadRow(
                id=cliente.id,
                cliente=perfil_cliente_.nombre_visible,
                canal=cliente.channel,
                telefono=perfil_cliente_.phone,
                bot_paused=bool(cliente.bot_paused),
                alta=cliente.created_at.strftime("%Y-%m-%d") if cliente.created_at else "—",
                extras=extras,
                perfil=perfil_cliente_,
                agentes=_agentes_de_la_fila(providers, extras, meta),
            )
        )
    return LeadsResponse(
        columns=_columnas(providers),
        filters=_filtros_de(providers),
        agents=meta,
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
    meta = _meta_de_agentes(providers, await _registros_de_agentes(tenant_id))
    nombre_agente = {a.key: a.name for a in meta}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "Cliente",
            "Nombre",
            "Apellido",
            "Alias",
            "Canal",
            "Clasificación",
            "Teléfono",
            "Indicativo",
            "Correo",
            "Tipo documento",
            "Documento",
            "Dirección",
            "Municipio",
            "Departamento",
            "País",
            "Latitud",
            "Longitud",
            "Agentes",
            "Estado del agente",
            "Alta",
            *[c.label for c in columnas],
        ]
    )
    for c in customers:
        extras = pack_values.get(c.id, {})
        p = _perfil_de(c)
        agentes = _agentes_de_la_fila(providers, extras, meta)
        writer.writerow(
            [
                _celda(p.nombre_visible),
                _celda(p.name or ""),
                _celda(p.apellido or ""),
                _celda(p.alias or ""),
                c.channel,
                _celda(p.clasificacion_label or p.clasificacion or ""),
                _celda(p.phone_e164 or p.phone or ""),
                _celda(p.phone_indicativo or ""),
                _celda(p.email or ""),
                _celda(_etiqueta_opcion("tipo_documento", p.tipo_documento)),
                _celda(p.id_document or ""),
                _celda(p.address or ""),
                _celda(p.municipio or ""),
                _celda(p.departamento or ""),
                _celda(p.pais or ""),
                _celda(p.latitud if p.latitud is not None else ""),
                _celda(p.longitud if p.longitud is not None else ""),
                _celda(", ".join(nombre_agente.get(k, k) for k in agentes)),
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
    meta = _meta_de_agentes(providers, await _registros_de_agentes(tenant_id))
    editables = await _editables(providers, cliente.id)
    perfil_out = _perfil_de(cliente)
    return LeadDetail(
        id=cliente.id,
        cliente=perfil_out.nombre_visible,
        nombre=perfil_out.name,
        canal=cliente.channel,
        identificador=cliente.external_id,
        telefono=perfil_out.phone,
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
        perfil=perfil_out,
        agentes=_agentes_data(providers, extras, editables, meta),
        campos_perfil=[LeadCampoPerfil(**campo) for campo in perfil_cliente.campos_editables()],
        editables=editables,
    )


@router.post("/{lead_id}/geocodificar", response_model=LeadGeocodificacion)
async def geocodificar_lead(
    lead_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> LeadGeocodificacion:
    """Convierte la dirección del cliente en coordenadas y las guarda en el perfil.

    El proveedor recibe solo la dirección compuesta (dirección, municipio, departamento y
    país). La plataforma paga la llamada y hay topes global y por negocio (ADR-0154).
    """
    if not geocoding_disponible():
        raise ApiError(503, "geocoding_no_configurado", "La geocodificación no está configurada.")
    tenant_id = principal.tenant_id
    async with get_session() as session:
        cliente = await _cliente(session, tenant_id, lead_id)
        perfil = perfil_cliente.leer_perfil(
            cliente.attributes or {},
            telefono=cliente.contact_phone,
            nombre_cliente=cliente.name,
            external_id=cliente.external_id,
        )
    direccion = perfil.get("direccion_completa")
    if not direccion:
        raise ApiError.bad_request(
            "Escribe la dirección, el municipio o el departamento antes de geocodificar.",
            code="geocoding_direccion_vacia",
        )

    await consumir_cuota_geocoding(tenant_id)
    try:
        resultado = await geocodificar_direccion(direccion)
    except GeocodingError as exc:
        raise ApiError(exc.status, exc.code, exc.message) from exc

    valores = {
        "latitud": resultado.latitud,
        "longitud": resultado.longitud,
        "direccion_normalizada": resultado.direccion_normalizada,
        "geocodificado_en": datetime.now().astimezone().isoformat(),
        "geocodificacion_precision": resultado.precision,
        "geocodificacion_proveedor": resultado.proveedor,
    }
    try:
        async with get_session() as session:
            await _cliente(session, tenant_id, lead_id)
            await facts.escribir_hechos(
                session,
                tenant_id=tenant_id,
                customer_id=lead_id,
                namespace=facts.PERFIL,
                valores={k: v for k, v in valores.items() if v is not None},
                source="human",
            )
    except facts.HechoInvalido as exc:
        raise ApiError(400, "geocoding_datos_invalidos", "No se pudo guardar la geocodificación.") from exc

    async with get_session() as session:
        cliente = await _cliente(session, tenant_id, lead_id)
        perfil_actualizado = _perfil_de(cliente)
    log.info(
        "lead_geocodificado",
        customer_id=str(lead_id),
        provider=resultado.proveedor,
        precision=resultado.precision,
        admin_id=str(principal.admin.id),
    )
    return LeadGeocodificacion(
        latitud=resultado.latitud,
        longitud=resultado.longitud,
        direccion_normalizada=resultado.direccion_normalizada,
        precision=resultado.precision,
        proveedor=resultado.proveedor,
        perfil=perfil_actualizado,
    )


@router.patch("/{lead_id}/perfil", response_model=LeadDetail)
async def update_lead_perfil(
    lead_id: uuid.UUID,
    body: LeadPerfilUpdate,
    principal: Annotated[Principal, Depends(require("leads", write=True))],
) -> LeadDetail:
    """Corrige los datos base del cliente desde la pestaña General.

    Solo acepta las claves canónicas de `core.memory.perfil`; escribe por
    `core/memory/facts.py` con `source="human"` para que el agente lo use desde la
    siguiente conversación y quede la procedencia en la ficha.
    """
    if not body.valores and not body.otros:
        raise ApiError.bad_request("No hay datos para guardar.")
    valores, errores = perfil_cliente.validar_perfil(body.valores)
    otros = {k.strip(): str(v).strip() for k, v in body.otros.items()}
    if len(otros) > _MAX_CLAVES:
        raise ApiError.bad_request(f"Máximo {_MAX_CLAVES} datos por vez.")
    for clave, valor in otros.items():
        if not clave or len(clave) > 80:
            errores[clave or "(vacía)"] = "La clave debe tener entre 1 y 80 caracteres."
        elif perfil_cliente.es_campo_canonico(clave):
            errores[clave] = "Este dato se edita en los campos del perfil."
        elif clave in facts.CLAVES_DEL_SISTEMA:
            errores[clave] = "Este dato lo administra el sistema."
        elif facts.clasificar(facts.PERFIL, clave) == "sensitive":
            errores[clave] = "Este tipo de dato no se guarda en la ficha del cliente."
        elif len(valor) > _MAX_VALOR:
            errores[clave] = f"Máximo {_MAX_VALOR} caracteres."
    if errores:
        raise ApiError.bad_request("Revisa los datos.", code="datos_invalidos", fields=errores)
    valores = {**valores, **{k: v for k, v in otros.items() if v}}
    if not valores:
        raise ApiError.bad_request("No hay datos para guardar.")

    tenant_id = principal.tenant_id
    try:
        async with get_session() as session:
            await _cliente(session, tenant_id, lead_id)
            await facts.escribir_hechos(
                session,
                tenant_id=tenant_id,
                customer_id=lead_id,
                namespace=facts.PERFIL,
                valores=valores,
                source="human",
            )
    except facts.HechoInvalido as exc:
        raise ApiError.bad_request("No se pudo guardar el perfil.", code="datos_invalidos") from exc
    log.info(
        "lead_perfil_actualizado",
        customer_id=str(lead_id),
        claves=sorted(valores),
        admin_id=str(principal.admin.id),
    )
    return await get_lead(lead_id, principal)


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
    try:
        await provider.edicion.escribir(lead_id, valores)
    except ValueError as exc:
        raise ApiError.bad_request(str(exc), code="datos_invalidos") from exc
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
