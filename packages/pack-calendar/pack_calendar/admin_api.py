"""API admin del pack de Calendario (ADR-0079).

Montada en `/api/v1/pack/calendar`. Estado de la conexión, tipos de cita y citas
(listar y cancelar). La configuración del pack ya se edita con el formulario
genérico de JSON Schema en `/habilidades/pack_calendar`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.db.session import get_owned, get_session

# Registra los providers de búsqueda global del pack (ADR-0096).
from . import search as _search  # noqa: F401
from . import sync as _sync  # noqa: F401
from .models import COLORES_TIPO, Appointment, AppointmentType

PLUGIN_ID = "pack_calendar"
router = APIRouter(prefix="/api/v1/pack/calendar", tags=["pack · calendario"])


async def _config_y_agendas() -> tuple[Any, list[Any]]:
    """Config del pack (o `None` si no está configurado) y las agendas del negocio."""
    from core.plugin_manager import plugin_manager
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    base = config if isinstance(config, CalendarConfig) else CalendarConfig()
    return config, await agendas.listar(base)


async def _agenda_de_alta(calendar_id: uuid.UUID | None, config: Any, lista: list[Any]) -> Any:
    """Agenda a la que se asigna una cita o bloqueo nuevo (HU-CAL-08).

    Con `calendar_id` debe ser una agenda activa del negocio; sin él, la principal, salvo que
    el asistente ofrezca varias agendas (2+ disponibles): entonces el panel debe elegir.
    """
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig

    base = config if isinstance(config, CalendarConfig) else CalendarConfig()
    if calendar_id is not None:
        agenda = next((a for a in lista if a.id == calendar_id and a.active), None)
        if agenda is None:
            raise ApiError.bad_request("La agenda no es de este negocio.", "agenda_invalida")
        return agenda
    if agendas.multi_activo(base, lista):
        raise ApiError.bad_request("Elige la agenda de la cita.", "agenda_requerida")
    return agendas.por_id(None, lista)


# ------------------------------------------------------------------ estado
class CalendarSummary(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    is_default: bool
    active: bool
    source: str  # "google" | "interno"
    connected: bool  # fuente lista (Google con la cuenta conectada, o interna)
    account_email: str = ""


class CalendarStatus(BaseModel):
    configured: bool
    # `connected`, `account_email` y `connection_label` describen la agenda principal (o la
    # primera con Google); son los campos de antes de HU-CAL-08 y quedan por compatibilidad.
    connected: bool
    account_email: str
    connection_label: str
    timezone: str
    default_duration_minutes: int
    calendars: list[CalendarSummary] = []
    multi_activo: bool = False
    multi_agenda_modo: str = "cliente_elige"


@router.get("/status", response_model=CalendarStatus)
async def calendar_status(
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion"))],
) -> CalendarStatus:
    from core.gateway.connections import get_active_connection
    from core.plugin_manager import plugin_manager
    from core.shared.exceptions import PluginConfigError

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except PluginConfigError:
        config = None
    connection = await get_active_connection("google_calendar")
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig

    base = config if isinstance(config, CalendarConfig) else CalendarConfig()
    lista = await agendas.listar(base) if config is not None else []
    correos = await _correos_de_conexiones([a.connection_id for a in lista if a.connection_id])
    return CalendarStatus(
        configured=config is not None,
        connected=connection is not None,
        account_email=(connection.external_id if connection else "") or "",
        connection_label=(connection.label if connection else "") or "",
        timezone=getattr(config, "timezone", "America/Bogota") or "America/Bogota",
        default_duration_minutes=int(getattr(config, "event_duration_minutes", 60) or 60),
        calendars=[
            CalendarSummary(
                id=a.id,
                name=a.name,
                color=a.color,
                is_default=a.is_default,
                active=a.active,
                source="google" if a.es_google else "interno",
                connected=agendas.disponible(a, base),
                account_email=correos.get(a.connection_id, "") if a.connection_id else "",
            )
            for a in lista
        ],
        multi_activo=agendas.multi_activo(base, lista),
        multi_agenda_modo=getattr(base, "multi_agenda_modo", "cliente_elige"),
    )


async def _correos_de_conexiones(ids: list[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    """`{connection_id: correo de la cuenta}` (solo para quien tiene la sección de conexión)."""
    from core.db.models import ChannelConnection

    limpios = [i for i in ids if i is not None]
    if not limpios:
        return {}
    async with get_session() as session:
        rows = await session.execute(
            select(ChannelConnection.id, ChannelConnection.external_id).where(
                ChannelConnection.id.in_(limpios)
            )
        )
        return {r.id: (r.external_id or "") for r in rows}


# ------------------------------------------------------------------ agendas (HU-CAL-08)
class CalendarOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    aliases: list[str]
    color: str
    timezone: str  # efectiva (la propia o la del negocio)
    own_timezone: str | None = None
    business_hours: str | None = None  # propio; None = hereda el del negocio
    is_default: bool
    active: bool
    sort_order: int
    source: str  # "google" | "interno"
    connection_id: uuid.UUID | None = None
    connected: bool = True
    account_email: str = ""  # solo con permiso de conexión
    responsable_admin_id: uuid.UUID | None = None


class CalendarListOut(BaseModel):
    calendars: list[CalendarOut]
    multi_activo: bool
    multi_agenda_modo: str


class CalendarIn(BaseModel):
    name: str
    description: str = ""
    aliases: list[str] = []
    color: str | None = None
    timezone: str | None = None
    business_hours: str | None = None
    responsable_admin_id: uuid.UUID | None = None


class CalendarPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    aliases: list[str] | None = None
    color: str | None = None
    timezone: str | None = None
    business_hours: str | None = None
    # Explícito: `true` quita el horario/zona propios y vuelve a heredar los del negocio.
    inherit_timezone: bool = False
    inherit_business_hours: bool = False
    responsable_admin_id: uuid.UUID | None = None
    clear_responsable: bool = False
    active: bool | None = None
    sort_order: int | None = None
    is_default: bool | None = None


def _validar_horario(valor: str | None) -> str | None:
    if valor is None or not valor.strip():
        return None
    import json

    try:
        datos = json.loads(valor)
    except ValueError as exc:
        raise ApiError.bad_request("El horario debe ser un JSON válido.", "horario_invalido") from exc
    if not isinstance(datos, dict) or not all(isinstance(v, str) for v in datos.values()):
        raise ApiError.bad_request("El horario debe mapear día → 'HH:MM-HH:MM'.", "horario_invalido")
    return valor.strip()


def _validar_zona(valor: str | None) -> str | None:
    if valor is None or not valor.strip():
        return None
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(valor.strip())
    except Exception as exc:
        raise ApiError.bad_request("Zona horaria inválida.", "zona_invalida") from exc
    return valor.strip()


def _calendar_out(a: Any, base: Any, correo: str, *, puede_ver_cuenta: bool) -> CalendarOut:
    from pack_calendar import agendas

    return CalendarOut(
        id=a.id,
        name=a.name,
        description=a.description,
        aliases=list(a.aliases),
        color=a.color,
        timezone=a.timezone or getattr(base, "timezone", "America/Bogota"),
        own_timezone=a.timezone,
        business_hours=a.business_hours,
        is_default=a.is_default,
        active=a.active,
        sort_order=a.sort_order,
        source="google" if a.es_google else "interno",
        connection_id=a.connection_id if puede_ver_cuenta else None,
        connected=agendas.disponible(a, base),
        account_email=correo if puede_ver_cuenta else "",
        responsable_admin_id=a.responsable_admin_id,
    )


@router.get("/calendars", response_model=CalendarListOut)
async def list_calendars(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
) -> CalendarListOut:
    """Agendas del negocio con el nombre que el asistente le dice al cliente."""
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig

    config, lista = await _config_y_agendas()
    base = config if isinstance(config, CalendarConfig) else CalendarConfig()
    correos = await _correos_de_conexiones([a.connection_id for a in lista])
    return CalendarListOut(
        calendars=[
            _calendar_out(a, base, correos.get(a.connection_id, "") if a.connection_id else "", puede_ver_cuenta=True)
            for a in lista
        ],
        multi_activo=agendas.multi_activo(base, lista),
        multi_agenda_modo=getattr(base, "multi_agenda_modo", "cliente_elige"),
    )


async def _guardar_agenda(
    principal: Principal, calendar_id: uuid.UUID | None, datos: dict[str, Any]
) -> CalendarOut:
    from sqlalchemy.exc import IntegrityError

    from core.db.models import utcnow
    from pack_calendar import agendas
    from pack_calendar.config import CalendarConfig
    from pack_calendar.models import COLORES_TIPO, Calendar

    config, lista = await _config_y_agendas()
    base = config if isinstance(config, CalendarConfig) else CalendarConfig()
    if "name" in datos:
        nombre = agendas.limpiar_nombre(datos["name"])
        if not nombre:
            raise ApiError.bad_request("El nombre de la agenda es obligatorio.", "nombre_requerido")
        datos["name"] = nombre
        choque = [
            a for a in lista if agendas.normalizar(a.name) == agendas.normalizar(nombre) and a.id != calendar_id
        ]
        if choque:
            raise ApiError.bad_request("Ya hay una agenda con ese nombre.", "nombre_repetido")
    if datos.get("color") is not None and datos["color"] not in COLORES_TIPO:
        raise ApiError.bad_request("Color inválido.", "color_invalido")
    if "aliases" in datos and datos["aliases"] is not None:
        datos["aliases"] = agendas.limpiar_aliases(datos["aliases"])
    if "description" in datos and datos["description"] is not None:
        datos["description"] = " ".join(str(datos["description"]).split())[:300]
    try:
        async with get_session() as session:
            if calendar_id is None:
                fila = Calendar(
                    tenant_id=principal.tenant_id,
                    name=datos["name"],
                    color=datos.get("color") or agendas.color_siguiente([a.color for a in lista]),
                    sort_order=max((a.sort_order for a in lista), default=0) + 1,
                )
                session.add(fila)
            else:
                fila = await get_owned(session, Calendar, calendar_id, principal.tenant_id)
                if fila is None or fila.deleted_at is not None:
                    raise ApiError.not_found("Agenda no encontrada.")
            for campo, valor in datos.items():
                if campo == "is_default":
                    continue
                setattr(fila, campo, valor)
            if datos.get("is_default") is True and not fila.is_default:
                await session.flush()
                for otra in (
                    await session.execute(
                        select(Calendar).where(
                            Calendar.tenant_id == principal.tenant_id,
                            Calendar.is_default.is_(True),
                            Calendar.deleted_at.is_(None),
                        )
                    )
                ).scalars():
                    otra.is_default = False
                await session.flush()
                fila.is_default = True
            fila.updated_at = utcnow()
            await session.flush()
            agendas.invalidar(principal.tenant_id)
            nueva_id = fila.id
    except IntegrityError as exc:
        raise ApiError.bad_request("Ya hay una agenda con ese nombre.", "nombre_repetido") from exc
    agendas.invalidar(principal.tenant_id)
    lista = await agendas.listar(base)
    agenda = next(a for a in lista if a.id == nueva_id)
    correos = await _correos_de_conexiones([agenda.connection_id])
    return _calendar_out(agenda, base, correos.get(agenda.connection_id, "") if agenda.connection_id else "", puede_ver_cuenta=True)


@router.post("/calendars", response_model=CalendarOut, status_code=201)
async def create_calendar(
    payload: CalendarIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:configuracion", write=True))],
) -> CalendarOut:
    """Crea una agenda interna (sin cuenta de Google). Las de Google nacen al conectar la cuenta."""
    datos: dict[str, Any] = {
        "name": payload.name,
        "description": payload.description,
        "aliases": payload.aliases,
        "timezone": _validar_zona(payload.timezone),
        "business_hours": _validar_horario(payload.business_hours),
        "responsable_admin_id": payload.responsable_admin_id,
    }
    if payload.color is not None:
        datos["color"] = payload.color
    if payload.responsable_admin_id is not None:
        await _validar_responsable(principal.tenant_id, payload.responsable_admin_id)
    return await _guardar_agenda(principal, None, datos)


@router.put("/calendars/{calendar_id}", response_model=CalendarOut)
async def update_calendar(
    calendar_id: uuid.UUID,
    payload: CalendarPatch,
    principal: Annotated[Principal, Depends(require("pack_calendar:configuracion", write=True))],
) -> CalendarOut:
    """Edita una agenda: nombre visible, descripción, alias, color, horario, zona, orden y estado."""
    datos: dict[str, Any] = {}
    for campo in ("name", "description", "aliases", "color", "sort_order", "active", "is_default"):
        valor = getattr(payload, campo)
        if valor is not None:
            datos[campo] = valor
    if payload.inherit_timezone:
        datos["timezone"] = None
    elif payload.timezone is not None:
        datos["timezone"] = _validar_zona(payload.timezone)
    if payload.inherit_business_hours:
        datos["business_hours"] = None
    elif payload.business_hours is not None:
        datos["business_hours"] = _validar_horario(payload.business_hours)
    if payload.clear_responsable:
        datos["responsable_admin_id"] = None
    elif payload.responsable_admin_id is not None:
        await _validar_responsable(principal.tenant_id, payload.responsable_admin_id)
        datos["responsable_admin_id"] = payload.responsable_admin_id
    if datos.get("active") is False or datos.get("is_default") is False:
        _, lista = await _config_y_agendas()
        actual = next((a for a in lista if a.id == calendar_id), None)
        if actual is not None and actual.is_default:
            raise ApiError.bad_request(
                "La agenda principal no se desactiva: marca otra como principal primero.",
                "agenda_principal",
            )
    return await _guardar_agenda(principal, calendar_id, datos)


class CalendarDelete(BaseModel):
    move_to: uuid.UUID | None = None


@router.post("/calendars/{calendar_id}/delete", status_code=204)
async def delete_calendar(
    calendar_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:configuracion", write=True))],
    payload: CalendarDelete | None = None,
) -> Response:
    """Elimina (suave) una agenda. No borra la cuenta de Google: eso va en Conexiones.

    La principal no se elimina. Si tiene citas futuras, hay que pasar `move_to` (otra agenda)
    para moverlas; si no, 409.
    """
    from sqlalchemy import update as sql_update

    from core.db.models import utcnow
    from pack_calendar import agendas
    from pack_calendar.models import Calendar, CalendarBlock

    _, lista = await _config_y_agendas()
    actual = next((a for a in lista if a.id == calendar_id), None)
    if actual is None:
        raise ApiError.not_found("Agenda no encontrada.")
    if actual.is_default:
        raise ApiError.bad_request("La agenda principal no se elimina.", "agenda_principal")
    destino = payload.move_to if payload else None
    if destino is not None and not any(a.id == destino and a.id != calendar_id for a in lista):
        raise ApiError.bad_request("La agenda de destino no es de este negocio.", "agenda_invalida")
    ahora = utcnow()
    async with get_session() as session:
        futuras = (
            await session.execute(
                select(Appointment.id).where(
                    Appointment.tenant_id == principal.tenant_id,
                    Appointment.calendar_id == calendar_id,
                    Appointment.deleted_at.is_(None),
                    Appointment.status == "scheduled",
                    Appointment.start_at >= ahora,
                ).limit(1)
            )
        ).first()
        if futuras and destino is None:
            raise ApiError(
                409, "agenda_con_citas", "La agenda tiene citas futuras: elige a qué agenda moverlas."
            )
        if destino is not None:
            for modelo in (Appointment, CalendarBlock):
                await session.execute(
                    sql_update(modelo)
                    .where(modelo.tenant_id == principal.tenant_id, modelo.calendar_id == calendar_id)
                    .values(calendar_id=destino)
                )
        fila = await get_owned(session, Calendar, calendar_id, principal.tenant_id)
        if fila is not None:
            fila.deleted_at = ahora
    agendas.invalidar(principal.tenant_id)
    return Response(status_code=204)


# ------------------------------------------------------------------ tipos de cita
class TypeOut(BaseModel):
    id: uuid.UUID
    name: str
    duration_minutes: int
    description: str
    active: bool
    # HU-SH-02: colaborador que recibe los avisos de este tipo y enlace de la reunión.
    responsable_admin_id: uuid.UUID | None = None
    enlace_reunion: str | None = None
    # HU-UX-08: clave de la paleta categórica (`cat-1`…`cat-4`) y código corto.
    color: str = "cat-1"
    codigo: str = ""


ColorTipo = Literal["cat-1", "cat-2", "cat-3", "cat-4"]


class TypeSave(BaseModel):
    name: str
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    description: str = ""
    responsable_admin_id: uuid.UUID | None = None
    enlace_reunion: str = ""
    # HU-UX-08. Sin enviar (None) se conservan al editar; al crear, el color rota
    # la paleta y el código sale del nombre.
    color: ColorTipo | None = None
    codigo: str | None = Field(default=None, max_length=6)


def codigo_por_defecto(nombre: str) -> str:
    """Las 3 primeras letras o cifras del nombre, en mayúsculas («Valoración» → «VAL»)."""
    return "".join(c for c in nombre if c.isalnum())[:3].upper()


def _codigo(valor: str) -> str:
    return valor.strip().upper()


async def _validar_responsable(tenant_id: uuid.UUID, admin_id: uuid.UUID | None) -> None:
    """El responsable tiene que ser colaborador de ESTE negocio."""
    if admin_id is None:
        return
    from core.db.models import TenantUser

    async with get_session() as session:
        miembro = (
            await session.execute(
                select(TenantUser.id).where(TenantUser.tenant_id == tenant_id, TenantUser.admin_id == admin_id)
            )
        ).first()
    if miembro is None:
        raise ApiError.bad_request("El responsable no es colaborador de este negocio.", "responsable_invalido")


def _enlace(valor: str) -> str | None:
    enlace = (valor or "").strip()
    if enlace and not enlace.startswith(("https://", "http://")):
        raise ApiError.bad_request("El enlace de la reunión tiene que empezar con https://.", "enlace_invalido")
    return enlace or None


@router.get("/types", response_model=list[TypeOut])
async def list_types(
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos"))],
) -> list[TypeOut]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(AppointmentType)
                    .where(
                        AppointmentType.tenant_id == principal.tenant_id,
                        AppointmentType.deleted_at.is_(None),
                    )
                    .order_by(AppointmentType.name)
                )
            )
            .scalars()
            .all()
        )
    return [
        TypeOut(
            id=t.id,
            name=t.name,
            duration_minutes=t.duration_minutes,
            description=t.description or "",
            active=bool(t.active),
            responsable_admin_id=t.responsable_admin_id,
            enlace_reunion=t.enlace_reunion,
            color=t.color or "cat-1",
            codigo=t.codigo or codigo_por_defecto(t.name),
        )
        for t in rows
    ]


@router.post("/types", status_code=204)
async def create_type(
    payload: TypeSave,
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos", write=True))],
) -> Response:
    name = payload.name.strip()
    if not name:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    if principal.tenant_id is None:
        raise ApiError.bad_request("Sin negocio activo.", "no_tenant")
    await _validar_responsable(principal.tenant_id, payload.responsable_admin_id)
    async with get_session() as session:
        color = payload.color
        if color is None:
            # Rota la paleta para que cada tipo nuevo se distinga del anterior.
            existentes = (
                await session.execute(
                    select(func.count())
                    .select_from(AppointmentType)
                    .where(
                        AppointmentType.tenant_id == principal.tenant_id,
                        AppointmentType.deleted_at.is_(None),
                    )
                )
            ).scalar_one()
            color = COLORES_TIPO[existentes % len(COLORES_TIPO)]
        codigo = _codigo(payload.codigo or "") or codigo_por_defecto(name)
        session.add(
            AppointmentType(
                tenant_id=principal.tenant_id,
                name=name,
                duration_minutes=max(5, payload.duration_minutes),
                description=payload.description,
                responsable_admin_id=payload.responsable_admin_id,
                enlace_reunion=_enlace(payload.enlace_reunion),
                color=color,
                codigo=codigo,
            )
        )
    return Response(status_code=204)


@router.put("/types/{type_id}", status_code=204)
async def update_type(
    type_id: uuid.UUID,
    payload: TypeSave,
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos", write=True))],
) -> Response:
    """Edita un tipo de cita (HU-SH-02: responsable y enlace de la reunión)."""
    name = payload.name.strip()
    if not name:
        raise ApiError.bad_request("El nombre es obligatorio.", "missing_name")
    if principal.tenant_id is None:
        raise ApiError.bad_request("Sin negocio activo.", "no_tenant")
    await _validar_responsable(principal.tenant_id, payload.responsable_admin_id)
    async with get_session() as session:
        tipo = await get_owned(session, AppointmentType, type_id, principal.tenant_id)
        if tipo is None or tipo.deleted_at is not None:
            raise ApiError.not_found("Tipo de cita no encontrado.")
        tipo.name = name
        tipo.duration_minutes = max(5, payload.duration_minutes)
        tipo.description = payload.description
        tipo.responsable_admin_id = payload.responsable_admin_id
        tipo.enlace_reunion = _enlace(payload.enlace_reunion)
        if payload.color is not None:
            tipo.color = payload.color
        if payload.codigo is not None:
            tipo.codigo = _codigo(payload.codigo) or codigo_por_defecto(name)
    return Response(status_code=204)


@router.post("/types/{type_id}/delete", status_code=204)
async def delete_type(
    type_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:tipos", write=True))],
) -> Response:
    from core.db.models import utcnow

    async with get_session() as session:
        row = await get_owned(session, AppointmentType, type_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Tipo de cita no encontrado.")
        row.deleted_at = utcnow()
        row.active = False
    return Response(status_code=204)


# ------------------------------------------------------------------ citas
class NotificationOut(BaseModel):
    """Resultado real del aviso al cliente (HU-CAL-05)."""

    email_sent: bool
    email_reason: str
    whatsapp_sent: bool


async def _notify_client(
    action: str,
    *,
    title: str,
    start: datetime,
    end: datetime,
    attendee_email: str | None,
    conversation_id: uuid.UUID | None,
) -> NotificationOut:
    """Avisa al cliente por correo y WhatsApp cuando el negocio mueve (`moved`) o
    cancela (`cancelled`) una cita desde el panel (D-SPEC-24-10, paridad con el
    panel legado). Best-effort: un fallo de envío se informa, nunca se lanza."""
    from zoneinfo import ZoneInfo

    from core.plugin_manager import plugin_manager
    from core.shared.logger import get_logger
    from pack_calendar.config import (
        DEFAULT_CANCELLED_MESSAGE,
        DEFAULT_MOVED_MESSAGE,
        CalendarConfig,
    )
    from pack_calendar.skills import CalendarSkill, _calendar_link

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    if not isinstance(config, CalendarConfig):
        return NotificationOut(email_sent=False, email_reason="sin_configuracion", whatsapp_sent=False)

    skill = CalendarSkill()
    title = title or "tu cita"
    link = ""
    if action != "cancelled":
        business = await skill._business_name()
        link = _calendar_link(start, end, title, f"Cita agendada con {business}", config.timezone)
    local = start.astimezone(ZoneInfo(config.timezone))
    if action == "cancelled":
        template = config.appointment_cancelled_message or DEFAULT_CANCELLED_MESSAGE
    else:
        template = config.appointment_moved_message or DEFAULT_MOVED_MESSAGE
    try:
        whatsapp_sent = await skill._send_client_notice(
            conversation_id,
            template,
            fecha=local.strftime("%d/%m/%Y"),
            hora=local.strftime("%H:%M"),
            titulo=title,
            link=link,
        )
    except Exception:
        get_logger(__name__).warning("calendar_panel_whatsapp_failed", exc_info=True)
        whatsapp_sent = False
    email_sent, email_reason = await skill._notify_appointment_email(
        action, title=title, start=start, attendee_email=attendee_email, config=config, link=link
    )
    return NotificationOut(email_sent=email_sent, email_reason=email_reason, whatsapp_sent=whatsapp_sent)


class AppointmentOut(BaseModel):
    id: uuid.UUID
    title: str
    attendee_email: str | None
    start_at: datetime
    end_at: datetime
    status: str
    notes: str
    google_event_id: str | None
    appointment_type_id: uuid.UUID | None
    created_by: str
    timezone: str
    # Resultado del aviso al cliente al mover desde el panel; None si no se avisó.
    notification: NotificationOut | None = None
    customer_id: uuid.UUID | None = None  # HU-SH-02
    asignado_admin_id: uuid.UUID | None = None
    calendar_id: uuid.UUID | None = None  # HU-CAL-08: agenda (None = la principal)


@router.get("/appointments", response_model=list[AppointmentOut])
async def list_appointments(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    include_past: Annotated[bool, Query()] = False,
) -> list[AppointmentOut]:
    """Próximas citas (por defecto 30 días) del negocio."""
    now = datetime.now(UTC)
    start = now - timedelta(days=1) if include_past else now - timedelta(hours=1)
    end = now + timedelta(days=days)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Appointment)
                    .where(
                        Appointment.tenant_id == principal.tenant_id,
                        Appointment.deleted_at.is_(None),
                        Appointment.start_at >= start,
                        Appointment.start_at <= end,
                    )
                    .order_by(Appointment.start_at)
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    return [
        AppointmentOut(
            id=a.id,
            title=a.title,
            attendee_email=a.attendee_email,
            start_at=a.start_at,
            end_at=a.end_at,
            status=a.status,
            notes=a.notes or "",
            google_event_id=a.google_event_id,
            appointment_type_id=a.appointment_type_id,
            created_by=a.created_by,
            timezone=a.timezone,
            calendar_id=a.calendar_id,
        )
        for a in rows
    ]


class AppointmentCancel(BaseModel):
    notify: bool = True


class AppointmentCancelOut(BaseModel):
    status: str
    # Resultado del aviso al cliente; None si no se avisó (o ya estaba cancelada).
    notification: NotificationOut | None = None


@router.post("/appointments/{appointment_id}/cancel", response_model=AppointmentCancelOut)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
    payload: Annotated[AppointmentCancel | None, Body()] = None,
) -> AppointmentCancelOut:
    """Cancela la cita: borra el evento en Google (best-effort), marca el espejo,
    registra la actividad y, con `notify` (default), avisa al cliente por correo
    y WhatsApp (HU-CAL-05). Idempotente: cancelar una cita ya cancelada no
    vuelve a avisar."""
    from core.db.models import utcnow
    from pack_calendar.skills import CalendarSkill

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
    if row is None:
        raise ApiError.not_found("Cita no encontrada.")
    if row.status == "cancelled":
        return AppointmentCancelOut(status="cancelled")

    if row.google_event_id:
        try:
            from core.plugin_manager import plugin_manager

            # Sin ToolContext: el cliente acotado del pack (`google_api.default_http`).
            from pack_calendar import agendas, google_api
            from pack_calendar.admin import _google_token

            config = await plugin_manager.load_config(PLUGIN_ID)
            config = await agendas.config_para(row.calendar_id, config)
            token = await _google_token(config)
            await google_api.delete_event(
                google_api.default_http(), config.calendar_id, token, row.google_event_id
            )
        except Exception:
            from core.shared.logger import get_logger

            get_logger(__name__).warning(
                "calendar_google_cancel_failed", appointment_id=str(appointment_id)
            )

    async with get_session() as session:
        row_db = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row_db is not None:
            row_db.status = "cancelled"
            row_db.updated_at = utcnow()

    await CalendarSkill()._record_event(
        action="cancelada",
        title=row.title or "",
        appointment_id=row.id,
        conversation_id=row.conversation_id,
        old_start=row.start_at,
        actor="panel",
    )
    notification = None
    if payload is None or payload.notify:
        notification = await _notify_client(
            "cancelled",
            title=row.title,
            start=row.start_at,
            end=row.end_at,
            attendee_email=row.attendee_email,
            conversation_id=row.conversation_id,
        )
    return AppointmentCancelOut(status="cancelled", notification=notification)


class AppointmentIn(BaseModel):
    title: str
    start_at: datetime
    end_at: datetime
    attendee_email: str = ""
    notes: str = ""
    appointment_type_id: uuid.UUID | None = None
    # HU-SH-02: cliente del negocio al que se le avisa por WhatsApp (opcional).
    customer_id: uuid.UUID | None = None
    # HU-CAL-08: agenda de la cita. Obligatoria si el asistente ofrece varias agendas.
    calendar_id: uuid.UUID | None = None


@router.post("/appointments", response_model=AppointmentOut, status_code=201)
async def create_appointment(
    payload: AppointmentIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> AppointmentOut:
    """Crea una cita: local y, si el negocio tiene Google conectado, también el evento real."""
    from core.plugin_manager import plugin_manager
    from pack_calendar.admin import PLUGIN_ID, _google_create_event, _google_token
    from pack_calendar.models import Appointment

    titulo = payload.title.strip()
    if not titulo:
        raise ApiError.bad_request("El título es obligatorio.", "missing_title")
    if payload.end_at <= payload.start_at:
        raise ApiError.bad_request("La hora de fin debe ser posterior al inicio.", "invalid_range")

    attendee = payload.attendee_email.strip() or None
    notas = payload.notes.strip()
    google_event_id: str | None = None
    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        # Sin configuración del pack (p. ej. admin master en un negocio que no lo
        # tiene habilitado) la cita es local: no hay Google al que empujarla.
        config = None
    from pack_calendar import agendas as _agendas

    _lista = (await _config_y_agendas())[1]
    agenda = await _agenda_de_alta(payload.calendar_id, config, _lista)
    if config is not None:
        config = _agendas.config_de(agenda, config)
    # Solo se empuja a Google si la agenda es de Google; en `interno` la cita es local
    # (antes se intentaba Google aunque el modo fuera interno → error 400).
    if config is not None and getattr(config, "source", "interno") == "google":
        try:
            token = await _google_token(config)
            evento = await _google_create_event(
                config,
                token,
                title=titulo,
                start=payload.start_at,
                end=payload.end_at,
                attendee_email=attendee,
                notes=notas,
            )
            google_event_id = evento.get("id")
        except Exception as exc:
            raise ApiError.bad_request(
                "Google Calendar rechazó la creación (revisá la conexión).", "google_rejected"
            ) from exc

    from core.db.models import Customer
    from pack_calendar.demo import es_solape

    tipo = None
    async with get_session() as session:
        if payload.appointment_type_id is not None:
            tipo = await get_owned(session, AppointmentType, payload.appointment_type_id, principal.tenant_id)
            if tipo is None:
                raise ApiError.bad_request("El tipo de cita no es de este negocio.", "tipo_invalido")
        if payload.customer_id is not None:
            cliente = await session.get(Customer, payload.customer_id)
            if cliente is None or cliente.tenant_id != principal.tenant_id or cliente.deleted_at is not None:
                raise ApiError.bad_request("El cliente no es de este negocio.", "cliente_invalido")
    zona = await _timezone_name()
    try:
        async with get_session() as session:
            cita = Appointment(
                tenant_id=principal.tenant_id,
                appointment_type_id=payload.appointment_type_id,
                customer_id=payload.customer_id,
                title=titulo,
                attendee_email=attendee,
                start_at=payload.start_at,
                end_at=payload.end_at,
                status="scheduled",
                notes=notas,
                google_event_id=google_event_id,
                created_by="panel",
                timezone=zona,
                asignado_admin_id=getattr(tipo, "responsable_admin_id", None),
                calendar_id=agenda.id if agenda is not None else None,
            )
            session.add(cita)
            await session.flush()
            out = AppointmentOut(
                id=cita.id,
                title=cita.title,
                attendee_email=cita.attendee_email,
                start_at=cita.start_at,
                end_at=cita.end_at,
                status=cita.status,
                notes=cita.notes,
                google_event_id=cita.google_event_id,
                appointment_type_id=cita.appointment_type_id,
                created_by=cita.created_by,
                timezone=cita.timezone,
                customer_id=cita.customer_id,
                asignado_admin_id=cita.asignado_admin_id,
                calendar_id=cita.calendar_id,
            )
    except Exception as exc:
        if es_solape(exc):
            raise ApiError.bad_request("Ese horario ya está ocupado por otra cita.", "horario_ocupado") from exc
        raise
    await _avisar_cita_desde_panel(out, tipo, config)
    return out


async def _avisar_cita_desde_panel(out: AppointmentOut, tipo: Any, config: Any) -> None:
    """Historial + aviso al equipo y, si la cita tiene cliente, aviso al cliente (HU-SH-02)."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_APPOINTMENT_CONFIRMED
    from core.shared.tenant import get_current_tenant_id
    from pack_calendar.config import CalendarConfig
    from pack_calendar.demo import datos_de_cita, enlace_de
    from pack_calendar.skills import CalendarSkill

    await CalendarSkill()._record_event(
        action="programada", title=out.title, appointment_id=out.id, new_start=out.start_at, actor="panel"
    )
    tenant_id = get_current_tenant_id()
    if out.customer_id is None or tenant_id is None:
        return
    config = config if isinstance(config, CalendarConfig) else CalendarConfig()
    datos = datos_de_cita(out.title, out.start_at, out.timezone, enlace_de(tipo, config))
    async with get_session() as session:
        await emit_domain(
            session,
            tenant_id=tenant_id,
            topic=ON_APPOINTMENT_CONFIRMED,
            dedupe_key=f"{ON_APPOINTMENT_CONFIRMED}:{out.id}",
            payload={
                "appointment_id": str(out.id),
                "customer_id": str(out.customer_id),
                **datos,
                "texto": config.cita_desde_panel_texto,
            },
        )


