"""Agendas del negocio (HU-CAL-08, ADR-0170).

Una agenda es lo que el asistente le ofrece al cliente: tiene un nombre visible, un horario
y una zona (opcionales: heredan los de `CalendarConfig`) y, si es de Google, una conexión
(una conexión de Google = una agenda). Este módulo es la puerta única para leerlas:

- `ensure_default()` garantiza que el negocio tenga su agenda principal (idempotente) y, en
  modo google, la vincula a la conexión activa si todavía no lo estaba.
- `listar()` / `resolver()` devuelven `Agenda` (datos inmutables, no filas ORM) con un caché
  corto por negocio: el predicado de herramientas se evalúa por turno.
- `multi_activo()` decide si el asistente ofrece elegir agenda: la bandera
  `multi_agenda_modo` y al menos 2 agendas con fuente disponible.
- `config_de()` devuelve una copia de la config con la zona, el horario y el calendario de
  Google de la agenda, para que `_open_window`/`_free_slots`/`_busy_for_day` funcionen igual.

Todo lo que viene de la base (nombre, descripción, alias) es dato del negocio, nunca
instrucción (invariante 7).
"""

from __future__ import annotations

import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pack_calendar.config import CalendarConfig
from pack_calendar.models import COLORES_TIPO, Calendar

log = structlog.get_logger()

CONNECTION_TYPE = "google_calendar"
NOMBRE_DEFECTO = "Agenda principal"
NOMBRE_MAX = 60
_CACHE_TTL_SECONDS = 60.0
_NIL = uuid.UUID(int=0)


@dataclass(frozen=True)
class Agenda:
    """Agenda del negocio (copia inmutable de una fila de `pack_calendar_calendars`)."""

    id: uuid.UUID
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    color: str = "cat-1"
    timezone: str | None = None
    business_hours: str | None = None
    connection_id: uuid.UUID | None = None
    google_calendar_id: str = "primary"
    responsable_admin_id: uuid.UUID | None = None
    is_default: bool = False
    active: bool = True
    sort_order: int = 0
    # Estado de la conexión de Google (None si no es de Google). Lo llena `listar()`.
    conexion_activa: bool | None = field(default=None, compare=False)

    @property
    def es_google(self) -> bool:
        return self.connection_id is not None


def normalizar(texto: str) -> str:
    """Minúsculas, sin acentos ni espacios repetidos: para comparar nombres y alias."""
    base = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(base.lower().split())


def limpiar_nombre(texto: str) -> str:
    """Nombre visible saneado: una línea, sin caracteres de control, máximo 60."""
    sin_control = "".join(
        " " if unicodedata.category(c)[0] == "C" else c for c in str(texto or "")
    )
    return " ".join(sin_control.split())[:NOMBRE_MAX].strip()


def limpiar_aliases(valores: Any) -> list[str]:
    """Aliases saneados, sin repetidos (por forma normalizada) y como máximo 8."""
    vistos: set[str] = set()
    salida: list[str] = []
    for valor in valores or []:
        alias = limpiar_nombre(str(valor))
        clave = normalizar(alias)
        if alias and clave not in vistos:
            vistos.add(clave)
            salida.append(alias)
    return salida[:8]


def nombre_desde_email(email: str, ocupados: set[str]) -> str:
    """Nombre por defecto de la agenda de una cuenta de Google nueva (determinista).

    La parte local del correo («ana.perez@x.com» → «Ana Perez»); si ya existe, se agrega un
    número. El dueño puede renombrarla desde la ficha de la conexión; el alta nunca falla por
    un nombre repetido.
    """
    local = (email or "").split("@", 1)[0]
    base = limpiar_nombre(" ".join(p.capitalize() for p in local.replace("_", ".").replace("-", ".").split(".") if p))
    base = base or "Agenda de Google"
    candidato = base
    n = 2
    ocupados_norm = {normalizar(o) for o in ocupados}
    while normalizar(candidato) in ocupados_norm:
        sufijo = f" {n}"
        candidato = f"{base[: NOMBRE_MAX - len(sufijo)]}{sufijo}"
        n += 1
    return candidato


def color_siguiente(usados: list[str]) -> str:
    """Primer color de la paleta categórica que menos se use (rota cuando se acaban)."""
    conteo = {c: usados.count(c) for c in COLORES_TIPO}
    return min(COLORES_TIPO, key=lambda c: (conteo[c], COLORES_TIPO.index(c)))


