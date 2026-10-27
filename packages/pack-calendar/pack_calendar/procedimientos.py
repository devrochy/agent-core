"""Procedimiento `pack_calendar.agendar_cita` y sus *resolvers* (HU-AOS-08, ADR-0113 §1).

El JSON del procedimiento vive en `packages/pack-calendar/procedures/`. Los resolvers validan los
datos que escribe el modelo con `actualizar_tarea`, con la agenda real del servidor:

- `horario_ofrecido`: un horario es "ofrecido" si está entre los horarios libres que el servidor
  ofrece ese día, con la misma cuenta que `check_availability` (`_free_slots`: ventana de atención −
  ocupados − margen, en la cuadrícula de `slot_minutes`, con la duración del tipo de cita o la
  configurada y nunca en el pasado). Si no lo está, el error lista los libres de ese día para que
  el asistente los ofrezca. Solo consulta el día pedido: corre con la conversación bloqueada y un
  presupuesto de 5 s, así que nunca recorre varios días (eso es `find_next_available`).
- `tipo_de_cita`: id o nombre de un tipo de cita activo → su id.

Reciben solo `ToolContext` (invariante 6), no tienen efectos y leen la configuración del pack,
nunca del prompt. Los mensajes de error son datos para el modelo, como los de las tools del pack.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.mcp.types import ResolverResult, ToolContext

# Horarios libres que se listan en el error (los de un día caben de sobra en una respuesta).
MAX_OFRECIDOS = 10
# Tope de la cuenta interna: un día de 24 h en bloques de 15 min.
_MAX_LIBRES_DEL_DIA = 96


def hora_ofrecida(inicio_local: datetime, libres: list[str]) -> bool:
    """¿La hora (HH:MM, en la zona del negocio) está entre los libres que ofrece el servidor?
    Un horario fuera de la cuadrícula (10:15 con bloques de 30 min) no es un horario ofrecido."""
    return inicio_local.second == 0 and inicio_local.microsecond == 0 and inicio_local.strftime("%H:%M") in libres


def mensaje_no_ofrecido(fecha_larga: str, libres: list[str]) -> str:
    if not libres:
        return (
            f"El {fecha_larga} no hay horarios libres. Consulta los próximos días con "
            "find_next_available y ofrece uno de esos."
        )
    return (
        f"Ese horario no está disponible. Horarios libres del {fecha_larga}: "
        f"{', '.join(libres[:MAX_OFRECIDOS])}. Ofrece solo uno de estos."
    )


def _parsear(valor: Any, tz: ZoneInfo) -> datetime | None:
    try:
        inicio = datetime.fromisoformat(str(valor).strip())
    except ValueError:
        return None
    return inicio.replace(tzinfo=tz) if inicio.tzinfo is None else inicio.astimezone(tz)


async def _tipo_por_id_o_nombre(valor: Any) -> Any | None:
    from sqlalchemy import func, select

    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id
    from pack_calendar.models import AppointmentType

    texto = str(valor or "").strip()
    if not texto:
        return None
    filtro = [
        AppointmentType.tenant_id == get_current_tenant_id(),
        AppointmentType.active.is_(True),
        AppointmentType.deleted_at.is_(None),
    ]
    try:
        filtro.append(AppointmentType.id == uuid.UUID(texto))
    except ValueError:
        filtro.append(func.lower(AppointmentType.name) == texto.lower()[:200])
    async with get_session() as session:
        return (await session.execute(select(AppointmentType).where(*filtro).limit(1))).scalars().first()


async def tipo_de_cita(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    tipo = await _tipo_por_id_o_nombre(valor)
    if tipo is None:
        return ResolverResult(
            ok=False,
            error="Ese tipo de cita no existe o no está activo: consulta los tipos con list_appointment_types.",
        )
    return ResolverResult(ok=True, value=str(tipo.id))


async def horario_ofrecido(valor: Any, ctx: ToolContext, datos: dict[str, Any]) -> ResolverResult:
    """El horario es uno de los libres que el servidor ofrece ese día (ver el docstring del módulo)."""
    from pack_calendar import agendas
    from pack_calendar.skills import CalendarSkill, _free_slots, _long_date, _open_window

    skill = CalendarSkill()
    config = await skill._tenant_config()
    tz = ZoneInfo(config.timezone)
    inicio = _parsear(valor, tz)
    if inicio is None:
        return ResolverResult(ok=False, error="Fecha y hora inválidas: usa el formato 2026-10-15T10:00:00.")
    ahora = datetime.now(tz)
    if inicio <= ahora:
        return ResolverResult(
            ok=False,
            error=f"Ese horario ya pasó; hoy es {_long_date(ahora.date())}. Consulta los próximos con find_next_available.",
        )
    tipo = await _tipo_por_id_o_nombre(datos.get("tipo_cita")) if datos.get("tipo_cita") else None
    duracion = int(getattr(tipo, "duration_minutes", 0) or 0) or config.event_duration_minutes
    dia = inicio.date().isoformat()
    not_before = ahora if inicio.date() == ahora.date() else None

    async def _libres(cfg: Any, agenda: Any = None) -> list[str]:
        _ocupados, intervalos = await skill._busy_for_day(cfg, cfg.source, dia, tz, ctx, agenda)
        return _free_slots(
            _open_window(cfg, dia),
            intervalos,
            tz,
            dia,
            slot_minutes=cfg.slot_minutes,
            duration_minutes=duracion,
            buffer_minutes=cfg.buffer_minutes,
            limit=_MAX_LIBRES_DEL_DIA,
            not_before=not_before,
        )

    lista, multi = await skill._agendas(config)
    if multi:
        # HU-CAL-08: con varias agendas el horario se ofrece si hay hueco en alguna; la agenda se
        # elige después (create_calendar_event la exige) y valida el solape en ESA agenda.
        libres = sorted(
            {h for a in agendas.disponibles(lista, config) for h in await _libres(agendas.config_de(a, config), a)}
        )
    else:
        libres = await _libres(config)
    if not hora_ofrecida(inicio, libres):
        return ResolverResult(ok=False, error=mensaje_no_ofrecido(_long_date(inicio.date()), libres))
    return ResolverResult(ok=True, value=inicio.isoformat())


def registrar(host: Any, plugin_id: str) -> None:
    """Publica el procedimiento y sus resolvers (lo llama `on_activate`). Sin banderas encendidas
    nadie los usa: el motor solo actúa con `tenants.aos_procedures` (o en sombra con `aos_runtime = v3`)."""
    from core.kernel.procedures import de_pack

    host.register_resolver(f"{plugin_id}.tipo_de_cita", validar=tipo_de_cita)
    host.register_resolver(f"{plugin_id}.horario_ofrecido", validar=horario_ofrecido)
    host.register_procedures(de_pack.cargar(plugin_id))
