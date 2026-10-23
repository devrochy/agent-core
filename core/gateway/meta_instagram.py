"""Envío por Instagram con Facebook Login (ADR-0152, HU-CHAN-04): reglas propias del canal.

La cuenta profesional de Instagram está vinculada a una Página de Facebook y se escribe con el token de
esa Página: `POST /{page_id}/messages` con el IGSID del cliente como destinatario. La ventana de 24 h y
`HUMAN_AGENT` (7 días, solo una persona) son las mismas de Messenger y las decide
`meta_messenger.plan_de_envio`; aquí solo viven los límites que Instagram agrega.

- **Texto: 1000 bytes UTF-8** (no caracteres: una vocal con tilde pesa 2). Un texto más largo se rechaza
  antes de llamar a Meta, que lo rechazaría sin que reintentar sirva.
- Sin contacto en frío: la conversación nace cuando el cliente escribe primero, así que siempre hay
  ventana registrada.
- La ruta por Instagram Login (otra app de Meta y otros permisos) no está en esta entrega.
"""

from __future__ import annotations

from core.shared.exceptions import AgentCoreError

MAX_TEXT_BYTES = 1000
LOGIN_ROUTE_FACEBOOK = "ig_facebook_login"


class InstagramSendError(AgentCoreError):
    """Envío bloqueado antes de llamar a Meta por un límite propio de Instagram."""


def validar_texto(text: str) -> None:
    """Lanza `InstagramSendError` si el texto no cabe en un mensaje de Instagram (1000 bytes UTF-8)."""
    if not text.strip():
        raise InstagramSendError("No se puede enviar un mensaje vacío por Instagram.")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise InstagramSendError(
            f"El mensaje supera los {MAX_TEXT_BYTES} bytes que Instagram permite por mensaje: acórtalo o divídelo."
        )


def igsid_de(customer_external_id: str) -> str:
    """El IGSID del cliente a partir de su clave: `instagram:<ig_user_id>:<IGSID>` (o `instagram:<IGSID>`)."""
    return customer_external_id.rsplit(":", 1)[-1]
