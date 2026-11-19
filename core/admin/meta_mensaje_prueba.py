"""Mensaje de prueba desde el hub de una conexión de WhatsApp (HU-SH-05).

Al terminar un alta (Embedded Signup, BYOK o manual) el negocio quiere ver que su número
envía. Fuera de la ventana de 24 h Meta solo deja escribir con una plantilla aprobada, así
que la prueba manda una plantilla **aprobada y sin variables** de la WABA de la conexión
(`hello_world` si está, la que Meta crea en las WABAs nuevas) a un número que escribe el
admin. Con las credenciales de esa conexión (`credentials_for`, ADR-0094).

- Solo una conexión viva y operativa (`connected` o `degraded`): una pausada, eliminada o a
  medias no envía (ADR-0122).
- Un envío cada 30 s y como mucho 10 al día por conexión: cada uno es un mensaje que Meta le
  cobra al negocio y que cuenta para la calidad del número.
- En producción, solo el propietario del negocio o el master (la ruta lo exige, ADR-0127).
- Queda en la auditoría (quién, cuándo, plantilla y los últimos 4 dígitos del destino); el
  número de destino no se guarda ni se loguea entero.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from core.db.models import MessageTemplate, MetaConnection, PhoneNumber, WabaAccount
from core.db.session import get_session
from core.gateway.meta_connection_state import get_live_meta_connection
from core.shared.logger import get_logger

log = get_logger(__name__)

ESTADOS_QUE_ENVIAN = ("connected", "degraded")
ESPERA_ENTRE_PRUEBAS_S = 30
PRUEBAS_POR_DIA = 10
_PREFERIDA = "hello_world"


class MensajePruebaError(Exception):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class PlantillaDePrueba:
    name: str
    language: str


@dataclass(frozen=True)
class OpcionesDePrueba:
    connection_id: uuid.UUID
    status: str
    phone_number_id: str | None
    display_phone_number: str | None
    templates: list[PlantillaDePrueba]
    blocked_reason: str | None


def _sin_variables(components: Any) -> bool:
    """¿Se puede enviar la plantilla sin parámetros? Sin `{{n}}` en ningún texto y sin
    encabezado de imagen, video o documento (esos piden el archivo)."""
    if not components:
        return True
    if not isinstance(components, list):
        return False
    for comp in components:
        if not isinstance(comp, dict):
            return False
        formato = str(comp.get("format") or "TEXT").upper()
        if str(comp.get("type") or "").upper() == "HEADER" and formato != "TEXT":
            return False
        if "{{" in str(comp):
            return False
    return True


async def _cargar(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[MetaConnection, PhoneNumber | None, list[MessageTemplate]]:
    async with get_session() as session:
        connection = await get_live_meta_connection(session, connection_id, tenant_id)
        if connection is None or connection.channel != "whatsapp":
            raise MensajePruebaError("Conexión no encontrada", code="not_found", status=404)
        phone = (
            await session.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.tenant_id == tenant_id,
                    PhoneNumber.connection_id == connection.id,
                    PhoneNumber.deleted_at.is_(None),
                )
                .order_by(PhoneNumber.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        plantillas = (
            await session.execute(
                select(MessageTemplate)
                .join(WabaAccount, WabaAccount.id == MessageTemplate.waba_account_id)
                .where(
                    MessageTemplate.tenant_id == tenant_id,
                    WabaAccount.tenant_id == tenant_id,
                    WabaAccount.connection_id == connection.id,
                    WabaAccount.deleted_at.is_(None),
                    MessageTemplate.status == "APPROVED",
                )
                .order_by(MessageTemplate.name, MessageTemplate.language)
                .limit(200)
            )
        ).scalars().all()
    return connection, phone, [t for t in plantillas if _sin_variables(t.components)]


def _motivo_bloqueo(connection: MetaConnection, phone: PhoneNumber | None, plantillas: list[Any]) -> str | None:
    if connection.status not in ESTADOS_QUE_ENVIAN:
        return f"La conexión todavía no está conectada (estado: {connection.status})."
    if phone is None:
        return "La conexión no tiene un número."
    if not plantillas:
        return (
            "No hay plantillas aprobadas sin variables en esta cuenta de WhatsApp. Sincroniza o crea una "
            "en Plantillas (Meta crea «hello_world» en las cuentas nuevas)."
        )
    return None


async def entorno_de(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """Entorno de la conexión (404 si no es de este negocio): la ruta decide si exige dueño."""
    connection, _phone, _plantillas = await _cargar(tenant_id, connection_id)
    return connection.environment


async def opciones_de_prueba(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> OpcionesDePrueba:
    """Lo que el hub necesita para ofrecer la prueba, sin llamar a Meta."""
    connection, phone, plantillas = await _cargar(tenant_id, connection_id)
    ordenadas = sorted(plantillas, key=lambda t: (t.name != _PREFERIDA, t.name, t.language))
    return OpcionesDePrueba(
        connection_id=connection.id,
        status=connection.status,
        phone_number_id=phone.phone_number_id if phone else None,
        display_phone_number=phone.display_phone_number if phone else None,
        templates=[PlantillaDePrueba(t.name, t.language) for t in ordenadas],
        blocked_reason=_motivo_bloqueo(connection, phone, plantillas),
    )


def normalizar_destino(to: str) -> str | None:
    """Número de destino en dígitos (E.164 sin `+`), o `None` si no lo parece."""
    digitos = "".join(ch for ch in to if ch not in " +-().")
    if not (8 <= len(digitos) <= 15 and digitos.isascii() and digitos.isdigit()):
        return None
    return digitos


async def enviar_mensaje_de_prueba(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    to: str,
    *,
    template_name: str | None = None,
    language: str | None = None,
    admin_id: uuid.UUID | None = None,
) -> tuple[str, PlantillaDePrueba]:
    """Envía la plantilla de prueba. Devuelve el id del mensaje en Meta y la plantilla usada."""
    from core.gateway.meta_credentials import CredentialsUnavailable, credentials_for
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_graph import graph_fetch
    from core.memory.working import get_redis

    destino = normalizar_destino(to)
    if destino is None:
        raise MensajePruebaError("Escribe el número de destino con el indicativo del país (solo dígitos).", code="invalid_to")

    connection, phone, plantillas = await _cargar(tenant_id, connection_id)
    motivo = _motivo_bloqueo(connection, phone, plantillas)
    if motivo is not None or phone is None:
        raise MensajePruebaError(motivo or "La conexión no tiene un número.", code="cannot_send", status=409)

    if template_name:
        elegida = next(
            (t for t in plantillas if t.name == template_name and (language is None or t.language == language)), None
        )
        if elegida is None:
            raise MensajePruebaError("Esa plantilla no está aprobada o tiene variables.", code="invalid_template")
    else:
        elegida = next((t for t in plantillas if t.name == _PREFERIDA), plantillas[0])

    redis = get_redis()
    if not await redis.set(f"mensaje_prueba:{tenant_id}:{connection.id}", "1", nx=True, ex=ESPERA_ENTRE_PRUEBAS_S):
        raise MensajePruebaError(
            "Espera unos segundos antes de enviar otra prueba.", code="too_many_tests", status=429
        )
    clave_dia = f"mensaje_prueba_dia:{tenant_id}:{connection.id}"
    enviados = await redis.incr(clave_dia)
    if enviados == 1:
        await redis.expire(clave_dia, 24 * 3600)
    if enviados > PRUEBAS_POR_DIA:
        raise MensajePruebaError(
            f"Ya enviaste {PRUEBAS_POR_DIA} pruebas hoy con esta conexión: vuelve a intentarlo mañana.",
            code="daily_limit",
            status=429,
        )

    try:
        creds = await credentials_for(connection)
    except CredentialsUnavailable as exc:
        raise MensajePruebaError(f"La conexión no tiene credenciales listas: {exc}"[:200], code="no_credentials", status=409) from exc

    try:
        data = await graph_fetch(
            f"/{phone.phone_number_id}/messages",
            method="POST",
            token=creds.access_token,
            app_secret=creds.app_secret,
            body={
                "messaging_product": "whatsapp",
                "to": destino,
                "type": "template",
                "template": {"name": elegida.name, "language": {"code": elegida.language}},
            },
            version=creds.graph_api_version,
            connection_id=connection.id,
        )
    except GraphError as exc:
        log.info("mensaje_prueba_rechazado", connection_id=str(connection.id), code=exc.code)
        raise MensajePruebaError(f"Meta no envió la prueba: {exc.message}"[:200], code="meta_error") from exc

    mensajes = (data or {}).get("messages") or [{}]
    message_id = str(mensajes[0].get("id") or "")
    log.info(
        "mensaje_prueba_enviado",
        connection_id=str(connection.id),
        admin_id=str(admin_id) if admin_id else None,
        template=elegida.name,
        destino_final=destino[-4:],
    )
    from core.shared.audit import record_audit

    await record_audit(
        f"admin:{admin_id}" if admin_id else "admin",
        "meta.test_message",
        input_data={"connection_id": str(connection.id), "template": elegida.name, "to_last4": destino[-4:]},
        output_data={"message_id": message_id},
    )
    return message_id, PlantillaDePrueba(elegida.name, elegida.language)