# ---------------------------------------------------------------- bloqueos
class BlockIn(BaseModel):
    start_at: datetime
    end_at: datetime
    reason: str = ""
    # HU-CAL-08: agenda que se bloquea. Sin ella, la principal (los bloqueos viejos también).
    calendar_id: uuid.UUID | None = None


class BlockOut(BaseModel):
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    reason: str
    calendar_id: uuid.UUID | None = None


@router.get("/blocks", response_model=list[BlockOut])
async def list_blocks(
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos"))],
    days: Annotated[int, Query(ge=1, le=365)] = 180,
    include_past: Annotated[bool, Query()] = False,
) -> list[BlockOut]:
    """Bloqueos de la agenda interna (vacaciones, ausencia, feriados, almuerzo)."""
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    now = utcnow()
    start = now - timedelta(days=1) if include_past else now
    end = now + timedelta(days=days)
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(CalendarBlock)
                    .where(
                        CalendarBlock.tenant_id == principal.tenant_id,
                        CalendarBlock.deleted_at.is_(None),
                        CalendarBlock.end_at >= start,
                        CalendarBlock.start_at <= end,
                    )
                    .order_by(CalendarBlock.start_at)
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    return [
        BlockOut(
            id=b.id, start_at=b.start_at, end_at=b.end_at, reason=b.reason or "", calendar_id=b.calendar_id
        )
        for b in rows
    ]


