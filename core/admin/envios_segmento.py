"""Envío de una plantilla de WhatsApp a un segmento de Clientes (HU-CRM-06, ADR-0150).

Tres pasos:

1. **Vista previa** (`vista_previa`): con los filtros de Clientes, arma la lista de destinatarios
   y los excluidos por motivo. Si hay a quién enviar y no pasa el tope, guarda el envío en
   `borrador` con su lista (la instantánea): lo que se confirma es lo que el dueño vio.
2. **Confirmar** (`confirmar`): pasa el envío a `confirmado` con su `Idempotency-Key` y emite el
   primer lote al outbox (ADR-0107), en la misma transacción.
3. **Lotes** (`procesar_lote`, handler del outbox): cada evento envía hasta `LOTE` destinatarios
   pendientes y emite el siguiente lote. Un evento por lote, no por destinatario: el despachador
   lo comparten todos los negocios. El despacho cada 30 s da el ritmo.

Idempotencia por destinatario: `client_message_id = segmento:<envio>:<cliente>`, único por
negocio en `messages`. Antes de enviar se vuelven a mirar la baja y las pausas, porque con la
pausa por uso de la API el sender descarta ese id (`sender.py`).

Consentimiento (D-SPEC-33-5): solo reciben los clientes cuya última fila de
`customer_consents` está `otorgado`. Lo marca el dueño en la ficha, con evidencia; la baja llega
por palabras clave del cliente o por el error 131050 de Meta.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Select, and_, func, select, update
from sqlalchemy.exc import IntegrityError

from core.db.models import (
    ChannelConnection,
    Conversation,
    Customer,
    CustomerConsent,
    Escalation,
    Message,
    MessageTemplate,
    PhoneNumber,
    SegmentSend,
    SegmentSendRecipient,
    WabaAccount,
    utcnow,
)
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# Decisiones del dueño (2026-10-21, D-SPEC-33-7).
TOPE_POR_ENVIO = 200
DIAS_ENTRE_PROMOCIONES = 7
VIGENCIA_VISTA_PREVIA = timedelta(minutes=10)
# Más candidatos que esto en el filtro y la vista previa pide acotarlo (como el tope del CSV).
MAX_CANDIDATOS = 5000
LOTE = 25
PREFIJO = "segmento"
PROPOSITO = "promociones"
MAX_NOMBRE = 60  # el nombre lo puede escribir el agente: se acota como en ADR-0108

MOTIVOS = {
    "no_whatsapp": "No es de WhatsApp",
    "sin_conversacion": "Sin conversación en esa conexión",
    "sin_consentimiento": "Sin consentimiento",
    "baja": "Se dio de baja",
    "escalacion_abierta": "Escalación abierta",
    "promocion_reciente": f"Recibió otra promoción hace menos de {DIAS_ENTRE_PROMOCIONES} días",
    "no_permitido_en_test": "No permitido en test",
    "conexion_pausada": "Conexión pausada",
    "sin_nombre": "Sin nombre y sin valor de respaldo",
}

PALABRAS_DE_BAJA_POR_DEFECTO = (
    "baja",
    "stop",
    "no mas promociones",
    "no quiero mas promociones",
    "no quiero mas mensajes",
    "cancelar promociones",
)


class EnvioError(Exception):
    """Error de negocio del envío: `status`, `code` y mensaje para la API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- utilidades


def client_message_id(send_id: uuid.UUID, customer_id: uuid.UUID) -> str:
    return f"{PREFIJO}:{send_id}:{customer_id}"


def es_de_un_envio(client_message_id_: str | None) -> bool:
    return bool(client_message_id_) and str(client_message_id_).startswith(f"{PREFIJO}:")


def _digitos(texto: str | None) -> str:
    return re.sub(r"\D", "", texto or "")


def lista_permitida() -> set[str] | None:
    """Números permitidos fuera de producción real, o `None` si no hay guarda (producción).

    Hay guarda en desarrollo y en test (`QA_TESTERS_ENABLED`); con la lista vacía no sale nada."""
    from core.config import get_settings

    settings = get_settings()
    if settings.app_env == "production" and not settings.qa_testers_enabled:
        return None
    return {d for d in (_digitos(n) for n in settings.segment_send_allowlist.split(",")) if d}


def normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFD", texto or "")
    sin_tildes = "".join(c for c in sin_tildes if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", sin_tildes.lower()).split())


def es_palabra_de_baja(texto: str, palabras: list[str] | tuple[str, ...] | None = None) -> bool:
    """El mensaje entero es una palabra de baja (sin mayúsculas, tildes ni signos). Solo el
    mensaje entero: «baja el precio» no da de baja a nadie."""
    candidatas = {normalizar(p) for p in (palabras or PALABRAS_DE_BAJA_POR_DEFECTO)}
    return normalizar(texto) in candidatas


# ---------------------------------------------------------------- consentimiento


async def estados_de_consentimiento(session: Any, tenant_id: uuid.UUID, customer_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Último estado (`otorgado`/`revocado`) por cliente; sin fila = sin consentimiento."""
    if not customer_ids:
        return {}
    filas = (
        await session.execute(
            select(CustomerConsent.customer_id, CustomerConsent.estado)
            .where(
                CustomerConsent.tenant_id == tenant_id,
                CustomerConsent.proposito == PROPOSITO,
                CustomerConsent.customer_id.in_(customer_ids),
            )
            .distinct(CustomerConsent.customer_id)
            .order_by(CustomerConsent.customer_id, CustomerConsent.created_at.desc(), CustomerConsent.id.desc())
        )
    ).all()
    return {cid: estado for cid, estado in filas}


async def registrar_consentimiento(
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    *,
    estado: str,
    origen: str,
    evidencia: str = "",
    actor_admin_id: uuid.UUID | None = None,
    actor_label: str = "",
) -> CustomerConsent:
    """Agrega una fila de consentimiento (nunca edita). El cliente tiene que ser del negocio."""
    async with get_session() as session:
        existe = (
            await session.execute(
                select(Customer.id).where(
                    Customer.id == customer_id, Customer.tenant_id == tenant_id, Customer.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existe is None:
            raise EnvioError(404, "not_found", "Cliente no encontrado.")
        fila = CustomerConsent(
            tenant_id=tenant_id,
            customer_id=customer_id,
            proposito=PROPOSITO,
            estado=estado,
            origen=origen,
            evidencia=evidencia[:500],
            actor_admin_id=actor_admin_id,
            actor_label=actor_label,
        )
        session.add(fila)
        await session.flush()
    log.info("consentimiento_registrado", customer_id=str(customer_id), estado=estado, origen=origen)
    return fila


async def registrar_baja_si_corresponde(
    customer_id: uuid.UUID, channel: str, texto: str, palabras: list[str] | None = None
) -> bool:
    """Inbound de WhatsApp que es una palabra de baja → fila `revocado` (si no lo estaba ya)."""
    if channel != "whatsapp" or not es_palabra_de_baja(texto, palabras):
        return False
    tenant_id = uuid.UUID(str(get_current_tenant_id()))
    async with get_session() as session:
        estado = (await estados_de_consentimiento(session, tenant_id, [customer_id])).get(customer_id)
    if estado == "revocado":
        return False
    await registrar_consentimiento(
        tenant_id, customer_id, estado="revocado", origen="palabra_de_baja", evidencia=texto[:500]
    )
    return True


# ---------------------------------------------------------------- conexión y plantillas


@dataclass(frozen=True)
class ConexionDeEnvio:
    id: uuid.UUID
    label: str
    cuenta: uuid.UUID  # meta_connections.id
    phone_number_id: str


async def conexion_de_envio(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ConexionDeEnvio:
    """La conexión de WhatsApp Cloud por la que sale el envío. Las de WhatsApp Web o manuales no
    tienen plantillas aprobadas y quedan fuera."""
    async with get_session() as session:
        conexion = (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.id == connection_id,
                    ChannelConnection.tenant_id == tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if conexion is None:
        raise EnvioError(404, "not_found", "Conexión no encontrada.")
    if conexion.channel != "whatsapp" or conexion.meta_connection_id is None:
        raise EnvioError(400, "conexion_sin_plantillas", "Esa conexión no envía plantillas de WhatsApp.")
    from core.gateway.meta_connection_state import motivo_pausa_de

    if motivo := await motivo_pausa_de(conexion.meta_connection_id):
        raise EnvioError(409, "connection_paused", f"{motivo}: reactívala en Conexiones para enviar.")
    return ConexionDeEnvio(
        id=conexion.id,
        label=conexion.label or conexion.external_id,
        cuenta=conexion.meta_connection_id,
        phone_number_id=conexion.external_id,
    )


async def plantillas_de_marketing(tenant_id: uuid.UUID, cuenta: uuid.UUID) -> list[MessageTemplate]:
    """Plantillas `MARKETING` aprobadas de la WABA de la cuenta, solo con variables en el cuerpo
    (v1 de ADR-0108)."""
    from core.gateway.event_notices import template_has_non_body_variables

    async with get_session() as session:
        plantillas = (
            await session.execute(
                select(MessageTemplate)
                .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                .where(
                    WabaAccount.connection_id == cuenta,
                    WabaAccount.deleted_at.is_(None),
                    WabaAccount.tenant_id == tenant_id,
                    MessageTemplate.tenant_id == tenant_id,
                    MessageTemplate.status == "APPROVED",
                    func.upper(MessageTemplate.category) == "MARKETING",
                )
                .order_by(MessageTemplate.name, MessageTemplate.language)
            )
        ).scalars().all()
    return [t for t in plantillas if not template_has_non_body_variables(t.components)]


async def _tier_del_numero(tenant_id: uuid.UUID, phone_number_id: str) -> str | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(PhoneNumber.messaging_limit_tier).where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.phone_number_id == phone_number_id,
                    PhoneNumber.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()


def limite_del_tier(tier: str | None) -> int | None:
    """`TIER_1K` → 1000; `TIER_UNLIMITED` o desconocido → None. Solo informa (ver ADR-0150)."""
    m = re.fullmatch(r"TIER_(\d+)(K?)", (tier or "").upper())
    if not m:
        return None
    return int(m.group(1)) * (1000 if m.group(2) else 1)


# ---------------------------------------------------------------- variables


def validar_variables(placeholders: list[str], variables: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Cada placeholder del cuerpo: `{"fuente": "fijo", "valor": …}` o
    `{"fuente": "cliente.nombre", "valor": respaldo}`. Nada sale del LLM."""
    from core.gateway.event_notices import clean_param

    faltan = [p for p in placeholders if p not in variables]
    if faltan:
        raise EnvioError(400, "template_variables", f"Faltan variables: {', '.join(faltan)}.")
    limpias: dict[str, dict[str, str]] = {}
    for p in placeholders:
        fuente = str(variables[p].get("fuente", "fijo"))
        valor = clean_param(str(variables[p].get("valor", "")), 1000)
        if fuente not in ("fijo", "cliente.nombre"):
            raise EnvioError(400, "template_variables", f"Fuente no permitida para {p}.")
        if fuente == "fijo" and not valor:
            raise EnvioError(400, "template_variables", f"Completa la variable {p}.")
        limpias[p] = {"fuente": fuente, "valor": valor}
    return limpias


def valores_para(variables: dict[str, dict[str, str]], nombre: str | None) -> dict[str, str] | None:
    """placeholder → texto para un cliente, o None si falta su nombre y no hay respaldo."""
    from core.gateway.event_notices import clean_param

    valores: dict[str, str] = {}
    for p, v in variables.items():
        if v["fuente"] == "cliente.nombre":
            texto = clean_param(nombre or "", MAX_NOMBRE) or v["valor"]
            if not texto:
                return None
            valores[p] = texto
        else:
            valores[p] = v["valor"]
    return valores


# ---------------------------------------------------------------- vista previa


@dataclass
class VistaPrevia:
    envio_id: uuid.UUID | None
    candidatos: int
    elegibles: int
    excluidos: dict[str, int]
    muestra: list[str]
    texto: str
    costo_creditos: str
    tope: int
    bloqueo: str | None
    limite_diario: int | None
    expira: Any = None
    destinatarios: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)


async def vista_previa(
    *,
    tenant_id: uuid.UUID,
    admin_id: uuid.UUID,
    admin_label: str,
    consulta: Select[Any],
    filtro: dict[str, Any],
    connection_id: uuid.UUID,
    template_id: uuid.UUID,
    variables: dict[str, dict[str, str]],
) -> VistaPrevia:
    from core.billing.tarifario import TARIFARIO_VIGENTE
    from core.gateway.event_notices import render_body, template_placeholders

    conexion = await conexion_de_envio(tenant_id, connection_id)
    plantilla = next((t for t in await plantillas_de_marketing(tenant_id, conexion.cuenta) if t.id == template_id), None)
    if plantilla is None:
        raise EnvioError(404, "template_not_found", "Plantilla de marketing no aprobada en esa conexión.")
    variables = validar_variables(template_placeholders(plantilla.components), variables)

    sub = consulta.subquery()
    async with get_session() as session:
        candidatos = (await session.execute(select(func.count()).select_from(sub))).scalar() or 0
        if candidatos > MAX_CANDIDATOS:
            raise EnvioError(400, "filtro_muy_amplio", f"El filtro trae {candidatos} clientes: acótalo a {MAX_CANDIDATOS} o menos.")
        clientes = (
            await session.execute(
                select(Customer.id, Customer.channel, Customer.external_id, Customer.contact_phone, Customer.name)
                .where(Customer.id.in_(select(sub.c.id)), Customer.tenant_id == tenant_id)
                .order_by(Customer.created_at.desc(), Customer.id)
            )
        ).all()
        ids = [c.id for c in clientes]
        conversaciones = dict(
            (
                await session.execute(
                    select(Conversation.customer_id, Conversation.id)
                    .where(
                        Conversation.tenant_id == tenant_id,
                        Conversation.customer_id.in_(ids),
                        Conversation.connection_id == conexion.id,
                        Conversation.channel == "whatsapp",
                        Conversation.deleted_at.is_(None),
                    )
                    .distinct(Conversation.customer_id)
                    .order_by(Conversation.customer_id, Conversation.started_at.desc())
                )
            ).all()
        ) if ids else {}
        consentimientos = await estados_de_consentimiento(session, tenant_id, ids)
        con_escalacion = set(
            (
                await session.execute(
                    select(Escalation.conversation_id).where(
                        Escalation.tenant_id == tenant_id,
                        Escalation.conversation_id.in_(list(conversaciones.values())),
                        Escalation.status.in_(("pendiente", "asignada")),
                        Escalation.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
        ) if conversaciones else set()
        desde = utcnow() - timedelta(days=DIAS_ENTRE_PROMOCIONES)
        recientes = set(
            (
                await session.execute(
                    select(SegmentSendRecipient.customer_id).where(
                        SegmentSendRecipient.tenant_id == tenant_id,
                        SegmentSendRecipient.customer_id.in_(ids),
                        SegmentSendRecipient.estado == "encolado",
                        SegmentSendRecipient.updated_at >= desde,
                    )
                )
            ).scalars().all()
        ) if ids else set()
        en_curso = (
            await session.execute(
                select(SegmentSend.id).where(
                    SegmentSend.tenant_id == tenant_id,
                    SegmentSend.connection_id == conexion.id,
                    SegmentSend.estado.in_(("confirmado", "enviando")),
                )
            )
        ).first()

    permitidos = lista_permitida()
    excluidos: dict[str, int] = {}
    destinatarios: list[tuple[uuid.UUID, uuid.UUID]] = []
    muestra: list[str] = []
    for c in clientes:
        motivo: str | None = None
        conv = conversaciones.get(c.id)
        estado = consentimientos.get(c.id)
        if c.channel != "whatsapp":
            motivo = "no_whatsapp"
        elif conv is None:
            motivo = "sin_conversacion"
        elif estado == "revocado":
            motivo = "baja"
        elif estado != "otorgado":
            motivo = "sin_consentimiento"
        elif conv in con_escalacion:
            motivo = "escalacion_abierta"
        elif c.id in recientes:
            motivo = "promocion_reciente"
        elif permitidos is not None and not ({_digitos(c.external_id), _digitos(c.contact_phone)} & permitidos):
            motivo = "no_permitido_en_test"
        elif valores_para(variables, c.name) is None:
            motivo = "sin_nombre"
        if motivo:
            excluidos[motivo] = excluidos.get(motivo, 0) + 1
            continue
        assert conv is not None
        destinatarios.append((c.id, conv))
        if len(muestra) < 5:
            muestra.append(c.name or c.external_id)

    bloqueo = None
    if not destinatarios:
        bloqueo = "sin_destinatarios"
    elif len(destinatarios) > TOPE_POR_ENVIO:
        bloqueo = "tope"
    elif en_curso is not None:
        bloqueo = "envio_en_curso"

    tier = await _tier_del_numero(tenant_id, conexion.phone_number_id)
    vista = VistaPrevia(
        envio_id=None,
        candidatos=int(candidatos),
        elegibles=len(destinatarios),
        excluidos=excluidos,
        muestra=muestra,
        texto=render_body(plantilla, {p: (v["valor"] or f"{{{{{p}}}}}") for p, v in variables.items()}),
        costo_creditos=str(TARIFARIO_VIGENTE.fees["template_sent"] * len(destinatarios)),
        tope=TOPE_POR_ENVIO,
        bloqueo=bloqueo,
        limite_diario=limite_del_tier(tier),
        destinatarios=destinatarios,
    )
    if bloqueo is not None:
        return vista

    async with get_session() as session:
        envio = SegmentSend(
            tenant_id=tenant_id,
            admin_id=admin_id,
            admin_label=admin_label,
            connection_id=conexion.id,
            template_id=plantilla.id,
            template_name=plantilla.name,
            template_language=plantilla.language,
            variables=variables,
            filtro=filtro,
            excluidos=excluidos,
            total=len(destinatarios),
            estado="borrador",
            expires_at=utcnow() + VIGENCIA_VISTA_PREVIA,
        )
        session.add(envio)
        await session.flush()
        session.add_all(
            SegmentSendRecipient(tenant_id=tenant_id, send_id=envio.id, customer_id=cid, conversation_id=conv)
            for cid, conv in destinatarios
        )
        vista.envio_id = envio.id
        vista.expira = envio.expires_at
    log.info("envio_segmento_vista_previa", send_id=str(vista.envio_id), elegibles=vista.elegibles, excluidos=excluidos)
    return vista


# ---------------------------------------------------------------- confirmar


def _hash(envio_id: uuid.UUID) -> str:
    return hashlib.sha256(str(envio_id).encode()).hexdigest()


async def confirmar(
    *, tenant_id: uuid.UUID, admin_id: uuid.UUID, envio_id: uuid.UUID, idempotency_key: str
) -> SegmentSend:
    """`borrador` → `confirmado` y primer lote al outbox, en una transacción.

    La misma clave con el mismo envío devuelve el envío tal cual (doble clic, reintento de red).
    La misma clave con otro envío, o un envío ya confirmado con otra clave → 409."""
    from core.events.outbox import emit_domain
    from core.shared.events import ON_SEGMENT_SEND_BATCH

    if not idempotency_key or len(idempotency_key) > 200:
        raise EnvioError(400, "idempotency_key", "Falta la cabecera Idempotency-Key.")
    pedido = _hash(envio_id)
    try:
        async with get_session() as session:
            previo = (
                await session.execute(
                    select(SegmentSend).where(
                        SegmentSend.tenant_id == tenant_id, SegmentSend.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if previo is not None:
                if previo.request_hash != pedido:
                    raise EnvioError(409, "idempotency_conflict", "Esa clave ya se usó con otro envío.")
                return previo
            envio = (
                await session.execute(
                    select(SegmentSend)
                    .where(SegmentSend.id == envio_id, SegmentSend.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if envio is None:
                raise EnvioError(404, "not_found", "Envío no encontrado.")
            if envio.admin_id != admin_id:
                raise EnvioError(403, "forbidden", "Solo quien armó la vista previa puede confirmarla.")
            if envio.estado != "borrador":
                raise EnvioError(409, "ya_confirmado", "Este envío ya se confirmó.")
            if envio.expires_at < utcnow():
                raise EnvioError(410, "vista_previa_vencida", "La vista previa venció: vuelve a armarla.")
            envio.estado = "confirmado"
            envio.idempotency_key = idempotency_key
            envio.request_hash = pedido
            envio.confirmed_at = utcnow()
            await session.flush()
            await emit_domain(
                session,
                tenant_id=tenant_id,
                topic=ON_SEGMENT_SEND_BATCH,
                dedupe_key=f"{PREFIJO}:{envio.id}:lote:1",
                payload={"send_id": str(envio.id), "lote": 1},
            )
    except IntegrityError as exc:
        # `uq_segment_sends_en_curso`: otro envío de la misma conexión se confirmó primero.
        raise EnvioError(409, "envio_en_curso", "Ya hay un envío en curso por esta conexión.") from exc
    log.info("envio_segmento_confirmado", send_id=str(envio_id), total=envio.total)
    return envio


# ---------------------------------------------------------------- lotes (worker)


async def _marcar(recipient_id: uuid.UUID, estado: str, *, motivo: str | None = None, message_id: uuid.UUID | None = None) -> None:
    async with get_session() as session:
        await session.execute(
            update(SegmentSendRecipient)
            .where(SegmentSendRecipient.id == recipient_id, SegmentSendRecipient.tenant_id == get_current_tenant_id())
            .values(estado=estado, motivo=motivo, message_id=message_id, updated_at=utcnow())
        )


async def _mensaje_por_id_cliente(tenant_id: uuid.UUID, cmid: str) -> uuid.UUID | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(Message.id).where(Message.tenant_id == tenant_id, Message.client_message_id == cmid)
            )
        ).scalar_one_or_none()


async def procesar_lote(send_id: str, lote: int = 1, **_: Any) -> None:
    """Handler del outbox: envía hasta `LOTE` destinatarios pendientes y emite el lote siguiente.

    Corre bajo el `tenant_scope` del evento. Si la API de Meta está en pausa por uso, deja los
    pendientes como están y lanza para que el outbox reintente con backoff."""
    from core.events.outbox import emit_domain
    from core.gateway.event_notices import build_components, render_body
    from core.gateway.meta_connection_state import motivo_pausa_de_conversacion
    from core.gateway.meta_usage import connection_id_for_phone_number, is_paused
    from core.gateway.sender import send_template_to_conversation
    from core.shared.events import ON_SEGMENT_SEND_BATCH

    tenant_id = uuid.UUID(str(get_current_tenant_id()))
    envio_id = uuid.UUID(send_id)
    async with get_session() as session:
        envio = (
            await session.execute(select(SegmentSend).where(SegmentSend.id == envio_id, SegmentSend.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if envio is None or envio.estado not in ("confirmado", "enviando"):
            return
        if envio.estado == "confirmado":
            envio.estado = "enviando"
        pendientes = (
            await session.execute(
                select(SegmentSendRecipient, Conversation, Customer)
                .join(
                    Conversation,
                    and_(Conversation.id == SegmentSendRecipient.conversation_id, Conversation.tenant_id == tenant_id),
                )
                .join(Customer, and_(Customer.id == SegmentSendRecipient.customer_id, Customer.tenant_id == tenant_id))
                .where(
                    SegmentSendRecipient.tenant_id == tenant_id,
                    SegmentSendRecipient.send_id == envio_id,
                    SegmentSendRecipient.estado == "pendiente",
                )
                .order_by(SegmentSendRecipient.created_at, SegmentSendRecipient.id)
                .limit(LOTE)
            )
        ).all()
        consentimientos = await estados_de_consentimiento(session, tenant_id, [r.customer_id for r, _c, _k in pendientes])
        plantilla = (
            await session.execute(
                select(MessageTemplate).where(MessageTemplate.id == envio.template_id, MessageTemplate.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        variables = dict(envio.variables or {})

    for destinatario, conversacion, cliente in pendientes:
        if consentimientos.get(cliente.id) != "otorgado":
            await _marcar(destinatario.id, "omitido", motivo="baja" if consentimientos.get(cliente.id) == "revocado" else "sin_consentimiento")
            continue
        if plantilla is None or plantilla.status != "APPROVED":
            await _marcar(destinatario.id, "omitido", motivo="plantilla_no_aprobada")
            continue
        if await motivo_pausa_de_conversacion(conversacion):
            await _marcar(destinatario.id, "omitido", motivo="conexion_pausada")
            continue
        cmid = client_message_id(envio_id, cliente.id)
        if (ya := await _mensaje_por_id_cliente(tenant_id, cmid)) is not None:
            await _marcar(destinatario.id, "encolado", message_id=ya)
            continue
        # Con la pausa por uso el sender descartaría el id estable: se mira antes y se reintenta.
        cuenta = await connection_id_for_phone_number(
            (await _phone_number_id_de(conversacion)) or "", tenant_id
        )
        if await is_paused(tenant_id, cuenta):
            raise RuntimeError("API de Meta en pausa por uso: el lote se reintenta")
        valores = valores_para(variables, cliente.name)
        if valores is None:
            await _marcar(destinatario.id, "omitido", motivo="sin_nombre")
            continue
        try:
            enviado = await send_template_to_conversation(
                conversacion.id,
                template_name=plantilla.name,
                language=plantilla.language,
                components=build_components(plantilla, valores),
                preview_text=render_body(plantilla, valores),
                sender_type="human",
                client_message_id=cmid,
            )
        except IntegrityError:
            enviado = True  # otro proceso ya lo creó: cuenta como enviado
        if not enviado:
            raise RuntimeError("La plantilla no se encoló: el lote se reintenta")
        await _marcar(destinatario.id, "encolado", message_id=await _mensaje_por_id_cliente(tenant_id, cmid))

    async with get_session() as session:
        quedan = (
            await session.execute(
                select(func.count()).where(
                    SegmentSendRecipient.tenant_id == tenant_id,
                    SegmentSendRecipient.send_id == envio_id,
                    SegmentSendRecipient.estado == "pendiente",
                )
            )
        ).scalar() or 0
        if quedan:
            await emit_domain(
                session,
                tenant_id=tenant_id,
                topic=ON_SEGMENT_SEND_BATCH,
                dedupe_key=f"{PREFIJO}:{envio_id}:lote:{lote + 1}",
                payload={"send_id": str(envio_id), "lote": lote + 1},
            )
        else:
            await session.execute(
                update(SegmentSend)
                .where(SegmentSend.id == envio_id, SegmentSend.tenant_id == tenant_id)
                .values(estado="terminado", finished_at=utcnow(), updated_at=utcnow())
            )
    log.info("envio_segmento_lote", send_id=send_id, lote=lote, enviados=len(pendientes), quedan=int(quedan))


async def _phone_number_id_de(conversacion: Conversation) -> str | None:
    """`phone_number_id` de la conexión de la conversación (la misma que el sender)."""
    async with get_session() as session:
        return (
            await session.execute(
                select(ChannelConnection.external_id).where(
                    ChannelConnection.id == conversacion.connection_id,
                    ChannelConnection.tenant_id == conversacion.tenant_id,
                )
            )
        ).scalar_one_or_none()


# ---------------------------------------------------------------- detalle


async def detalle(tenant_id: uuid.UUID, envio_id: uuid.UUID) -> tuple[SegmentSend, dict[str, int], list[tuple[str, str]]]:
    """(envío, conteo por estado, fallas [(cliente, motivo)] hasta 50). Los estados de Meta
    (enviado, entregado, leído, fallido) salen del `Message`, que actualiza el webhook."""
    async with get_session() as session:
        envio = (
            await session.execute(select(SegmentSend).where(SegmentSend.id == envio_id, SegmentSend.tenant_id == tenant_id))
        ).scalar_one_or_none()
        if envio is None:
            raise EnvioError(404, "not_found", "Envío no encontrado.")
        filas = (
            await session.execute(
                select(SegmentSendRecipient.estado, SegmentSendRecipient.motivo, Message.status, Message.error_reason, Customer.name, Customer.external_id)
                .outerjoin(Message, and_(Message.id == SegmentSendRecipient.message_id, Message.tenant_id == tenant_id))
                .join(Customer, and_(Customer.id == SegmentSendRecipient.customer_id, Customer.tenant_id == tenant_id))
                .where(SegmentSendRecipient.tenant_id == tenant_id, SegmentSendRecipient.send_id == envio_id)
            )
        ).all()
    conteo: dict[str, int] = {}
    fallas: list[tuple[str, str]] = []
    for estado, motivo, status, error, nombre, ext in filas:
        clave = status if estado == "encolado" and status else estado
        conteo[clave] = conteo.get(clave, 0) + 1
        if (estado == "omitido" or status == "failed") and len(fallas) < 50:
            fallas.append((nombre or ext, MOTIVOS.get(motivo or "", motivo or error or "Falló")))
    return envio, conteo, fallas
