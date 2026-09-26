"""Adaptador saliente de correo por SMTP (ADR-0071).

Solo salida: un correo que dispara el asistente (p. ej. la confirmación de una
cita) se modela como un `Message` del canal `email` y se envía por SMTP,
reutilizando la cola/reintentos/DLQ de `outbound_send`. El `Message` no tiene
columna de asunto: viaja como primera línea `Subject: …` del contenido y este
adaptador la separa del cuerpo.

Sin dependencias nuevas: usa `smtplib` de la stdlib en un hilo.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid
from typing import Any

from core.gateway.channels.base import OutboundAdapter
from core.shared.logger import get_logger

log = get_logger(__name__)

SUBJECT_PREFIX = "Subject:"


def single_recipient(value: str) -> str | None:
    """Normaliza a UN solo email válido, o None.

    Rechaza listas (`a@x.com, b@y.com`), `;` y CR/LF: `smtplib.send_message`
    enviaría a TODAS las direcciones de los headers (abuso de la cuenta).
    """
    raw = (value or "").strip()
    if not raw or any(c in raw for c in "\r\n,;"):
        return None
    parsed = getaddresses([raw])
    if len(parsed) != 1:
        return None
    _name, addr = parsed[0]
    addr = (addr or "").strip()
    if not addr or "@" not in addr:
        return None
    return addr


def split_subject(text: str) -> tuple[str, str]:
    """Separa `"Subject: X\\n\\ncuerpo"` en (asunto, cuerpo)."""
    stripped = text.lstrip()
    if stripped[: len(SUBJECT_PREFIX)].lower() == SUBJECT_PREFIX.lower():
        first, _, rest = stripped.partition("\n")
        subject = first[len(SUBJECT_PREFIX):].strip()
        return subject or "(sin asunto)", rest.lstrip("\n")
    return "(sin asunto)", text


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "true", "on", "yes")


def _send_sync(
    credentials: dict[str, Any], to_external_id: str, subject: str, body: str
) -> str:
    host = str(credentials.get("host", "") or "")
    port = int(credentials.get("port", 587) or 587)
    username = str(credentials.get("username", "") or "")
    password = str(credentials.get("password", "") or "")
    from_address = str(credentials.get("from_address", "") or "") or username
    use_tls = _truthy(credentials.get("use_tls", "true"))
    if not host or not from_address:
        raise ValueError("Conexión de correo incompleta (falta host o remitente)")

    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to_external_id
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_tls:
                smtp.starttls(context=ssl.create_default_context())
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    return str(msg["Message-ID"])


class EmailSmtpOutbound(OutboundAdapter):
    provider = "smtp"

    async def send_text(
        self, credentials: dict[str, Any], to_external_id: str, text: str
    ) -> str:
        subject, body = split_subject(text)
        return await asyncio.to_thread(_send_sync, credentials, to_external_id, subject, body)

    async def send_image(
        self,
        credentials: dict[str, Any],
        to_external_id: str,
        image_url: str,
        caption: str = "",
    ) -> str:
        raise NotImplementedError("El canal de correo todavía no adjunta imágenes")