@router.post("/blocks", response_model=BlockOut, status_code=201)
async def create_block(
    payload: BlockIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> BlockOut:
    from pack_calendar.models import CalendarBlock

    if principal.tenant_id is None:
        raise ApiError.bad_request("Sin negocio activo.", "no_tenant")
    if payload.end_at <= payload.start_at:
        raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")
    config, lista = await _config_y_agendas()
    agenda = await _agenda_de_alta(payload.calendar_id, config, lista)
    async with get_session() as session:
        block = CalendarBlock(
            tenant_id=principal.tenant_id,
            start_at=payload.start_at,
            end_at=payload.end_at,
            reason=payload.reason.strip(),
            calendar_id=agenda.id if agenda is not None else None,
        )
        session.add(block)
        await session.flush()
        return BlockOut(
            id=block.id,
            start_at=block.start_at,
            end_at=block.end_at,
            reason=block.reason,
            calendar_id=block.calendar_id,
        )


@router.delete("/blocks/{block_id}", status_code=204)
async def delete_block(
    block_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> Response:
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    async with get_session() as session:
        row = await get_owned(session, CalendarBlock, block_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Bloqueo no encontrado.")
        row.deleted_at = utcnow()
    return Response(status_code=204)


# ---------------------------------------------------------------- vista semanal
class WeekItem(BaseModel):
    kind: str  # "appointment" | "block"
    id: uuid.UUID
    title: str
    start_at: datetime
    end_at: datetime
    status: str
    source: str  # "local" | "google"


class WeekOut(BaseModel):
    start: str
    days: list[dict[str, Any]]
    items: list[WeekItem]


@router.get("/week", response_model=WeekOut)
async def week_view(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    start: Annotated[str | None, Query()] = None,
) -> WeekOut:
    """Citas + bloqueos de la semana que arranca en `start` (YYYY-MM-DD, lunes si se omite)."""
    from core.config import get_settings
    from pack_calendar.config import CalendarConfig
    from pack_calendar.models import Appointment, CalendarBlock

    tzname = getattr(get_settings(), "calendar_timezone", None) or "America/Bogota"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tzname)
    except Exception:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Bogota")
    now = datetime.now(UTC)
    if start:
        base = datetime.fromisoformat(start).replace(tzinfo=tz)
    else:
        hoy = now.astimezone(tz)
        base = (hoy - timedelta(days=hoy.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    end = base + timedelta(days=7)
    start_utc, end_utc = base.astimezone(UTC), end.astimezone(UTC)

    # Horario de atención del negocio (si el pack está configurado).
    try:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None

    async with get_session() as session:
        citas = (
            (
                await session.execute(
                    select(Appointment)
                    .where(
                        Appointment.tenant_id == principal.tenant_id,
                        Appointment.deleted_at.is_(None),
                        Appointment.start_at < end_utc,
                        Appointment.end_at > start_utc,
                    )
                    .order_by(Appointment.start_at)
                )
            )
            .scalars()
            .all()
        )
        blocks = (
            (
                await session.execute(
                    select(CalendarBlock)
                    .where(
                        CalendarBlock.tenant_id == principal.tenant_id,
                        CalendarBlock.deleted_at.is_(None),
                        CalendarBlock.start_at < end_utc,
                        CalendarBlock.end_at > start_utc,
                    )
                    .order_by(CalendarBlock.start_at)
                )
            )
            .scalars()
            .all()
        )

    items: list[WeekItem] = [
        WeekItem(
            kind="appointment",
            id=c.id,
            title=c.title or "Cita",
            start_at=c.start_at,
            end_at=c.end_at,
            status=c.status,
            source="google" if c.google_event_id else "local",
        )
        for c in citas
    ] + [
        WeekItem(
            kind="block",
            id=b.id,
            title=b.reason or "Bloqueo",
            start_at=b.start_at,
            end_at=b.end_at,
            status="blocked",
            source="local",
        )
        for b in blocks
    ]
    items.sort(key=lambda i: i.start_at)

    from pack_calendar.skills import _open_window

    days = []
    for i in range(7):
        d = base + timedelta(days=i)
        iso = d.strftime("%Y-%m-%d")
        days.append(
            {
                "date": iso,
                "weekday": d.weekday(),
                "business_hours": _open_window(config, iso) if isinstance(config, CalendarConfig) else "",
            }
        )
    return WeekOut(start=base.strftime("%Y-%m-%d"), days=days, items=items)


# ---------------------------------------------------------------- eventos (FullCalendar)
class EventOut(BaseModel):
    id: str
    title: str
    start: str
    end: str
    kind: str  # appointment | block
    status: str
    source: str  # local | google
    color: str
    can_cancel: bool
    attendee_email: str = ""
    notes: str = ""
    appointment_type_id: str = ""
    html_link: str = ""
    # HU-UX-08: color (`cat-1`…`cat-4`) y código del tipo de la cita; vacíos sin tipo.
    tipo_color: str = ""
    tipo_codigo: str = ""
    # HU-CAL-08: agenda del evento y su color (los bloqueos viejos y las citas sin agenda
    # son de la principal).
    calendar_id: str = ""
    calendar_color: str = ""


@router.get("/events")
async def calendar_events(
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
    start: Annotated[str | None, Query()] = None,
    end: Annotated[str | None, Query()] = None,
    calendar_ids: Annotated[str | None, Query(description="IDs de agenda separados por coma")] = None,
) -> dict[str, Any]:
    """Citas + bloqueos en formato FullCalendar para la grilla del panel (HU-CAL-03).

    `start`/`end` ISO (o YYYY-MM-DD). Sin parámetros, cubre el mes en curso.
    """
    from pack_calendar.models import Appointment, CalendarBlock

    now = datetime.now(UTC)
    def _parse(value: str | None, default: datetime) -> datetime:
        if not value:
            return default
        try:
            return datetime.fromisoformat(value).astimezone(UTC)
        except (ValueError, TypeError):
            return default

    ini = _parse(start, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    fin = _parse(end, ini + timedelta(days=31))
    if fin < ini:
        fin = ini + timedelta(days=31)
    if fin - ini > timedelta(days=400):
        fin = ini + timedelta(days=400)

    async with get_session() as session:
        citas = (await session.execute(
            select(Appointment).where(
                Appointment.tenant_id == principal.tenant_id,
                Appointment.deleted_at.is_(None),
                Appointment.start_at < fin,
                Appointment.end_at > ini,
            ).order_by(Appointment.start_at)
        )).scalars().all()
        blocks = (await session.execute(
            select(CalendarBlock).where(
                CalendarBlock.tenant_id == principal.tenant_id,
                CalendarBlock.deleted_at.is_(None),
                CalendarBlock.start_at < fin,
                CalendarBlock.end_at > ini,
            ).order_by(CalendarBlock.start_at)
        )).scalars().all()
        # HU-UX-08: los tipos van en la respuesta para la leyenda y el color de cada
        # cita (quien ve el calendario no necesita el permiso de Tipos de cita).
        tipos = (await session.execute(
            select(AppointmentType).where(
                AppointmentType.tenant_id == principal.tenant_id,
                AppointmentType.deleted_at.is_(None),
            ).order_by(AppointmentType.name)
        )).scalars().all()

    config_ev, lista_ev = await _config_y_agendas()
    principal_ev = next((a for a in lista_ev if a.is_default), None)
    por_agenda = {a.id: a for a in lista_ev}
    filtro: set[uuid.UUID] | None = None
    if calendar_ids:
        try:
            filtro = {uuid.UUID(x) for x in calendar_ids.split(",") if x.strip()}
        except ValueError as exc:
            raise ApiError.bad_request("Agenda inválida.", "agenda_invalida") from exc

    def _de_agenda(calendar_id: uuid.UUID | None) -> Any:
        return por_agenda.get(calendar_id) if calendar_id is not None else principal_ev

    def _visible(calendar_id: uuid.UUID | None) -> bool:
        agenda = _de_agenda(calendar_id)
        return filtro is None or agenda is None or agenda.id in filtro

    citas = [a for a in citas if _visible(a.calendar_id)]
    blocks = [b for b in blocks if _visible(b.calendar_id)]

    por_tipo = {t.id: t for t in tipos}
    events: list[dict[str, Any]] = []
    for a in citas:
        tipo = por_tipo.get(a.appointment_type_id) if a.appointment_type_id else None
        events.append({
            "id": f"appt-{a.id}",
            "title": a.title or "Cita",
            "start": a.start_at.astimezone(UTC).isoformat(),
            "end": a.end_at.astimezone(UTC).isoformat(),
            "kind": "appointment",
            "status": a.status,
            "source": "google" if a.google_event_id else "local",
            "color": "#198754" if a.status == "scheduled" else "#6c757d",
            "can_cancel": a.status == "scheduled",
            "attendee_email": a.attendee_email or "",
            "notes": a.notes or "",
            "appointment_type_id": str(a.appointment_type_id) if a.appointment_type_id else "",
            "tipo_color": (tipo.color or "cat-1") if tipo else "",
            "tipo_codigo": (tipo.codigo or codigo_por_defecto(tipo.name)) if tipo else "",
            "calendar_id": str(_de_agenda(a.calendar_id).id) if _de_agenda(a.calendar_id) else "",
            "calendar_color": _de_agenda(a.calendar_id).color if _de_agenda(a.calendar_id) else "",
        })
    for b in blocks:
        events.append({
            "id": f"block-{b.id}",
            "title": b.reason or "Bloqueo",
            "start": b.start_at.astimezone(UTC).isoformat(),
            "end": b.end_at.astimezone(UTC).isoformat(),
            "kind": "block",
            "status": "blocked",
            "source": "local",
            "color": "#dc3545",
            "can_cancel": True,
            "attendee_email": "",
            "notes": "",
            "appointment_type_id": "",
            "tipo_color": "",
            "tipo_codigo": "",
            "calendar_id": str(_de_agenda(b.calendar_id).id) if _de_agenda(b.calendar_id) else "",
            "calendar_color": _de_agenda(b.calendar_id).color if _de_agenda(b.calendar_id) else "",
        })
    from pack_calendar import agendas as _ag

    base_ev = config_ev if config_ev is not None else _ag.CalendarConfig()
    leyenda_agendas = [
        {
            "id": str(a.id),
            "name": a.name,
            "color": a.color,
            "is_default": a.is_default,
            "active": a.active,
            "disponible": _ag.disponible(a, base_ev),
        }
        for a in lista_ev
    ]
    leyenda = [
        {
            "id": str(t.id),
            "name": t.name,
            "color": t.color or "cat-1",
            "codigo": t.codigo or codigo_por_defecto(t.name),
        }
        for t in tipos
        if t.active
    ]
    return {
        "events": events,
        "tipos": leyenda,
        "calendars": leyenda_agendas,
        "multi_activo": _ag.multi_activo(base_ev, lista_ev),
        "timezone": await _timezone_name(),
    }


class AppointmentPatch(BaseModel):
    title: str | None = None
    attendee_email: str | None = None
    notes: str | None = None
    appointment_type_id: uuid.UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    notify: bool = True


@router.patch("/appointments/{appointment_id}", response_model=AppointmentOut)
async def patch_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentPatch,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> AppointmentOut:
    """Edita campos/horario de una cita y, si se mueve y `notify`, avisa al cliente
    por correo y WhatsApp y a los colaboradores (HU-CAL-02/05). Valida
    solapamiento en el servidor."""
    from core.db.models import utcnow
    from pack_calendar.models import Appointment
    from pack_calendar.skills import CalendarSkill

    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Cita no encontrada.")
        old_start, old_end = row.start_at, row.end_at
        cita_calendar_id = row.calendar_id

    new_start = payload.start_at or old_start
    new_end = payload.end_at or old_end
    if new_end <= new_start:
        raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")

    moved = new_start != old_start or new_end != old_end
    # Validación dura (solo agenda interna): no doble reserva al mover.
    from core.plugin_manager import plugin_manager

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    from pack_calendar.config import CalendarConfig

    if isinstance(config, CalendarConfig):
        from pack_calendar import agendas

        config = await agendas.config_para(cita_calendar_id, config)
    if moved and isinstance(config, CalendarConfig) and getattr(config, "source", "interno") != "google":
        conflict = await CalendarSkill()._validate_internal_slot(
            config, new_start, new_end, exclude_appt_id=appointment_id, calendar_id=cita_calendar_id
        )
        if conflict is not None:
            raise ApiError.bad_request(
                "Ese horario ya está ocupado o está fuera del horario de atención.",
                conflict,
            )

    from pack_calendar.demo import es_solape

    try:
        async with get_session() as session:
            row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
            if row is None:
                raise ApiError.not_found("Cita no encontrada.")
            if payload.title is not None:
                row.title = payload.title.strip() or row.title
            if payload.attendee_email is not None:
                row.attendee_email = payload.attendee_email.strip() or None
            if payload.notes is not None:
                row.notes = payload.notes.strip()
            if payload.appointment_type_id is not None:
                if await get_owned(session, AppointmentType, payload.appointment_type_id, principal.tenant_id) is None:
                    raise ApiError.bad_request("El tipo de cita no es de este negocio.", "tipo_invalido")
                row.appointment_type_id = payload.appointment_type_id
            row.start_at = new_start
            row.end_at = new_end
            row.updated_at = utcnow()
            conversation_id = row.conversation_id
            out = AppointmentOut(
                id=row.id, title=row.title, attendee_email=row.attendee_email,
                start_at=row.start_at, end_at=row.end_at, status=row.status, notes=row.notes,
                google_event_id=row.google_event_id, appointment_type_id=row.appointment_type_id,
                created_by=row.created_by, timezone=row.timezone, calendar_id=row.calendar_id,
            )
    except Exception as exc:
        if es_solape(exc):
            raise ApiError.bad_request("Ese horario ya está ocupado por otra cita.", "horario_ocupado") from exc
        raise

    if moved:
        from pack_calendar.skills import CalendarSkill as _CS

        skill = _CS()
        await skill._record_event(
            action="reprogramada",
            title=out.title,
            appointment_id=appointment_id,
            old_start=old_start,
            new_start=new_start,
            actor="panel",
        )
        if payload.notify:
            out.notification = await _notify_client(
                "moved",
                title=out.title,
                start=new_start,
                end=new_end,
                attendee_email=out.attendee_email,
                conversation_id=conversation_id,
            )
    return out


class ActivityOut(BaseModel):
    action: str
    title: str
    actor: str
    old_start_at: datetime | None
    new_start_at: datetime | None
    created_at: datetime


@router.get("/appointments/{appointment_id}/activities", response_model=list[ActivityOut])
async def appointment_activities(
    appointment_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas"))],
) -> list[ActivityOut]:
    """Historial de una cita (programada/reprogramada/cancelada) (HU-CAL-03)."""
    from pack_calendar.models import AppointmentEvent

    async with get_session() as session:
        rows = (await session.execute(
            select(AppointmentEvent).where(
                AppointmentEvent.tenant_id == principal.tenant_id,
                AppointmentEvent.appointment_id == appointment_id,
            ).order_by(AppointmentEvent.created_at.desc()).limit(100)
        )).scalars().all()
    return [
        ActivityOut(
            action=e.action, title=e.title, actor=e.actor,
            old_start_at=e.old_start_at, new_start_at=e.new_start_at, created_at=e.created_at,
        )
        for e in rows
    ]


class BlockPatch(BaseModel):
    start_at: datetime | None = None
    end_at: datetime | None = None
    reason: str | None = None


@router.patch("/blocks/{block_id}", response_model=BlockOut)
async def patch_block(
    block_id: uuid.UUID,
    payload: BlockPatch,
    principal: Annotated[Principal, Depends(require("pack_calendar:bloqueos", write=True))],
) -> BlockOut:
    """Edita un bloqueo de agenda (arrastrar/redimensionar en la grilla, HU-CAL-03)."""
    from core.db.models import utcnow
    from pack_calendar.models import CalendarBlock

    async with get_session() as session:
        row = await get_owned(session, CalendarBlock, block_id, principal.tenant_id)
        if row is None:
            raise ApiError.not_found("Bloqueo no encontrado.")
        new_start = payload.start_at or row.start_at
        new_end = payload.end_at or row.end_at
        if new_end <= new_start:
            raise ApiError.bad_request("El fin debe ser posterior al inicio.", "invalid_range")
        row.start_at = new_start
        row.end_at = new_end
        if payload.reason is not None:
            row.reason = payload.reason.strip()
        row.updated_at = utcnow()
        return BlockOut(
            id=row.id,
            start_at=row.start_at,
            end_at=row.end_at,
            reason=row.reason,
            calendar_id=row.calendar_id,
        )


def _panel_base() -> str:
    from core.config import get_settings

    base = (get_settings().panel_base_url or get_settings().public_base_url).rstrip("/")
    return base

async def _calendar_config() -> Any:
    """Config del pack (None si no está configurado — p. ej. admin master)."""
    from core.plugin_manager import plugin_manager

    try:
        return await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        return None


async def _timezone_name() -> str:
    """Zona horaria del negocio (fallback `America/Bogota`)."""
    config = await _calendar_config()
    return getattr(config, "timezone", None) or "America/Bogota"


# Cookies del flujo OAuth (HU-CAL-07): el `state` anti-CSRF y el destino de vuelta.
_OAUTH_STATE_COOKIE = "oauth_state"
_OAUTH_NEXT_COOKIE = "oauth_next"
_OAUTH_CONN_COOKIE = "oauth_conn"  # reconectar una conexión concreta (HU-CAL-08)
_OAUTH_COOKIE_MAX_AGE = 600


def _con_query(destino: str, **params: str) -> str:
    """`destino` con parámetros agregados, respetando la query que ya traiga (`?a=1` → `?a=1&b=2`)."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    partes = urlsplit(destino)
    query = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True) if k not in params]
    query.extend((k, v) for k, v in params.items() if v != "")
    return urlunsplit(("", "", partes.path, urlencode(query), partes.fragment))


@router.get("/oauth/start", status_code=302)
async def oauth_start(
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion", write=True))],
    next: str = "/calendario",
    connection_id: uuid.UUID | None = None,
) -> Response:
    """Inicia el OAuth2 de Google (302 a accounts.google.com).

    Con `connection_id` se reconecta esa cuenta (HU-CAL-08): se sugiere su correo a Google y
    el callback verifica que sea la misma cuenta antes de guardar.

    Deja dos cookies httponly que el panel reenvía al navegador: `oauth_state`
    (anti-CSRF, D-SPEC-24-12) y `oauth_next` (a dónde volver). El retorno de Google
    entra por la ruta BFF del panel (D-SPEC-24-11, ADR-0093).

    El `state` lleva el negocio que inició el flujo (`<tenant_id>.<aleatorio>`): el
    callback guarda la cuenta en el negocio de la sesión, que puede haber cambiado
    mientras el usuario estaba en Google.
    """
    import secrets

    from fastapi.responses import RedirectResponse

    from pack_calendar import oauth
    from pack_calendar.oauth import build_authorization_url, panel_redirect_uri

    config = await _calendar_config()
    if config is None or not (getattr(config, "client_id", "") or "").strip():
        raise ApiError.bad_request(
            "Configura las credenciales de Google del pack de Calendario antes de conectar.",
            "not_configured",
        )
    destino = next if next.startswith("/") and not next.startswith("//") else "/calendario"
    pista = ""
    if connection_id is not None:
        previos = await oauth._connection_tokens(connection_id)
        if previos is None:
            raise ApiError.not_found("La conexión de Google no existe.")
        pista = str(previos.get("account_email", "") or "")
    state = f"{principal.tenant_id}.{secrets.token_urlsafe(32)}"
    respuesta = RedirectResponse(
        build_authorization_url(
            config.client_id, redirect=panel_redirect_uri(), state=state, login_hint=pista
        ),
        status_code=302,
    )
    cookies = [(_OAUTH_STATE_COOKIE, state), (_OAUTH_NEXT_COOKIE, destino)]
    if connection_id is not None:
        cookies.append((_OAUTH_CONN_COOKIE, str(connection_id)))
    for nombre, valor in cookies:
        respuesta.set_cookie(
            nombre, valor, max_age=_OAUTH_COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/"
        )
    return respuesta


@router.get("/oauth/callback", status_code=302)
async def oauth_callback(
    request: Request,
    principal: Annotated[Principal, Depends(require("pack_calendar:conexion", write=True))],
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    """Vuelve de Google (vía la ruta BFF del panel): valida el `state`, intercambia
    el código, guarda los tokens en la conexión del negocio y regresa al panel.

    Exige sesión (el BFF reenvía la del usuario): así el negocio es el del
    principal y no se puede guardar una cuenta en otro (HU-CAL-07). Sin `state`
    o con un `state` distinto al de la cookie, no se intercambia nada (CSRF).
    """
    import hmac

    from fastapi.responses import RedirectResponse

    from core.shared.logger import get_logger
    from pack_calendar import oauth

    log = get_logger(__name__)
    marcador = request.cookies.get(_OAUTH_NEXT_COOKIE) or ""
    destino = marcador if marcador.startswith("/") and not marcador.startswith("//") else "/calendario"

    def volver(estado: str, **extra: str) -> RedirectResponse:
        respuesta = RedirectResponse(
            f"{_panel_base()}{_con_query(destino, google=estado, **extra)}", status_code=302
        )
        for nombre in (_OAUTH_STATE_COOKIE, _OAUTH_NEXT_COOKIE, _OAUTH_CONN_COOKIE):
            respuesta.delete_cookie(nombre, path="/")
        return respuesta

    esperado = request.cookies.get(_OAUTH_STATE_COOKIE) or ""
    if not esperado or not state or not hmac.compare_digest(esperado, state):
        log.warning("calendar_oauth_state_invalido", tenant_id=str(principal.tenant_id))
        return volver("error")
    if state.split(".", 1)[0] != str(principal.tenant_id):
        # Validado contra la cookie: el flujo es legítimo pero de otro negocio.
        log.warning("calendar_oauth_otro_negocio", tenant_id=str(principal.tenant_id))
        return volver("otro_negocio")
    if error or not code:
        return volver("cancelado")
    config = await _calendar_config()
    if config is None:
        return volver("error")
    try:
        tokens = await oauth.exchange_code(
            config.client_id,
            config.client_secret.get_secret_value(),
            code,
            redirect=oauth.panel_redirect_uri(),
        )
        reconectar = None
        marca = request.cookies.get(_OAUTH_CONN_COOKIE) or ""
        if marca:
            try:
                reconectar = uuid.UUID(marca)
            except ValueError:
                return volver("error")
        conexion_id = await oauth.save_tokens(tokens, connection_id=reconectar)
        agenda_id = ""
        if conexion_id is not None:
            from pack_calendar import agendas

            email = str((await oauth._connection_tokens(conexion_id) or {}).get("account_email", "") or "")
            agenda, _ = await agendas.vincular_conexion(conexion_id, email, config)
            agenda_id = str(agenda.id)
    except oauth.OtraCuentaError:
        log.warning("calendar_oauth_otra_cuenta", tenant_id=str(principal.tenant_id))
        return volver("otra_cuenta")
    except Exception:
        log.exception("calendar_oauth_exchange_failed")
        return volver("error")
    return volver("ok", conexion=str(conexion_id or ""), agenda=agenda_id)


# ---------------------------------------------------------------- asistencia (HU-SH-02)
class AsistenciaIn(BaseModel):
    asistio: bool


@router.post("/appointments/{appointment_id}/asistencia", response_model=AppointmentOut)
async def marcar_asistencia(
    appointment_id: uuid.UUID,
    payload: AsistenciaIn,
    principal: Annotated[Principal, Depends(require("pack_calendar:citas", write=True))],
) -> AppointmentOut:
    """Marca una cita pasada `asistio` o `no_asistio`. Al `no_asistio`, el asistente le
    ofrece reagendar al cliente (plantilla si la ventana está cerrada)."""
    from core.db.models import utcnow
    from core.events.outbox import emit_domain
    from core.plugin_manager import plugin_manager
    from core.shared.events import ON_APPOINTMENT_NO_SHOW
    from pack_calendar.admin import PLUGIN_ID
    from pack_calendar.config import CalendarConfig
    from pack_calendar.demo import datos_de_cita, enlace_de

    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        config = None
    config = config if isinstance(config, CalendarConfig) else CalendarConfig()
    async with get_session() as session:
        row = await get_owned(session, Appointment, appointment_id, principal.tenant_id)
        if row is None or row.deleted_at is not None:
            raise ApiError.not_found("Cita no encontrada.")
        if row.status not in ("scheduled", "asistio", "no_asistio"):
            raise ApiError.bad_request("La cita no está programada.", "cita_no_programada")
        if row.start_at > utcnow():
            raise ApiError.bad_request("La asistencia se marca después de la hora de la cita.", "cita_futura")
        nuevo = "asistio" if payload.asistio else "no_asistio"
        cambio = row.status != nuevo
        row.status = nuevo
        row.updated_at = utcnow()
        if cambio and nuevo == "no_asistio" and row.customer_id is not None:
            tipo = (
                await get_owned(session, AppointmentType, row.appointment_type_id, row.tenant_id)
                if row.appointment_type_id
                else None
            )
            await emit_domain(
                session,
                tenant_id=row.tenant_id,
                topic=ON_APPOINTMENT_NO_SHOW,
                dedupe_key=f"{ON_APPOINTMENT_NO_SHOW}:{row.id}",
                payload={
                    "appointment_id": str(row.id),
                    "customer_id": str(row.customer_id),
                    **datos_de_cita(row.title, row.start_at, row.timezone, enlace_de(tipo, config)),
                    "texto": config.no_asistio_texto,
                },
            )
        return AppointmentOut(
            id=row.id, title=row.title, attendee_email=row.attendee_email,
            start_at=row.start_at, end_at=row.end_at, status=row.status, notes=row.notes,
            google_event_id=row.google_event_id, appointment_type_id=row.appointment_type_id,
            created_by=row.created_by, timezone=row.timezone,
            customer_id=row.customer_id, asignado_admin_id=row.asignado_admin_id,
        )
