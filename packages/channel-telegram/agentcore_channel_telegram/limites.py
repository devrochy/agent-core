"""Límites de la Bot API de Telegram (ADR-0199). Leídos en https://core.telegram.org/bots/api (v10.4, 2026-11-15).

El core ya parte el texto largo en burbujas (`message_splitter`): aquí solo están los topes y las comprobaciones
que dependen de ellos.
"""

from __future__ import annotations

import re
import secrets

# `sendMessage.text`: 1-4096 caracteres después de procesar entidades (sin `parse_mode` en v1, texto plano).
TEXTO_MAX = 4096
# Caption de `sendPhoto` / `sendVoice`: 0-1024 caracteres.
CAPTION_MAX = 1024
# `InlineKeyboardButton.callback_data`: 1-64 BYTES (no caracteres).
CALLBACK_DATA_MAX_BYTES = 64
# `answerCallbackQuery.text`: 0-200 caracteres.
CALLBACK_ANSWER_MAX = 200
# `getFile`: un bot solo descarga archivos de hasta 20 MB.
ARCHIVO_DESCARGA_MAX = 20 * 1024 * 1024
# `sendVoice`: hasta 50 MB (OGG con Opus, MP3 o M4A).
VOZ_MAX = 50 * 1024 * 1024
# `setWebhook.secret_token`: Telegram admite 1-256 caracteres de `A-Z`, `a-z`, `0-9`, `_` y `-`. Es lo único que
# autentica el webhook (el `public_id` va en la URL), así que se exigen al menos 32.
SECRETO_WEBHOOK = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
# Ritmo de salida: Telegram admite unos 30 mensajes por segundo por bot y 1 por segundo por chat. Se deja margen.
RITMO_BOT_POR_SEGUNDO = 25
RITMO_CHAT_POR_SEGUNDO = 1
# Lo que el webhook acepta (`setWebhook.allowed_updates`): los demás tipos no llegan.
ALLOWED_UPDATES = ("message", "callback_query")


def cabe_callback_data(valor: str) -> bool:
    """¿Cabe como `callback_data`? (1-64 bytes en UTF-8)."""
    return 1 <= len(valor.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES


def secreto_webhook_valido(valor: object) -> bool:
    return isinstance(valor, str) and bool(SECRETO_WEBHOOK.match(valor))


def nuevo_secreto_webhook() -> str:
    """Secreto aleatorio para `setWebhook.secret_token`: 256 bits en base64 URL (43 caracteres del alfabeto
    permitido)."""
    return secrets.token_urlsafe(32)
