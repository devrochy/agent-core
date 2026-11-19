"""Envío por Messenger e Instagram (Send API) con la ventana de servicio y las etiquetas de mensajería.

Reglas verificadas contra la referencia de Send API de Meta el 2026-10-23 (ADR-0152 §7):

- **Ventana estándar de 24 h** desde el último mensaje del cliente: se responde con
  `messaging_type = RESPONSE`.
- **Fuera de la ventana** solo se puede con la etiqueta `HUMAN_AGENT`: hasta 7 días desde el último
  mensaje del cliente y únicamente para que **una persona** responda; los mensajes automatizados no
  están permitidos. Exige el permiso «Human Agent» de App Review.
- `CUSTOMER_FEEDBACK` solo vale con su plantilla propia, que aquí no se envía: no se admite.
- `CONFIRMED_EVENT_UPDATE`, `ACCOUNT_UPDATE` y `POST_PURCHASE_UPDATE` están retirados en 2026 (las
  fuentes discrepan en la fecha: 10-feb o 27-abr). Nunca se envían.

Todo se decide ANTES de llamar a Meta: un envío que ya se sabe que va a fallar no gasta una llamada
ni un reintento de la cola. La decisión (`plan_de_envio`) es pura y se prueba sin red.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from core.shared.exceptions import AgentCoreError

SERVICE_WINDOW_HOURS = 24
HUMAN_AGENT_DAYS = 7
HUMAN_AGENT_TAG = "HUMAN_AGENT"
# Etiquetas que este módulo puede enviar. Agregar otra exige confirmarla contra la doc primaria de Meta.
ALLOWED_MESSAGE_TAGS = frozenset({HUMAN_AGENT_TAG})

PAGE_TOKEN_KIND = "page_access"

# Nombre de cada canal en los mensajes que ve el usuario (las reglas de ventana son las mismas).
_NOMBRE_DE_CANAL = {"messenger": "Messenger", "instagram": "Instagram"}


class MessengerSendError(AgentCoreError):
    """Envío bloqueado antes de llamar a Meta: fuera de la ventana y sin derecho a `HUMAN_AGENT`."""


@dataclass(frozen=True)
class PlanDeEnvio:
    """Cómo se manda el mensaje: `RESPONSE`, o `MESSAGE_TAG` con su etiqueta."""

    messaging_type: str
    tag: str | None = None


def plan_de_envio(
    *,
    service_window_expires_at: datetime | None,
    humano: bool,
    ahora: datetime | None = None,
    canal: str = "messenger",
) -> PlanDeEnvio:
    """Decide el tipo de mensaje o lanza `MessengerSendError`.

    `service_window_expires_at`: fin de la ventana de 24 h, que cada mensaje del cliente renueva
    (`last_inbound + 24 h`). `None` es una conversación sin ventana registrada: se responde como
    dentro de la ventana, igual que WhatsApp (`validate_service_window`).
    `humano`: lo envía una persona desde la bandeja, nunca el asistente.
    `canal`: `messenger` o `instagram`, solo para nombrarlo en el error."""
    ahora = ahora or datetime.now(UTC)
    nombre = _NOMBRE_DE_CANAL.get(canal, "Messenger")
    if service_window_expires_at is None or service_window_expires_at >= ahora:
        return PlanDeEnvio("RESPONSE")
    # `HUMAN_AGENT` cubre 7 días desde el último mensaje del cliente: la ventana ya contó 1 de ellos.
    limite = service_window_expires_at - timedelta(hours=SERVICE_WINDOW_HOURS) + timedelta(days=HUMAN_AGENT_DAYS)
    if not humano:
        raise MessengerSendError(
            f"La ventana de 24 h de {nombre} venció: el asistente no puede escribir. "
            "Solo una persona puede responder hasta 7 días después del último mensaje del cliente."
        )
    if ahora > limite:
        raise MessengerSendError(
            f"Pasaron más de 7 días desde el último mensaje del cliente: {nombre} ya no permite responderle."
        )
    return PlanDeEnvio("MESSAGE_TAG", HUMAN_AGENT_TAG)
