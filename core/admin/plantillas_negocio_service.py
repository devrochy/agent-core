"""Plantillas del negocio en todas sus conexiones de WhatsApp (HU-CONN-21).

WhatsApp aprueba cada plantilla por cuenta: la de la cuenta de Meta no existe en la de Twilio. La «plantilla del
negocio» es la clave `(nombre, idioma)`, la misma con que los avisos por evento eligen plantilla en la conexión de
cada conversación (ADR-0108, `event_notices`). Aquí no hay tabla nueva: se leen `message_templates` (Meta, por
WABA) y `twilio_content_templates` (Twilio, por conexión) y se copia con los creadores que ya tiene cada proveedor.

- **Conexiones.** Meta: la cuenta (`meta_connections`) con su WABA viva; la pausa es la de ADR-0122
  (`motivo_pausa`). Twilio: la conexión (`channel_connections`); pausada si `active` es falso. El Sandbox de Twilio
  nunca es destino ni cuenta para «aviso que falta»: solo sirve las plantillas preaprobadas de Twilio (HU-CONN-20).
- **Qué se copia.** Hacia Twilio solo texto (ADR-0186 §8): una de Meta con encabezado, pie o botones, con variables
  con nombre (el mapa de avisos por nombre no casaría con `{{1}}`) o de autenticación queda «no replicable». De
  Meta a Meta van los componentes completos, salvo un encabezado con multimedia (su archivo se sube en cada cuenta).
  De Twilio a Meta el texto pasa a un componente BODY con sus ejemplos.
- **No pisar.** Si el destino ya tiene ese nombre e idioma con el mismo texto, «ya existía»; con otro texto,
  «distinta» y no se toca. En Twilio el nombre es único por conexión sin idioma: el mismo nombre en otro idioma
  queda «no replicable».
- **Fallas por destino.** Cada destino es independiente; la respuesta trae el resultado de cada uno. Repetir la copia
  no duplica (los índices únicos de cada proveedor) y retoma lo que quedó a medias (una de Twilio creada sin
  enviar a aprobación se envía).
- Nunca hay una sesión de base abierta mientras se llama a Meta o a Twilio.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry import metrics
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.db.models import (
    ChannelConnection,
    MessageTemplate,
    MetaConnection,
    PhoneNumber,
    TwilioConnection,
    TwilioContentTemplate,
    WabaAccount,
)
from core.db.session import get_session
from core.gateway.meta_connection_state import motivo_pausa
from core.gateway.twilio import alta, sandbox
from core.gateway.twilio import plantillas as twilio_plantillas
from core.shared.logger import get_logger

log = get_logger(__name__)

Proveedor = Literal["meta", "twilio"]
PROVEEDORES: tuple[str, ...] = ("meta", "twilio")

# Resultados de una copia por destino (etiquetas cerradas del contador).
CREADA, YA_EXISTIA, DISTINTA, NO_REPLICABLE, ERROR = (
    "creada",
    "ya_existia",
    "distinta",
    "no_replicable",
    "error",
)
RESULTADOS = (CREADA, YA_EXISTIA, DISTINTA, NO_REPLICABLE, ERROR)

# Estado de una plantilla en una conexión, igual para los dos proveedores.
APROBADA, PENDIENTE, SIN_ENVIAR, RECHAZADA, PAUSADA, FALTA = (
    "aprobada",
    "pendiente",
    "sin_enviar",
    "rechazada",
    "pausada",
    "falta",
)

# «Copiar todas» es síncrona: tope de copias con llamadas al proveedor por pedido; el panel repite con `restantes`.
LIMITE_COPIAR_TODAS = 10

_META_AUSENTE = frozenset({"DELETED", "PENDING_DELETION", "ARCHIVED"})
_META_ESTADO = {
    "APPROVED": APROBADA,
    "REINSTATED": APROBADA,
    "UNARCHIVED": APROBADA,
    "PENDING": PENDIENTE,
    "IN_APPEAL": PENDIENTE,
    "DRAFT": SIN_ENVIAR,
    "REJECTED": RECHAZADA,
}
_TWILIO_ESTADO = {
    "approved": APROBADA,
    "received": PENDIENTE,
    "pending": PENDIENTE,
    "creating": SIN_ENVIAR,
    "unsubmitted": SIN_ENVIAR,
    "rejected": RECHAZADA,
}
_MULTIMEDIA = frozenset({"IMAGE", "VIDEO", "DOCUMENT", "LOCATION"})
_POSICIONAL = re.compile(r"\{\{(\d+)\}\}")

_meter = metrics.get_meter("agent-core")
_replicadas = _meter.create_counter(
    "agentcore_plantillas_replicadas_total",
    description="Copias de plantillas entre conexiones de WhatsApp del negocio (HU-CONN-21), por resultado",
)


class PlantillasNegocioError(Exception):
    """Pedido inválido para todo el comando (no por destino): `code` estable y mensaje en español."""

    def __init__(self, http_status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.http_status, self.code, self.message = http_status, code, message


# --------------------------------------------------------------------------- modelo de lectura


@dataclass(frozen=True)
class Conexion:
    proveedor: str
    id: uuid.UUID
    etiqueta: str
    activa: bool
    motivo_pausa: str | None
    sandbox: bool
    # Meta: id externo de la WABA, para no copiar dos veces a la misma cuenta colgada de dos conexiones.
    waba_id: str | None = None

    @property
    def clave(self) -> str:
        return f"{self.proveedor}:{self.id}"

    @property
    def destinable(self) -> bool:
        return self.activa and not self.sandbox


@dataclass(frozen=True)
class Fuente:
    """Una plantilla en una conexión, en forma común."""

    proveedor: str
    conexion_id: uuid.UUID
    plantilla_id: uuid.UUID
    nombre: str
    idioma: str
    categoria: str
    cuerpo: str
    ejemplos: dict[str, str]
    estado: str
    estado_proveedor: str
    motivo_rechazo: str
    # Meta: componentes tal como los guarda el sync. Twilio: vacío.
    componentes: list[dict[str, Any]] = field(default_factory=list)
    parameter_format: str | None = None
    # Twilio: ¿solo `twilio/text`? (las creadas en su consola pueden tener otros tipos).
    solo_texto: bool = True
    content_sid: str | None = None

    @property
    def clave(self) -> str:
        return f"{self.proveedor}:{self.conexion_id}"


def _texto(valor: Any) -> str:
    return str(valor or "").strip()


def _cuerpo_meta(componentes: Any) -> tuple[str, dict[str, str]]:
    for c in componentes or []:
        if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY":
            texto = str(c.get("text") or "")
            ejemplo = ((c.get("example") or {}).get("body_text") or [[]])[0] or []
            return texto, {str(i + 1): str(v) for i, v in enumerate(ejemplo)}
    return "", {}


def _fuente_meta(t: MessageTemplate, conexion_id: uuid.UUID) -> Fuente:
    cuerpo, ejemplos = _cuerpo_meta(t.components)
    return Fuente(
        proveedor="meta",
        conexion_id=conexion_id,
        plantilla_id=t.id,
        nombre=t.name,
        idioma=t.language,
        categoria=(t.category or "UTILITY").upper(),
        cuerpo=cuerpo,
        ejemplos=ejemplos,
        estado=_META_ESTADO.get(t.status, PAUSADA),
        estado_proveedor=t.status,
        motivo_rechazo=t.rejected_reason or "",
        componentes=[c for c in (t.components or []) if isinstance(c, dict)],
        parameter_format=(t.parameter_format or "").upper() or None,
    )


def _fuente_twilio(t: TwilioContentTemplate) -> Fuente:
    return Fuente(
        proveedor="twilio",
        conexion_id=t.channel_connection_id,
        plantilla_id=t.id,
        nombre=t.friendly_name,
        idioma=t.language,
        categoria=(t.category or "UTILITY").upper(),
        cuerpo=t.body or "",
        ejemplos={str(k): str(v) for k, v in (t.variables or {}).items()},
        estado=_TWILIO_ESTADO.get(t.approval_status, PAUSADA),
        estado_proveedor=t.approval_status,
        motivo_rechazo=t.rejection_reason or "",
        solo_texto=set((t.types or {}).keys()) <= {"twilio/text"},
        content_sid=t.content_sid,
    )


async def conexiones(tenant_id: uuid.UUID) -> list[Conexion]:
    """Conexiones de WhatsApp del negocio con plantillas: cuentas de Meta con WABA viva y conexiones de Twilio."""
    alta._exigir_scope(tenant_id)
    salida: list[Conexion] = []
    async with get_session() as session:
        metas = (
            await session.execute(
                select(MetaConnection, WabaAccount)
                .join(WabaAccount, WabaAccount.connection_id == MetaConnection.id)
                .where(
                    MetaConnection.tenant_id == tenant_id,
                    MetaConnection.channel == "whatsapp",
                    MetaConnection.deleted_at.is_(None),
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.deleted_at.is_(None),
                )
                .order_by(MetaConnection.created_at, WabaAccount.created_at)
            )
        ).all()
        numeros: dict[uuid.UUID, str] = {}
        for conn_id, numero in (
            await session.execute(
                select(PhoneNumber.connection_id, PhoneNumber.display_phone_number)
                .where(PhoneNumber.tenant_id == tenant_id, PhoneNumber.deleted_at.is_(None))
                .order_by(PhoneNumber.created_at)
            )
        ).all():
            if numero:
                numeros.setdefault(conn_id, numero)
        twilios = (
            await session.execute(
                select(TwilioConnection, ChannelConnection)
                .join(ChannelConnection, ChannelConnection.id == TwilioConnection.channel_connection_id)
                .where(
                    TwilioConnection.tenant_id == tenant_id,
                    TwilioConnection.deleted_at.is_(None),
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                )
                .order_by(ChannelConnection.created_at)
            )
        ).all()
    vistas: set[uuid.UUID] = set()
    for mc, waba in metas:
        if mc.id in vistas:  # una conexión, una columna: la primera WABA viva (como `resolve_waba_and_credentials`)
            continue
        vistas.add(mc.id)
        pausa = motivo_pausa(mc)
        etiqueta = " · ".join(x for x in (waba.name, numeros.get(mc.id)) if x) or "WhatsApp (Meta)"
        salida.append(
            Conexion("meta", mc.id, etiqueta, pausa is None, pausa, False, waba_id=waba.waba_id)
        )
    for tc, cc in twilios:
        es_sandbox = tc.modo == sandbox.MODO_SANDBOX
        pausa = None if cc.active and not sandbox.inerte(tc.modo) else "La conexión está pausada"
        etiqueta = cc.label or tc.display_name or tc.sender_e164
        salida.append(Conexion("twilio", cc.id, etiqueta, pausa is None, pausa, es_sandbox))
    return salida


async def fuentes(tenant_id: uuid.UUID, conns: list[Conexion]) -> list[Fuente]:
    """Las plantillas vivas de esas conexiones, en forma común."""
    alta._exigir_scope(tenant_id)
    metas = {c.id for c in conns if c.proveedor == "meta"}
    twilios = {c.id for c in conns if c.proveedor == "twilio"}
    salida: list[Fuente] = []
    async with get_session() as session:
        if metas:
            for t, conn_id in (
                await session.execute(
                    select(MessageTemplate, WabaAccount.connection_id)
                    .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                    .where(
                        MessageTemplate.tenant_id == tenant_id,
                        WabaAccount.tenant_id == tenant_id,
                        WabaAccount.deleted_at.is_(None),
                        WabaAccount.connection_id.in_(metas),
                        MessageTemplate.status.not_in(_META_AUSENTE),
                    )
                    .order_by(MessageTemplate.name, MessageTemplate.language)
                )
            ).all():
                salida.append(_fuente_meta(t, conn_id))
        if twilios:
            for t in (
                await session.execute(
                    select(TwilioContentTemplate)
                    .where(
                        TwilioContentTemplate.tenant_id == tenant_id,
                        TwilioContentTemplate.channel_connection_id.in_(twilios),
                        TwilioContentTemplate.deleted_at.is_(None),
                    )
                    .order_by(TwilioContentTemplate.friendly_name)
                )
            ).scalars():
                salida.append(_fuente_twilio(t))
    return salida


# --------------------------------------------------------------------------- reglas de copia


def motivo_no_replicable(fuente: Fuente, destino: str) -> str | None:
    """Por qué esa plantilla no se puede llevar a una conexión de ese proveedor, o `None` si se puede."""
    if fuente.proveedor == "twilio" and not fuente.solo_texto:
        return "Solo se copian plantillas de texto: esta tiene otros tipos de contenido de Twilio."
    if fuente.proveedor != destino and fuente.categoria == "AUTHENTICATION":
        return "Las plantillas de autenticación no se copian entre Meta y Twilio: se crean en cada una."
    if not fuente.cuerpo.strip():
        return "La plantilla no tiene texto en el cuerpo."
    if fuente.proveedor == "meta":
        tipos = {str(c.get("type", "")).upper() for c in fuente.componentes}
        cabecera = next((c for c in fuente.componentes if str(c.get("type", "")).upper() == "HEADER"), None)
        if cabecera is not None and str(cabecera.get("format", "TEXT")).upper() in _MULTIMEDIA:
            return "Tiene encabezado con imagen, video o documento: ese archivo se sube en cada cuenta."
        if destino == "twilio":
            if tipos - {"BODY"}:
                return "Twilio solo usa plantillas de texto: esta tiene encabezado, pie o botones."
            if fuente.parameter_format == "NAMED":
                return "Usa variables con nombre y Twilio usa {{1}}, {{2}}…: créala en Twilio con variables numeradas."
    if destino == "twilio":
        try:
            twilio_plantillas.validar(
                fuente.nombre, fuente.idioma, fuente.categoria, fuente.cuerpo, fuente.ejemplos
            )
        except alta.AltaError as exc:
            return exc.message
    return None


def _mismo_texto(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def _componentes_meta_desde(fuente: Fuente) -> tuple[list[dict[str, Any]], str | None]:
    """Componentes para crear en Meta: los mismos si la fuente es de Meta; el cuerpo con sus ejemplos si es Twilio."""
    if fuente.proveedor == "meta":
        return [dict(c) for c in fuente.componentes], fuente.parameter_format
    cuerpo: dict[str, Any] = {"type": "BODY", "text": fuente.cuerpo}
    numeros = sorted({int(n) for n in _POSICIONAL.findall(fuente.cuerpo)})
    if numeros:
        cuerpo["example"] = {"body_text": [[fuente.ejemplos.get(str(n), "") for n in numeros]]}
    return [cuerpo], None


# --------------------------------------------------------------------------- matriz


@dataclass(frozen=True)
class Celda:
    estado: str  # aprobada | pendiente | sin_enviar | rechazada | pausada | falta | no_replicable
    distinta: bool = False
    plantilla_id: uuid.UUID | None = None
    motivo: str = ""


@dataclass(frozen=True)
class Fila:
    nombre: str
    idioma: str
    categoria: str
    cuerpo: str
    celdas: dict[str, Celda]


@dataclass(frozen=True)
class AvisoFaltante:
    evento: str
    nombre: str
    idioma: str
    conexion: str
    estado: str


@dataclass(frozen=True)
class Matriz:
    conexiones: list[Conexion]
    filas: list[Fila]
    avisos_faltantes: list[AvisoFaltante]


def _referencia(grupo: list[Fuente]) -> Fuente:
    """La que manda el texto de la fila: la primera aprobada, si no la primera."""
    return next((f for f in grupo if f.estado == APROBADA), grupo[0])


def armar_filas(conns: list[Conexion], todas: list[Fuente]) -> list[Fila]:
    grupos: dict[tuple[str, str], list[Fuente]] = {}
    for f in todas:
        grupos.setdefault((f.nombre, f.idioma), []).append(f)
    filas: list[Fila] = []
    for (nombre, idioma), grupo in sorted(grupos.items()):
        ref = _referencia(grupo)
        por_conexion = {f.clave: f for f in grupo}
        celdas: dict[str, Celda] = {}
        for c in conns:
            f = por_conexion.get(c.clave)
            if f is not None:
                celdas[c.clave] = Celda(
                    f.estado, not _mismo_texto(f.cuerpo, ref.cuerpo), f.plantilla_id, f.motivo_rechazo
                )
                continue
            motivos = [motivo_no_replicable(g, c.proveedor) for g in grupo]
            if all(m is not None for m in motivos):
                celdas[c.clave] = Celda(NO_REPLICABLE, motivo=str(motivos[0]))
            else:
                celdas[c.clave] = Celda(FALTA)
        filas.append(Fila(nombre, idioma, ref.categoria, ref.cuerpo, celdas))
    return filas


async def avisos_faltantes(
    tenant_id: uuid.UUID, conns: list[Conexion], filas: list[Fila]
) -> list[AvisoFaltante]:
    """Avisos por evento cuya plantilla no está aprobada en una conexión activa (fuera del Sandbox): por esa
    conexión el aviso no saldrá con plantilla."""
    from core.gateway.event_notices import EVENTOS, load_templates_map

    mapa = await load_templates_map(tenant_id)
    por_clave = {(f.nombre, f.idioma): f for f in filas}
    salida: list[AvisoFaltante] = []
    for evento, entrada in sorted(mapa.items()):
        if evento not in EVENTOS or not isinstance(entrada, dict):
            continue
        nombre, idioma = _texto(entrada.get("template")), _texto(entrada.get("language")) or "es"
        if not nombre:
            continue
        fila = por_clave.get((nombre, idioma))
        for c in conns:
            if not c.destinable:
                continue
            estado = fila.celdas[c.clave].estado if fila is not None else FALTA
            if estado != APROBADA:
                salida.append(AvisoFaltante(evento, nombre, idioma, c.clave, estado))
    if salida:
        log.info("avisos_plantilla_faltante", tenant_id=str(tenant_id), total=len(salida))
    return salida


async def matriz(tenant_id: uuid.UUID) -> Matriz:
    conns = await conexiones(tenant_id)
    filas = armar_filas(conns, await fuentes(tenant_id, conns))
    return Matriz(conns, filas, await avisos_faltantes(tenant_id, conns, filas))


# --------------------------------------------------------------------------- copia


@dataclass(frozen=True)
class ResultadoDestino:
    conexion: str
    resultado: str
    motivo: str = ""
    nombre: str = ""
    idioma: str = ""


def _registrar(tenant_id: uuid.UUID, origen: Fuente, destino: Conexion, r: ResultadoDestino) -> ResultadoDestino:
    log.info(
        "plantilla_replicada",
        tenant_id=str(tenant_id),
        origen=origen.clave,
        destino=destino.clave,
        proveedor=destino.proveedor,
        resultado=r.resultado,
    )
    try:
        _replicadas.add(1, {"resultado": r.resultado, "proveedor": destino.proveedor})
    except Exception as exc:
        log.warning("metrica_fallo", error_type=type(exc).__name__)
    return r


def _res(destino: Conexion, fuente: Fuente, resultado: str, motivo: str = "") -> ResultadoDestino:
    return ResultadoDestino(destino.clave, resultado, motivo, fuente.nombre, fuente.idioma)


async def _existente_twilio(tenant_id: uuid.UUID, conexion_id: uuid.UUID, nombre: str) -> Fuente | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(TwilioContentTemplate).where(
                    TwilioContentTemplate.tenant_id == tenant_id,
                    TwilioContentTemplate.channel_connection_id == conexion_id,
                    TwilioContentTemplate.friendly_name == nombre,
                    TwilioContentTemplate.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    return _fuente_twilio(fila) if fila is not None else None


async def _existente_meta(tenant_id: uuid.UUID, conexion_id: uuid.UUID, nombre: str, idioma: str) -> Fuente | None:
    async with get_session() as session:
        fila = (
            await session.execute(
                select(MessageTemplate)
                .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                .where(
                    MessageTemplate.tenant_id == tenant_id,
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.connection_id == conexion_id,
                    WabaAccount.deleted_at.is_(None),
                    MessageTemplate.name == nombre,
                    MessageTemplate.language == idioma,
                    MessageTemplate.status.not_in(_META_AUSENTE),
                )
            )
        ).scalars().first()
    return _fuente_meta(fila, conexion_id) if fila is not None else None


async def _comparar(
    tenant_id: uuid.UUID, fuente: Fuente, destino: Conexion, existente: Fuente
) -> ResultadoDestino:
    """El destino ya tiene ese nombre: igual (y retoma lo que quedó sin enviar en Twilio) o distinta."""
    if existente.idioma != fuente.idioma:
        return _res(
            destino,
            fuente,
            NO_REPLICABLE,
            f"Esa conexión ya usa el nombre «{fuente.nombre}» en otro idioma ({existente.idioma}); en Twilio el "
            "nombre no se repite entre idiomas.",
        )
    if not _mismo_texto(existente.cuerpo, fuente.cuerpo):
        return _res(destino, fuente, DISTINTA, "Ya existe con otro texto: no se modifica.")
    if destino.proveedor == "twilio" and existente.estado_proveedor in ("creating", "unsubmitted"):
        return await _crear_twilio(tenant_id, fuente, destino, reanudar=existente)
    return _res(destino, fuente, YA_EXISTIA)


async def _crear_twilio(
    tenant_id: uuid.UUID, fuente: Fuente, destino: Conexion, *, reanudar: Fuente | None = None
) -> ResultadoDestino:
    plantilla_id = reanudar.plantilla_id if reanudar is not None else uuid.uuid4()
    try:
        if reanudar is None or reanudar.content_sid is None:
            creada = await twilio_plantillas.crear(
                tenant_id,
                destino.id,
                nombre=fuente.nombre,
                idioma=fuente.idioma,
                categoria=fuente.categoria,
                cuerpo=fuente.cuerpo,
                ejemplos=fuente.ejemplos,
            )
            plantilla_id = creada.id
    except alta.AltaError as exc:
        if exc.code == "nombre_en_uso":  # otra copia la creó a la vez: la decide el índice por nombre
            existente = await _existente_twilio(tenant_id, destino.id, fuente.nombre)
            if existente is not None:
                return await _comparar(tenant_id, fuente, destino, existente)
        if exc.http_status == 422:
            return _res(destino, fuente, NO_REPLICABLE, exc.message)
        return _res(destino, fuente, ERROR, exc.message)
    try:
        await twilio_plantillas.enviar_a_aprobacion(tenant_id, destino.id, plantilla_id)
    except alta.AltaError as exc:
        return _res(
            destino,
            fuente,
            ERROR,
            f"Se creó en Twilio pero no se envió a aprobación ({exc.message}). Repite la copia para enviarla.",
        )
    return _res(destino, fuente, CREADA)


async def _crear_meta(tenant_id: uuid.UUID, fuente: Fuente, destino: Conexion) -> ResultadoDestino:
    from core.admin.meta_numbers_service import resolve_waba_and_credentials
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_templates import (
        TemplateRateLimitError,
        TemplateValidationError,
        create_template,
        find_template_by_name,
    )

    connection, waba, creds = await resolve_waba_and_credentials(destino.id, tenant_id)
    if connection is None or waba is None or creds is None:
        return _res(destino, fuente, ERROR, "La cuenta de Meta no tiene credenciales listas.")
    componentes, formato = _componentes_meta_desde(fuente)
    try:
        await create_template(
            waba.waba_id,
            creds.access_token,
            creds.app_secret,
            creds.graph_api_version,
            connection.id,
            tenant_id,
            waba.id,
            name=fuente.nombre,
            language=fuente.idioma,
            category=fuente.categoria,
            components=componentes,
            parameter_format=formato,
        )
    except TemplateValidationError as exc:
        return _res(destino, fuente, NO_REPLICABLE, str(exc)[:300])
    except TemplateRateLimitError as exc:
        return _res(destino, fuente, ERROR, str(exc)[:300])
    except IntegrityError:  # otra copia la guardó a la vez (único por WABA, nombre e idioma)
        existente = await _existente_meta(tenant_id, destino.id, fuente.nombre, fuente.idioma)
        if existente is not None:
            return await _comparar(tenant_id, fuente, destino, existente)
        return _res(destino, fuente, YA_EXISTIA)
    except GraphError as exc:
        # Meta rechaza un nombre e idioma que ya tiene aunque aquí no se haya sincronizado todavía.
        try:
            remota = await find_template_by_name(
                waba.waba_id,
                creds.access_token,
                creds.app_secret,
                creds.graph_api_version,
                connection.id,
                name=fuente.nombre,
                language=fuente.idioma,
            )
        except GraphError:
            remota = None
        if remota is not None:
            return _res(
                destino, fuente, YA_EXISTIA, "Ya está en la cuenta de Meta: sincroniza sus plantillas para ver su texto."
            )
        return _res(destino, fuente, ERROR, str(getattr(exc, "message", exc))[:300])
    return _res(destino, fuente, CREADA)


async def _copiar_a(tenant_id: uuid.UUID, fuente: Fuente, destino: Conexion) -> ResultadoDestino:
    motivo = motivo_no_replicable(fuente, destino.proveedor)
    if motivo is not None:
        return _res(destino, fuente, NO_REPLICABLE, motivo)
    if destino.proveedor == "twilio":
        existente = await _existente_twilio(tenant_id, destino.id, fuente.nombre)
    else:
        existente = await _existente_meta(tenant_id, destino.id, fuente.nombre, fuente.idioma)
    if existente is not None:
        return await _comparar(tenant_id, fuente, destino, existente)
    if destino.proveedor == "twilio":
        return await _crear_twilio(tenant_id, fuente, destino)
    return await _crear_meta(tenant_id, fuente, destino)


def _buscar_conexion(conns: list[Conexion], proveedor: str, conexion_id: uuid.UUID) -> Conexion | None:
    return next((c for c in conns if c.proveedor == proveedor and c.id == conexion_id), None)


def _no_es_destino(destino: Conexion) -> str | None:
    if destino.sandbox:
        return "El Sandbox de Twilio no acepta plantillas propias: no es destino."
    if not destino.activa:
        return f"{destino.motivo_pausa or 'La conexión está pausada'}: no es destino."
    return None


async def copiar(
    tenant_id: uuid.UUID,
    *,
    origen: tuple[str, uuid.UUID],
    nombre: str,
    idioma: str,
    destinos: list[tuple[str, uuid.UUID]],
) -> list[ResultadoDestino]:
    """Copia la plantilla `(nombre, idioma)` de la conexión de origen a cada destino. Cada destino es independiente."""
    conns = await conexiones(tenant_id)
    conn_origen = _buscar_conexion(conns, *origen)
    if conn_origen is None:
        raise PlantillasNegocioError(404, "not_found", "Conexión de origen no encontrada.")
    fuente = next(
        (
            f
            for f in await fuentes(tenant_id, [conn_origen])
            if f.nombre == nombre and f.idioma == idioma
        ),
        None,
    )
    if fuente is None:
        raise PlantillasNegocioError(404, "plantilla_no_encontrada", "Esa plantilla no está en la conexión de origen.")
    if fuente.estado != APROBADA:
        raise PlantillasNegocioError(
            409, "origen_no_aprobada", "Solo se copian plantillas aprobadas en la conexión de origen."
        )
    resultados: list[ResultadoDestino] = []
    por_waba: dict[str, ResultadoDestino] = {}
    vistos: set[str] = set()
    for proveedor, conexion_id in destinos:
        destino = _buscar_conexion(conns, proveedor, conexion_id)
        clave = f"{proveedor}:{conexion_id}"
        if clave in vistos:
            continue
        vistos.add(clave)
        if destino is None:
            resultados.append(ResultadoDestino(clave, ERROR, "Conexión no encontrada.", nombre, idioma))
            continue
        if destino.clave == conn_origen.clave:
            continue
        motivo = _no_es_destino(destino)
        if motivo is not None:
            resultados.append(_res(destino, fuente, ERROR, motivo))
            continue
        # La misma WABA colgada de dos cuentas: Meta la crea una vez; la segunda recibe el mismo resultado.
        if destino.waba_id is not None and destino.waba_id in por_waba:
            previo = por_waba[destino.waba_id]
            resultados.append(_res(destino, fuente, YA_EXISTIA if previo.resultado == CREADA else previo.resultado, previo.motivo))
            continue
        try:
            r = await _copiar_a(tenant_id, fuente, destino)
        except Exception:
            log.exception("plantilla_replicada_fallo", destino=destino.clave)
            r = _res(destino, fuente, ERROR, "Error inesperado al copiar a esta conexión.")
        if destino.waba_id is not None:
            por_waba[destino.waba_id] = r
        resultados.append(_registrar(tenant_id, fuente, destino, r))
    return resultados


@dataclass(frozen=True)
class ResumenCopia:
    creadas: int
    ya_existian: int
    distintas: int
    no_replicables: int
    errores: int
    restantes: int
    detalle: list[ResultadoDestino]


async def copiar_todas(
    tenant_id: uuid.UUID,
    *,
    origen: tuple[str, uuid.UUID],
    destino: tuple[str, uuid.UUID],
    limite: int = LIMITE_COPIAR_TODAS,
) -> ResumenCopia:
    """Copia a `destino` las plantillas aprobadas del origen. Lo que se decide sin llamar al proveedor (ya existe,
    distinta, no replicable) no cuenta para el tope; de lo demás copia hasta `limite` y dice cuántas quedan."""
    conns = await conexiones(tenant_id)
    conn_origen, conn_destino = _buscar_conexion(conns, *origen), _buscar_conexion(conns, *destino)
    if conn_origen is None or conn_destino is None:
        raise PlantillasNegocioError(404, "not_found", "Conexión no encontrada.")
    if conn_origen.clave == conn_destino.clave:
        raise PlantillasNegocioError(422, "mismo_destino", "El origen y el destino son la misma conexión.")
    motivo = _no_es_destino(conn_destino)
    if motivo is not None:
        raise PlantillasNegocioError(409, "no_es_destino", motivo)
    todas = await fuentes(tenant_id, [conn_origen, conn_destino])
    en_destino = {(f.nombre, f.idioma): f for f in todas if f.clave == conn_destino.clave}
    nombres_destino = {f.nombre: f for f in en_destino.values()}
    detalle: list[ResultadoDestino] = []
    llamadas = restantes = 0
    for f in (f for f in todas if f.clave == conn_origen.clave and f.estado == APROBADA):
        motivo_nr = motivo_no_replicable(f, conn_destino.proveedor)
        if motivo_nr is not None:
            detalle.append(_registrar(tenant_id, f, conn_destino, _res(conn_destino, f, NO_REPLICABLE, motivo_nr)))
            continue
        existente = en_destino.get((f.nombre, f.idioma))
        if existente is None and conn_destino.proveedor == "twilio":
            existente = nombres_destino.get(f.nombre)
        retomar = (
            existente is not None
            and conn_destino.proveedor == "twilio"
            and existente.idioma == f.idioma
            and _mismo_texto(existente.cuerpo, f.cuerpo)
            and existente.estado_proveedor in ("creating", "unsubmitted")
        )
        if existente is not None and not retomar:
            detalle.append(_registrar(tenant_id, f, conn_destino, await _comparar(tenant_id, f, conn_destino, existente)))
            continue
        if llamadas >= limite:
            restantes += 1
            continue
        llamadas += 1
        try:
            r = await _copiar_a(tenant_id, f, conn_destino)
        except Exception:
            log.exception("plantilla_replicada_fallo", destino=conn_destino.clave)
            r = _res(conn_destino, f, ERROR, "Error inesperado al copiar esta plantilla.")
        detalle.append(_registrar(tenant_id, f, conn_destino, r))
    cuenta = {r: sum(1 for d in detalle if d.resultado == r) for r in RESULTADOS}
    return ResumenCopia(
        creadas=cuenta[CREADA],
        ya_existian=cuenta[YA_EXISTIA],
        distintas=cuenta[DISTINTA],
        no_replicables=cuenta[NO_REPLICABLE],
        errores=cuenta[ERROR],
        restantes=restantes,
        detalle=detalle,
    )
