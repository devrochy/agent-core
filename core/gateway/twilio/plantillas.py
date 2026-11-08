"""Plantillas de WhatsApp vía Twilio (ADR-0186 §8, PR-6): crear, enviar a aprobación, consultar y sincronizar.

Una plantilla es un Content de la cuenta de Twilio de la conexión (`HX…`). En v1 son de texto (`twilio/text`) con
variables `{{1}}`, `{{2}}`… y su valor de ejemplo. Una plantilla enviada a aprobación no se edita: se crea otra.

Crear es en dos pasos, para que un corte a mitad no duplique nada: primero la fila local en `creating` (el único
por `friendly_name` evita dos altas iguales) y después el Content en Twilio. Si el segundo paso falla, la fila
queda en `creating`; repetir el pedido la reconcilia: busca el Content por `friendly_name` en la cuenta antes de
crearlo de nuevo.

Twilio no avisa por webhook cuando cambia la aprobación. `next_poll_at` programa la próxima consulta con backoff
(cada 5 min la primera hora, cada 30 min hasta 48 h, cada hora hasta 14 días; después se deja). El cron
`core.twilio_template_poll` consulta las vencidas y `core.twilio_template_sync` trae cada hora todas las de cada
cuenta (también las creadas en la consola de Twilio). El sync manual tiene tope de uno por minuto por conexión.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from agentcore_channel_twilio.content import (
    APPROVAL_STATUSES,
    CATEGORIES,
    ContentTemplate,
    create_content,
    delete_content,
    fetch_approval,
    list_contents_completo,
    list_contents_with_approvals,
    submit_whatsapp_approval,
    validate_variables,
)
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from core.db.models import TwilioContentTemplate, utcnow
from core.db.session import get_session
from core.gateway.channels.base import ProviderError
from core.gateway.twilio import alta, operacion
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

_NOMBRE = re.compile(r"[a-z0-9_]{1,64}")
_IDIOMA = re.compile(r"[a-z]{2}(_[A-Z]{2})?")
MAX_CUERPO = 1024
_EN_REVISION = frozenset({"received", "pending"})
_ESTADOS = APPROVAL_STATUSES | {"creating"}
SYNC_MANUAL_CADA_S = 60
MAX_EJEMPLO = 256
# Tope de plantillas vivas por conexión (WhatsApp permite miles por cuenta; aquí no hace falta tanto).
MAX_PLANTILLAS_POR_CONEXION = 1000


@dataclass(frozen=True)
class Plantilla:
    id: uuid.UUID
    connection_id: uuid.UUID
    content_sid: str | None
    nombre: str
    idioma: str
    categoria: str
    cuerpo: str
    ejemplos: dict[str, str]
    estado: str
    motivo_rechazo: str
    enviada_at: datetime | None
    aprobada_at: datetime | None


def _salida(f: TwilioContentTemplate) -> Plantilla:
    return Plantilla(
        id=f.id,
        connection_id=f.channel_connection_id,
        content_sid=f.content_sid,
        nombre=f.friendly_name,
        idioma=f.language,
        categoria=f.category,
        cuerpo=f.body,
        ejemplos=dict(f.variables or {}),
        estado=f.approval_status,
        motivo_rechazo=f.rejection_reason,
        enviada_at=f.submitted_at,
        aprobada_at=f.approved_at,
    )


def proximo_poll(enviada_at: datetime | None, ahora: datetime) -> datetime | None:
    """Cuándo volver a consultar una plantilla en revisión, según cuánto lleva esperando."""
    if enviada_at is None:
        return ahora + timedelta(minutes=5)
    espera = ahora - enviada_at
    if espera < timedelta(hours=1):
        return ahora + timedelta(minutes=5)
    if espera < timedelta(hours=48):
        return ahora + timedelta(minutes=30)
    if espera < timedelta(days=14):
        return ahora + timedelta(hours=1)
    return None


def _aplicar_aprobacion(
    fila: TwilioContentTemplate, estado: str, motivo: str, categoria: str, ahora: datetime
) -> None:
    estado = estado.lower()
    if estado not in _ESTADOS:
        log.warning("twilio_plantilla_estado_desconocido", estado=estado[:32])
        return
    anterior = fila.approval_status
    fila.approval_status = estado
    fila.rejection_reason = motivo[:500]
    if categoria in CATEGORIES:
        fila.category = categoria
    if estado == "approved" and fila.approved_at is None:
        fila.approved_at = ahora
    if estado in _EN_REVISION:
        if fila.submitted_at is None:
            fila.submitted_at = ahora
        fila.next_poll_at = proximo_poll(fila.submitted_at, ahora)
    else:
        fila.next_poll_at = None
    fila.last_polled_at = ahora
    if anterior != estado:
        log.info(
            "twilio_template_status_changed", template_id=str(fila.id), antes=anterior, ahora=estado
        )


def validar(
    nombre: str, idioma: str, categoria: str, cuerpo: str, ejemplos: dict[str, str]
) -> None:
    """Reglas de WhatsApp antes de llamar a Twilio. Lanza `AltaError` 422 con el motivo."""
    if not _NOMBRE.fullmatch(nombre):
        raise alta.AltaError(
            422, "nombre_invalido", "El nombre usa minúsculas, números y guion bajo (hasta 64)."
        )
    if not _IDIOMA.fullmatch(idioma):
        raise alta.AltaError(422, "idioma_invalido", "El idioma va como «es» o «es_CO».")
    if categoria not in CATEGORIES:
        raise alta.AltaError(
            422, "categoria_invalida", "La categoría es UTILITY, MARKETING o AUTHENTICATION."
        )
    if not cuerpo.strip() or len(cuerpo) > MAX_CUERPO:
        raise alta.AltaError(
            422,
            "cuerpo_invalido",
            f"El texto no puede estar vacío ni pasar de {MAX_CUERPO} caracteres.",
        )
    if any(not str(k).isdigit() or len(str(v)) > MAX_EJEMPLO for k, v in ejemplos.items()):
        raise alta.AltaError(
            422,
            "variables_invalidas",
            f"Las variables son números y cada ejemplo tiene hasta {MAX_EJEMPLO} caracteres.",
        )
    try:
        validate_variables([cuerpo], ejemplos)
    except ProviderError as exc:
        raise alta.AltaError(422, "variables_invalidas", str(exc)) from None


async def _conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, transport: httpx.AsyncBaseTransport | None
):
    c = await operacion._cargar(tenant_id, connection_id)
    if c is None:
        raise alta.AltaError(404, "not_found", "Conexión no encontrada.")
    return operacion._cliente_de(c, transport)


async def _fila(
    session: Any, tenant_id: uuid.UUID, plantilla_id: uuid.UUID, *, bloquear: bool = False
):
    stmt = select(TwilioContentTemplate).where(
        TwilioContentTemplate.id == plantilla_id,
        TwilioContentTemplate.tenant_id == tenant_id,
        TwilioContentTemplate.deleted_at.is_(None),
    )
    if bloquear:
        stmt = stmt.with_for_update()
    fila = (await session.execute(stmt)).scalar_one_or_none()
    if fila is None:
        raise alta.AltaError(404, "not_found", "Plantilla no encontrada.")
    return fila


async def listar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, solo_aprobadas: bool = False
) -> list[Plantilla]:
    alta._exigir_scope(tenant_id)
    async with get_session() as session:
        stmt = select(TwilioContentTemplate).where(
            TwilioContentTemplate.tenant_id == tenant_id,
            TwilioContentTemplate.channel_connection_id == connection_id,
            TwilioContentTemplate.deleted_at.is_(None),
        )
        if solo_aprobadas:
            stmt = stmt.where(TwilioContentTemplate.approval_status == "approved")
        filas = (
            (await session.execute(stmt.order_by(TwilioContentTemplate.friendly_name)))
            .scalars()
            .all()
        )
    return [_salida(f) for f in filas]


async def crear(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    nombre: str,
    idioma: str,
    categoria: str,
    cuerpo: str,
    ejemplos: dict[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> Plantilla:
    categoria = categoria.upper()
    validar(nombre, idioma, categoria, cuerpo, ejemplos)
    client = await _conexion(tenant_id, connection_id, transport)
    tipos = {"twilio/text": {"body": cuerpo}}

    # Paso 1: la fila local, o la que quedó a medias de un intento anterior con el mismo nombre.
    try:
        async with get_session() as session:
            fila = (
                await session.execute(
                    select(TwilioContentTemplate).where(
                        TwilioContentTemplate.tenant_id == tenant_id,
                        TwilioContentTemplate.channel_connection_id == connection_id,
                        TwilioContentTemplate.friendly_name == nombre,
                        TwilioContentTemplate.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if fila is not None and fila.approval_status != "creating":
                raise alta.AltaError(
                    409, "nombre_en_uso", "Ya hay una plantilla con ese nombre en esta conexión."
                )
            reintento = fila is not None
            if fila is None:
                vivas = (
                    await session.execute(
                        select(func.count(TwilioContentTemplate.id)).where(
                            TwilioContentTemplate.tenant_id == tenant_id,
                            TwilioContentTemplate.channel_connection_id == connection_id,
                            TwilioContentTemplate.deleted_at.is_(None),
                        )
                    )
                ).scalar_one()
                if vivas >= MAX_PLANTILLAS_POR_CONEXION:
                    raise alta.AltaError(
                        409,
                        "limite_de_plantillas",
                        f"La conexión ya tiene {MAX_PLANTILLAS_POR_CONEXION} plantillas.",
                    )
            if fila is None:
                fila = TwilioContentTemplate(
                    tenant_id=tenant_id,
                    channel_connection_id=connection_id,
                    friendly_name=nombre,
                    whatsapp_name=nombre,
                    language=idioma,
                    category=categoria,
                    body=cuerpo,
                    types=tipos,
                    variables=dict(ejemplos),
                    approval_status="creating",
                )
                session.add(fila)
            else:
                fila.language, fila.category, fila.body, fila.types, fila.variables = (
                    idioma,
                    categoria,
                    cuerpo,
                    tipos,
                    dict(ejemplos),
                )
            await session.flush()
            plantilla_id = fila.id
    except IntegrityError:
        raise alta.AltaError(
            409, "nombre_en_uso", "Ya hay una plantilla con ese nombre en esta conexión."
        ) from None

    # Paso 2: el Content en Twilio (reconciliado si ya existe con ese nombre).
    try:
        existente = (
            next(
                (
                    c
                    for c in await list_contents_with_approvals(client)
                    if c.friendly_name == nombre and _SID_HX.fullmatch(c.sid or "")
                ),
                None,
            )
            if reintento
            else None
        )
        content = existente or await create_content(
            client, friendly_name=nombre, language=idioma, types=tipos, variables=ejemplos
        )
    except ProviderError as exc:
        log.warning("twilio_plantilla_no_creada", template_id=str(plantilla_id), code=exc.code)
        raise alta._traducir(exc) from None

    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id, bloquear=True)
        if existente is not None:
            _adoptar(fila, existente)
        else:
            fila.content_sid = content.sid
        _aplicar_aprobacion(
            fila, content.approval.status, content.approval.rejection_reason, "", utcnow()
        )
        resultado = _salida(fila)
    log.info(
        "twilio_plantilla_creada", template_id=str(plantilla_id), reconciliada=existente is not None
    )
    return resultado


async def enviar_a_aprobacion(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    plantilla_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Plantilla:
    """Pide la aprobación de WhatsApp. Idempotente: si Twilio ya tiene la solicitud, solo trae su estado."""
    client = await _conexion(tenant_id, connection_id, transport)
    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id)
        if fila.channel_connection_id != connection_id:
            raise alta.AltaError(404, "not_found", "Plantilla no encontrada.")
        if not fila.content_sid:
            raise alta.AltaError(
                409, "plantilla_sin_crear", "La plantilla todavía no existe en Twilio."
            )
        sid, nombre, categoria = fila.content_sid, fila.whatsapp_name, fila.category
    try:
        aprobacion = await fetch_approval(client, sid)
        if aprobacion.status in ("unsubmitted", ""):
            aprobacion = await submit_whatsapp_approval(
                client, sid, name=nombre, category=categoria
            )
    except ProviderError as exc:
        raise alta._traducir(exc) from None
    ahora = utcnow()
    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id, bloquear=True)
        if aprobacion.status in _EN_REVISION and fila.submitted_at is None:
            fila.submitted_at = ahora
        _aplicar_aprobacion(
            fila, aprobacion.status, aprobacion.rejection_reason, aprobacion.category, ahora
        )
        return _salida(fila)


async def refrescar(
    tenant_id: uuid.UUID,
    plantilla_id: uuid.UUID,
    *,
    connection_id: uuid.UUID | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Plantilla:
    """Consulta ahora la aprobación de una plantilla (de esa conexión, si se indica)."""
    alta._exigir_scope(tenant_id)
    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id)
        if connection_id is not None and fila.channel_connection_id != connection_id:
            raise alta.AltaError(404, "not_found", "Plantilla no encontrada.")
        connection_id, sid = fila.channel_connection_id, fila.content_sid
    if not sid:
        async with get_session() as session:
            return _salida(await _fila(session, tenant_id, plantilla_id))
    client = await _conexion(tenant_id, connection_id, transport)
    try:
        aprobacion = await fetch_approval(client, sid)
    except ProviderError as exc:
        transitorio = exc.category in ("TRANSIENT", "AMBIGUOUS", "RATE_LIMIT", "QUOTA")
        no_existe = exc.http_status == 404 or exc.code == 20404
        async with get_session() as session:
            fila = await _fila(session, tenant_id, plantilla_id, bloquear=True)
            ahora = utcnow()
            fila.last_polled_at = ahora
            if no_existe:
                # El Content se borró en la consola de Twilio: la plantilla ya no envía nada.
                fila.deleted_at, fila.next_poll_at = ahora, None
            else:
                # Pasajero: en 5 min. Token revocado u otro error: en 1 h (una fila trabada no acapara el cron).
                fila.next_poll_at = ahora + (
                    timedelta(minutes=5) if transitorio else timedelta(hours=1)
                )
            salida = _salida(fila)
        if transitorio:
            return salida
        raise alta._traducir(exc) from None
    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id, bloquear=True)
        _aplicar_aprobacion(
            fila, aprobacion.status, aprobacion.rejection_reason, aprobacion.category, utcnow()
        )
        return _salida(fila)


async def sincronizar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    manual: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Plantilla]:
    """Trae todas las plantillas de la cuenta (también las creadas en la consola de Twilio) y sus aprobaciones. Las que
    ya no están en Twilio (borradas en su consola) se dan de baja aquí."""
    client = await _conexion(tenant_id, connection_id, transport)
    if manual:
        # El tope se toma después de validar la conexión: un id inventado no gasta claves.
        from core.memory.working import get_redis

        clave = f"twilio_tpl_sync:{tenant_id}:{connection_id}"
        try:
            libre = await get_redis().set(clave, "1", nx=True, ex=SYNC_MANUAL_CADA_S)
        except Exception:
            libre = True  # sin Redis no se bloquea el botón
        if not libre:
            raise alta.AltaError(
                429, "sync_rate_limited", "Espera un minuto antes de sincronizar otra vez."
            )
    try:
        contenidos, completo = await list_contents_completo(client)
    except ProviderError as exc:
        raise alta._traducir(exc) from None
    ahora = utcnow()
    try:
        async with get_session() as session:
            filas = (
                (
                    await session.execute(
                        select(TwilioContentTemplate)
                        .where(
                            TwilioContentTemplate.tenant_id == tenant_id,
                            TwilioContentTemplate.channel_connection_id == connection_id,
                            TwilioContentTemplate.deleted_at.is_(None),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            por_sid = {f.content_sid: f for f in filas if f.content_sid}
            # Las que se borraron aquí y siguen en Twilio (importadas, compartidas o sin poder borrarse allá) no vuelven.
            dadas_de_baja = set(
                (
                    await session.execute(
                        select(TwilioContentTemplate.content_sid).where(
                            TwilioContentTemplate.tenant_id == tenant_id,
                            TwilioContentTemplate.channel_connection_id == connection_id,
                            TwilioContentTemplate.deleted_at.is_not(None),
                            TwilioContentTemplate.content_sid.is_not(None),
                        )
                    )
                ).scalars()
            )
            por_nombre = {f.friendly_name: f for f in filas}
            vistos: set[str] = set()
            for content in contenidos:
                if not _SID_HX.fullmatch(content.sid or ""):
                    continue
                vistos.add(content.sid)
                fila = por_sid.get(content.sid)
                pendiente = por_nombre.get(content.friendly_name)
                if fila is None and pendiente is not None and not pendiente.content_sid:
                    fila = pendiente  # la que quedó en `creating`
                    _adoptar(fila, content)
                if fila is None and content.sid in dadas_de_baja:
                    continue
                if fila is None:
                    fila = _desde_twilio(tenant_id, connection_id, content)
                    if fila is None or fila.friendly_name in por_nombre:
                        continue
                    session.add(fila)
                    por_nombre[fila.friendly_name] = fila
                _aplicar_aprobacion(
                    fila,
                    content.approval.status,
                    content.approval.rejection_reason,
                    content.approval.category,
                    ahora,
                )
            if contenidos and completo:
                # Borradas en la consola de Twilio: el `HX` ya no envía nada. Solo con la lista COMPLETA (no vacía ni
                # cortada en el tope de páginas): si no, se darían de baja plantillas vivas.
                for f in filas:
                    if f.content_sid and f.content_sid not in vistos:
                        f.deleted_at, f.next_poll_at = ahora, None
                        por_nombre.pop(f.friendly_name, None)
            await session.flush()
            resultado = [_salida(f) for f in por_nombre.values()]
    except IntegrityError:
        # Sin el SQL ni sus parámetros en el log (llevan el texto de las plantillas).
        log.warning(
            "twilio_plantillas_sync_conflicto",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
        )
        raise alta.AltaError(
            409, "conflicto", "No se pudieron sincronizar las plantillas. Intenta de nuevo."
        ) from None
    log.info(
        "twilio_plantillas_sincronizadas",
        tenant_id=str(tenant_id),
        connection_id=str(connection_id),
        total=len(contenidos),
    )
    return sorted(resultado, key=lambda p: p.nombre)


_SID_HX = re.compile(r"HX[0-9a-f]{32}")
_MAX_VARIABLES = 20


def _datos_de_twilio(
    content: ContentTemplate,
) -> tuple[str, dict[str, Any], dict[str, str], str] | None:
    """Lo que se guarda de un Content de Twilio, acotado (invariante 7: es dato y viene de fuera): solo el texto de
    `twilio/text`, hasta 20 variables cortas y un idioma válido. `None` si no cabe en las reglas."""
    texto = content.types.get("twilio/text")
    cuerpo = str(texto.get("body") or "") if isinstance(texto, dict) else ""
    if not _SID_HX.fullmatch(content.sid or "") or not cuerpo or len(cuerpo) > MAX_CUERPO:
        return None
    variables = {
        str(k): str(v)[:MAX_EJEMPLO]
        for k, v in list((content.variables or {}).items())[:_MAX_VARIABLES]
        if str(k).isdigit() and len(str(k)) <= 2
    }
    idioma = content.language if _IDIOMA.fullmatch(content.language or "") else "es"
    return cuerpo, {"twilio/text": {"body": cuerpo}}, variables, idioma


def _adoptar(fila: TwilioContentTemplate, content: ContentTemplate) -> None:
    """Una fila que quedó en `creating` toma el Content de Twilio con ese nombre y SU contenido: lo que se muestra es
    lo que el `HX` envía."""
    fila.content_sid = content.sid
    # No se puede probar que lo creó solvershub (pudo crearse en la consola con el mismo nombre): se trata como
    # importado y no se borra en Twilio al borrarlo aquí.
    fila.origen = "twilio"
    datos = _datos_de_twilio(content)
    if datos is not None:
        fila.body, fila.types, fila.variables, fila.language = datos


def _desde_twilio(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, content: ContentTemplate
) -> TwilioContentTemplate | None:
    """Una plantilla creada en la consola de Twilio (`origen='twilio'`). Se omite si no cabe en las reglas."""
    datos = _datos_de_twilio(content)
    if not _NOMBRE.fullmatch(content.friendly_name or "") or datos is None:
        log.info("twilio_plantilla_omitida", content_sid=(content.sid or "")[:34])
        return None
    cuerpo, tipos, variables, idioma = datos
    categoria = content.approval.category if content.approval.category in CATEGORIES else "UTILITY"
    nombre_wa = (
        content.approval.name
        if _NOMBRE.fullmatch(content.approval.name or "")
        else content.friendly_name
    )
    return TwilioContentTemplate(
        tenant_id=tenant_id,
        channel_connection_id=connection_id,
        content_sid=content.sid,
        friendly_name=content.friendly_name,
        whatsapp_name=nombre_wa,
        language=idioma,
        category=categoria,
        body=cuerpo,
        types=tipos,
        variables=variables,
        origen="twilio",
        approval_status="unsubmitted",
    )


async def borrar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    plantilla_id: uuid.UUID,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Borra la plantilla aquí (lógico) y el Content en Twilio (best-effort: si falla, queda en la cuenta)."""
    client = await _conexion(tenant_id, connection_id, transport)
    async with get_session() as session:
        fila = await _fila(session, tenant_id, plantilla_id, bloquear=True)
        if fila.channel_connection_id != connection_id:
            raise alta.AltaError(404, "not_found", "Plantilla no encontrada.")
        sid = fila.content_sid
        fila.deleted_at = utcnow()
        fila.next_poll_at = None
        # En Twilio solo se borra lo que creó solvershub y nadie más usa: una importada de la consola puede usarse
        # fuera de aquí, y el mismo `HX` puede estar en otra conexión de la cuenta.
        otra = (
            await session.execute(
                select(TwilioContentTemplate.id).where(
                    TwilioContentTemplate.tenant_id == tenant_id,
                    TwilioContentTemplate.content_sid == sid,
                    TwilioContentTemplate.id != plantilla_id,
                    TwilioContentTemplate.deleted_at.is_(None),
                )
            )
        ).first()
        borrar_en_twilio = bool(sid) and fila.origen == "solvershub" and otra is None
    if borrar_en_twilio and sid:
        try:
            await delete_content(client, sid)
        except ProviderError:
            log.warning("twilio_plantilla_no_borrada_en_twilio", template_id=str(plantilla_id))