def _a_agenda(row: Calendar, conexion_activa: bool | None) -> Agenda:
    return Agenda(
        id=row.id,
        name=row.name,
        description=row.description or "",
        aliases=tuple(row.aliases or ()),
        color=row.color,
        timezone=row.timezone,
        business_hours=row.business_hours,
        connection_id=row.connection_id,
        google_calendar_id=row.google_calendar_id or "primary",
        responsable_admin_id=row.responsable_admin_id,
        is_default=bool(row.is_default),
        active=bool(row.active),
        sort_order=int(row.sort_order or 0),
        conexion_activa=conexion_activa,
    )


# --------------------------------------------------------------------------- caché por negocio
_cache: dict[uuid.UUID, tuple[float, list[Agenda]]] = {}


def invalidar(tenant_id: uuid.UUID | None = None) -> None:
    """Descarta el caché (tras escribir una agenda). Sin id, de todos los negocios."""
    if tenant_id is None:
        _cache.clear()
    else:
        _cache.pop(tenant_id, None)


async def _conexiones_google_activas(session: Any, tenant_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """`{connection_id: label}` de las conexiones de Google activas del negocio."""
    from core.db.models import ChannelConnection

    rows = await session.execute(
        select(ChannelConnection.id, ChannelConnection.external_id).where(
            ChannelConnection.tenant_id == tenant_id,
            ChannelConnection.type_id == CONNECTION_TYPE,
            ChannelConnection.active.is_(True),
            ChannelConnection.deleted_at.is_(None),
        )
    )
    return {r.id: (r.external_id or "") for r in rows}


async def ensure_default(config: CalendarConfig | None = None) -> Agenda:
    """Agenda principal del negocio actual: la crea si falta y la vincula a Google si aplica.

    Idempotente y tolerante a carreras (el índice único parcial de «una por defecto»).
    La migración 0006 no puede leer `CalendarConfig` (está cifrada), por eso la vinculación
    con la cuenta de Google de los negocios que ya estaban en modo google se hace aquí, la
    primera vez que alguien lee las agendas.
    """
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        raise RuntimeError("agendas.ensure_default sin tenant_scope")
    for intento in (1, 2):
        try:
            async with get_session() as session:
                fila = (
                    await session.execute(
                        select(Calendar).where(
                            Calendar.tenant_id == tenant_id,
                            Calendar.is_default.is_(True),
                            Calendar.deleted_at.is_(None),
                        )
                    )
                ).scalar_one_or_none()
                if fila is None:
                    fila = Calendar(tenant_id=tenant_id, name=NOMBRE_DEFECTO, is_default=True)
                    session.add(fila)
                    await session.flush()
                if fila.connection_id is None and config is not None and config.source == "google":
                    activas = await _conexiones_google_activas(session, tenant_id)
                    ya_enlazadas = set(
                        (
                            await session.execute(
                                select(Calendar.connection_id).where(
                                    Calendar.tenant_id == tenant_id,
                                    Calendar.connection_id.is_not(None),
                                    Calendar.deleted_at.is_(None),
                                )
                            )
                        ).scalars()
                    )
                    libres = [cid for cid in activas if cid not in ya_enlazadas]
                    if libres:
                        # La primaria primero (mismo orden que `get_active_connection`).
                        primaria = (
                            await session.execute(
                                select(ChannelConnection.id)
                                .where(ChannelConnection.id.in_(libres))
                                .order_by(
                                    ChannelConnection.is_primary.desc(),
                                    ChannelConnection.created_at.desc(),
                                )
                                .limit(1)
                            )
                        ).scalar_one()
                        fila.connection_id = primaria
                        fila.google_calendar_id = (config.calendar_id or "primary") or "primary"
                invalidar(tenant_id)
                return _a_agenda(fila, None)
        except IntegrityError:
            if intento == 2:
                raise
            log.info("calendar_default_race", tenant_id=str(tenant_id))
    raise RuntimeError("inalcanzable")  # pragma: no cover


async def listar(config: CalendarConfig | None = None, *, solo_activas: bool = False) -> list[Agenda]:
    """Agendas del negocio actual en orden (principal primero), con el estado de su conexión."""
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    ahora = time.monotonic()
    cacheadas = _cache.get(tenant_id) if tenant_id is not None else None
    if cacheadas is not None and ahora - cacheadas[0] < _CACHE_TTL_SECONDS:
        agendas = cacheadas[1]
    else:
        await ensure_default(config)
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(Calendar)
                    .where(Calendar.tenant_id == tenant_id, Calendar.deleted_at.is_(None))
                    .order_by(Calendar.is_default.desc(), Calendar.sort_order, Calendar.created_at)
                )
            ).scalars().all()
            activas = await _conexiones_google_activas(session, tenant_id)
            agendas = [
                _a_agenda(f, (f.connection_id in activas) if f.connection_id is not None else None)
                for f in filas
            ]
        if tenant_id is not None:
            _cache[tenant_id] = (ahora, agendas)
    return [a for a in agendas if a.active] if solo_activas else list(agendas)


