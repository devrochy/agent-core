"""Correo de SISTEMA por SMTP (ADR-0098).

Distinto del canal SMTP por tenant (`core/gateway/channels/email.py`, que modela
un `Message` dentro de un negocio): este correo sale de la plataforma misma,
para mensajes sin contexto de tenant como la recuperación de contraseña.

Sin `SYSTEM_SMTP_HOST` configurado no envía nada y solo registra un aviso
(mismo criterio que las alertas de Slack: un canal no configurado no rompe el
flujo). En desarrollo/test nunca se envía correo real.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)


def _send_sync(host: str, port: int, username: str, password: str, from_address: str, to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["From"] = from_address
    msg["To"] = to
    msg["Subject"] = subject.replace("\r", " ").replace("\n", " ")
    msg["Message-ID"] = make_msgid()
    msg.set_content(body)

    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
    return str(msg["Message-ID"])


async def send_system_email(to: str, subject: str, body: str) -> str | None:
    """Envía (o solo registra) un correo de sistema. Devuelve el message-id o None.

    `None` = no se intentó el envío (SMTP de sistema sin configurar).
    Lanza `OSError`/`smtplib.SMTPException` si el envío falla: el llamador decide.
    """
    settings = get_settings()
    if not settings.system_smtp_host or not settings.system_smtp_from:
        from core.shared.texto_seguro import enmascarar_correo

        # Sin SMTP el correo no sale: el log no expone la dirección entera (ADR-0121).
        log.warning("system_email_skipped", to=enmascarar_correo(to), subject=subject[:80])
        return None
    return await asyncio.to_thread(
        _send_sync,
        settings.system_smtp_host,
        settings.system_smtp_port,
        settings.system_smtp_username,
        settings.system_smtp_password,
        settings.system_smtp_from,
        to,
        subject,
        body,
    )