async def revisar_pendientes(limite: int = 200) -> int:
    """Cron: consulta las plantillas en revisión cuyo `next_poll_at` venció, de todos los negocios."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text("select id, tenant_id from admin_twilio_plantillas_por_revisar(:l)"),
                {"l": limite},
            )
        ).all()
    revisadas = 0
    for plantilla_id, tenant_id in filas:
        try:
            async with tenant_scope(tenant_id):
                await refrescar(tenant_id, plantilla_id)
            revisadas += 1
        except Exception:
            log.exception(
                "twilio_plantilla_poll_fallo",
                tenant_id=str(tenant_id),
                template_id=str(plantilla_id),
            )
    log.info("twilio_plantillas_poll", revisadas=revisadas, total=len(filas))
    return revisadas


async def sincronizar_todas() -> int:
    """Cron horario: sincroniza las plantillas de todas las conexiones vivas y activas."""
    async with get_session() as session:
        filas = (
            await session.execute(text("select id, tenant_id from admin_twilio_connection_ids()"))
        ).all()
    hechas = 0
    for connection_id, tenant_id in filas:
        try:
            async with tenant_scope(tenant_id):
                await sincronizar(tenant_id, connection_id, manual=False)
            hechas += 1
        except Exception:
            log.exception(
                "twilio_plantillas_sync_fallo",
                tenant_id=str(tenant_id),
                connection_id=str(connection_id),
            )
    return hechas
