"""Plantillas de Twilio en uso (ADR-0186 §8, PR-7a): las aprobadas de la conexión de una conversación, para el
selector del chat y los avisos por evento (ADR-0108).

El camino de Meta (`event_notices.approved_template_for`, `build_components`) no cambia: estas funciones solo se usan
cuando la conversación es de una conexión de Twilio. Un mensaje de plantilla de Twilio se encola igual que uno de
Meta (`send_template_to_conversation`): `template_name` es el nombre legible (lo muestran el panel y la línea de
tiempo y lo cuenta el crédito) y `template_components` lleva UN elemento `twilio_content` con el `ContentSid` y las
variables, que `outbound_send` le pasa a `send_content_template`.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select, text

from core.db.models import TwilioContentTemplate
from core.db.session import get_session
from core.kernel.mensajes.saneo import clean_param

TIPO_COMPONENTE = "twilio_content"
_PLACEHOLDER = re.compile(r"\{\{\s*(\d{1,3})\s*\}\}")
_SID_HX = re.compile(r"^HX[0-9a-f]{32}$")
# Solo las de texto (`twilio/text`): una con botones o media enviaría algo distinto de lo que se muestra (PR-7i).
_SOLO_TEXTO = text(
    "twilio_content_templates.types = jsonb_build_object('twilio/text', twilio_content_templates.types -> 'twilio/text')"
)


async def es_de_twilio(conversation: Any) -> bool:
    from core.gateway import avisos_canal as ac

    if getattr(conversation, "connection_id", None) is None:
        return False
    return await ac.proveedor_de_conversacion(conversation) == "twilio"


def placeholders(cuerpo: str) -> list[str]:
    """Variables `{{n}}` del cuerpo, como texto y en orden (`["1", "2"]`), las claves de `resolve_values`."""
    return [str(n) for n in sorted({int(m) for m in _PLACEHOLDER.findall(cuerpo or "")})]


def componentes(fila: TwilioContentTemplate, valores: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "type": TIPO_COMPONENTE,
            "content_sid": fila.content_sid,
            # Las mismas reglas de parámetro de WhatsApp que en Meta (`clean_param`): sin saltos ni espacios de más,
            # largo acotado. La vista previa usa los mismos valores.
            "variables": {
                k: clean_param(str(valores[k])) for k in placeholders(fila.body) if k in valores
            },
        }
    ]


def contenido_de(components: Any) -> tuple[str, dict[str, str]] | None:
    """`(ContentSid, variables)` del mensaje encolado, o `None` si no es una plantilla de Twilio bien formada."""
    if (
        not isinstance(components, list)
        or len(components) != 1
        or not isinstance(components[0], dict)
    ):
        return None
    c = components[0]
    sid = str(c.get("content_sid") or "")
    if c.get("type") != TIPO_COMPONENTE or not _SID_HX.match(sid):
        return None
    variables = c.get("variables") if isinstance(c.get("variables"), dict) else {}
    return sid, {str(k): str(v) for k, v in variables.items()}


def vista_previa(fila: TwilioContentTemplate, valores: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(
        lambda m: clean_param(str(valores[m.group(1)])) if m.group(1) in valores else m.group(0),
        fila.body or "",
    )


async def aprobadas(conversation: Any) -> list[TwilioContentTemplate]:
    """Las aprobadas de la conexión de la conversación (del mismo negocio), por nombre."""
    connection_id = getattr(conversation, "connection_id", None)
    if connection_id is None:
        return []
    return await aprobadas_de_conexion(conversation.tenant_id, connection_id)


async def aprobadas_de_conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> list[TwilioContentTemplate]:
    """Las aprobadas de una conexión de ese negocio, por nombre (también las usa el envío a segmentos, PR-7b)."""
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TwilioContentTemplate)
                    .where(
                        TwilioContentTemplate.tenant_id == tenant_id,
                        TwilioContentTemplate.channel_connection_id == connection_id,
                        TwilioContentTemplate.approval_status == "approved",
                        TwilioContentTemplate.content_sid.is_not(None),
                        TwilioContentTemplate.deleted_at.is_(None),
                        _SOLO_TEXTO,
                    )
                    .order_by(
                        TwilioContentTemplate.friendly_name,
                        TwilioContentTemplate.language,
                        TwilioContentTemplate.id,
                    )
                )
            ).scalars()
        )


async def aprobada(conversation: Any, nombre: str, idioma: str) -> TwilioContentTemplate | None:
    return next(
        (
            f
            for f in await aprobadas(conversation)
            if f.friendly_name == nombre and f.language == idioma
        ),
        None,
    )


async def aprobada_por_id(
    conversation: Any, plantilla_id: uuid.UUID
) -> TwilioContentTemplate | None:
    return next((f for f in await aprobadas(conversation) if f.id == plantilla_id), None)


async def del_negocio(
    tenant_id: uuid.UUID, *, solo_aprobadas: bool = True
) -> list[TwilioContentTemplate]:
    """Las del negocio en todas sus conexiones de Twilio (para configurar el mapa de avisos por evento)."""
    async with get_session() as session:
        stmt = select(TwilioContentTemplate).where(
            TwilioContentTemplate.tenant_id == tenant_id,
            TwilioContentTemplate.deleted_at.is_(None),
            _SOLO_TEXTO,
        )
        if solo_aprobadas:
            stmt = stmt.where(TwilioContentTemplate.approval_status == "approved")
        return list(
            (await session.execute(stmt.order_by(TwilioContentTemplate.friendly_name))).scalars()
        )


async def enviable(tenant_id: uuid.UUID, connection_id: uuid.UUID | None, content_sid: str) -> bool:
    """¿El `ContentSid` encolado sigue vivo, aprobado y de texto en la conexión ACTUAL de la conversación?

    La plantilla se elige contra la conexión de la conversación al encolarla, pero el envío lee de nuevo
    `conversation.connection_id` (que un entrante por otro número del negocio puede cambiar) y la plantilla puede
    pausarse o borrarse mientras espera. Sin esta comprobación saldría el HX de una conexión por el número de otra."""
    if connection_id is None:
        return False
    async with get_session() as session:
        fila = (
            await session.execute(
                select(TwilioContentTemplate.id).where(
                    TwilioContentTemplate.tenant_id == tenant_id,
                    TwilioContentTemplate.channel_connection_id == connection_id,
                    TwilioContentTemplate.content_sid == content_sid,
                    TwilioContentTemplate.approval_status == "approved",
                    TwilioContentTemplate.deleted_at.is_(None),
                    _SOLO_TEXTO,
                )
            )
        ).first()
    return fila is not None
