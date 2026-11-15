"""Política de respuestas por voz del pack (ADR-0197).

La decisión final es una función PURA sobre los datos del turno. La parte
asíncrona solo lee la configuración y el estado de la conversación; así la
regla se puede probar sin base de datos ni Redis.
"""

from __future__ import annotations

import random
import uuid

from sqlalchemy import select

from core.db.models import ChannelConnection, Conversation, Message
from core.db.session import get_session
from core.mcp.types import VoicePlan
from core.plugin_manager import plugin_manager
from core.shared.logger import get_logger

from .config import VoiceConfig

log = get_logger(__name__)

PLUGIN_ID = "pack_voice"


def decidir_por_reglas(
    config: VoiceConfig,
    *,
    texto: str,
    es_audio_entrante: bool,
    voz_anterior: bool,
    azar: int,
) -> bool:
    """Veredicto puro: ¿este turno va con nota de voz?"""
    if not config.enabled or config.modo == "nunca":
        return False
    if not texto.strip() or len(texto) > config.max_caracteres:
        return False
    if config.solo_si_cliente_manda_audio and not es_audio_entrante:
        return False
    if config.evitar_voz_seguida and voz_anterior:
        return False
    if config.modo == "siempre":
        return True
    return 1 <= azar <= config.probabilidad_voz


async def cargar_config() -> VoiceConfig | None:
    try:
        config = await plugin_manager.load_config(PLUGIN_ID)
    except Exception:
        # Sin fila de configuración (pack instalado pero no configurado) no hay voz.
        return None
    return config if isinstance(config, VoiceConfig) else None


async def _estado_de_la_conversacion(
    conversation_id: uuid.UUID,
) -> tuple[bool, bool, bool] | None:
    """(canal waweb, último inbound es audio, última respuesta fue voz)."""
    async with get_session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.channel != "whatsapp":
            return None
        if conversation.connection_id is None:
            return None
        connection = (
            await session.execute(
                select(ChannelConnection).where(
                    ChannelConnection.id == conversation.connection_id,
                    ChannelConnection.tenant_id == conversation.tenant_id,
                    ChannelConnection.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if connection is None or connection.provider != "waweb":
            return None

        ultimo_in = (
            await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        es_audio = bool(
            ultimo_in is not None
            and str(ultimo_in.media_type or "").startswith("audio/")
        )

        ultimo_out = (
            await session.execute(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "outbound",
                    Message.deleted_at.is_(None),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        voz_anterior = bool(
            ultimo_out is not None
            and str(ultimo_out.media_type or "") in ("audio/ogg", "audio/tts")
        )
    return True, es_audio, voz_anterior


async def decidir(conversation_id: uuid.UUID, texto: str) -> VoicePlan | None:
    config = await cargar_config()
    if config is None or not config.enabled or config.modo == "nunca":
        return None
    try:
        estado = await _estado_de_la_conversacion(conversation_id)
    except Exception:
        log.warning("voice_policy_state_failed", conversation_id=str(conversation_id), exc_info=True)
        return None
    if estado is None:
        return None
    _waweb, es_audio, voz_anterior = estado
    if not decidir_por_reglas(
        config,
        texto=texto,
        es_audio_entrante=es_audio,
        voz_anterior=voz_anterior,
        azar=random.randint(1, 100),
    ):
        return None
    return VoicePlan(include_text=config.incluir_texto)


__all__ = ["PLUGIN_ID", "cargar_config", "decidir", "decidir_por_reglas"]