def disponible(agenda: Agenda, config: CalendarConfig) -> bool:
    """¿La agenda puede atender ahora? (activa y con su fuente lista).

    - De Google: su conexión debe estar activa.
    - Interna: siempre, salvo la agenda principal sin vincular en un negocio en modo google
      (todavía no tiene cuenta conectada).
    """
    if not agenda.active:
        return False
    if agenda.es_google:
        return bool(agenda.conexion_activa)
    return not (agenda.is_default and config.source == "google")


def disponibles(agendas: list[Agenda], config: CalendarConfig) -> list[Agenda]:
    return [a for a in agendas if disponible(a, config)]


def multi_activo(config: CalendarConfig, agendas: list[Agenda]) -> bool:
    """El asistente ofrece elegir agenda: bandera encendida y 2 o más agendas disponibles."""
    if getattr(config, "multi_agenda_modo", "cliente_elige") != "cliente_elige":
        return False
    return len(disponibles(agendas, config)) >= 2


def resolver(ref: Any, agendas: list[Agenda]) -> tuple[Agenda | None, list[Agenda]]:
    """Agenda a la que se refiere `ref` (id, nombre o alias) y, si es ambigua, los candidatos.

    Devuelve `(agenda, [])` si hay una sola coincidencia, `(None, candidatos)` si hay varias y
    `(None, [])` si ninguna. Un nombre exacto gana sobre un alias.
    """
    texto = str(ref or "").strip()
    if not texto:
        return None, []
    try:
        ident = uuid.UUID(texto)
    except ValueError:
        ident = None
    if ident is not None:
        for a in agendas:
            if a.id == ident:
                return a, []
    clave = normalizar(texto)
    exactas = [a for a in agendas if normalizar(a.name) == clave]
    if len(exactas) == 1:
        return exactas[0], []
    por_alias = [a for a in agendas if clave in {normalizar(x) for x in a.aliases}]
    if len(por_alias) == 1:
        return por_alias[0], []
    if por_alias:
        return None, por_alias
    # Coincidencia parcial («doctora ana» ⊂ «Dra. Ana Pérez») solo si es única.
    parciales = [a for a in agendas if clave and (clave in normalizar(a.name) or normalizar(a.name) in clave)]
    if len(parciales) == 1:
        return parciales[0], []
    return None, parciales


def por_id(agenda_id: uuid.UUID | None, agendas: list[Agenda]) -> Agenda | None:
    """Agenda por id; `None` (citas viejas sin `calendar_id`) es la principal."""
    if agenda_id is None:
        return next((a for a in agendas if a.is_default), agendas[0] if agendas else None)
    return next((a for a in agendas if a.id == agenda_id), None)


def config_de(agenda: Agenda | None, config: CalendarConfig) -> CalendarConfig:
    """Config efectiva de una agenda: su zona, horario y calendario de Google sobre la global."""
    if agenda is None:
        return config
    cambios: dict[str, Any] = {}
    if agenda.timezone:
        cambios["timezone"] = agenda.timezone
    if agenda.business_hours:
        cambios["business_hours"] = agenda.business_hours
    if agenda.es_google:
        cambios["calendar_id"] = agenda.google_calendar_id or "primary"
        cambios["source"] = "google"
    elif config.source == "google":
        # Una agenda interna dentro de un negocio en modo google no usa Google.
        cambios["source"] = "interno"
    return config.model_copy(update=cambios) if cambios else config


def descripcion_para_asistente(agenda: Agenda) -> dict[str, Any]:
    """Forma que ve el modelo: solo texto del negocio, sin ids internos más que el de la agenda."""
    return {
        "calendar_id": str(agenda.id),
        "nombre": agenda.name,
        "descripcion": agenda.description,
        "aliases": list(agenda.aliases),
    }
