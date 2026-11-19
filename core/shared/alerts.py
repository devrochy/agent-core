"""Único punto de entrada de alerting externo del repo -- antes de este
ticket, "esto es grave" significaba únicamente `log.error`/`log.warning`
(ver comentarios en `meta_usage.py`/`webhooks.py`). `send_alert` agrega un
canal real (Slack, vía webhook URL) sin reemplazar el log: SIEMPRE logea,
Slack es best-effort y nunca bloquea al caller que detectó la condición.
"""

from __future__ import annotations

from typing import Literal

import httpx

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 5.0

Severity = Literal["info", "warning", "critical"]

_LOG_FN = {"info": "info", "warning": "warning", "critical": "error"}


async def send_alert(title: str, detail: str, severity: Severity = "warning") -> None:
    log_method = getattr(log, _LOG_FN[severity])
    log_method("alert", title=title, detail=detail, severity=severity)

    webhook_url = get_settings().slack_alerts_webhook_url
    if not webhook_url:
        return

    emoji = {"info": ":information_source:", "warning": ":warning:", "critical": ":rotating_light:"}[severity]
    payload = {"text": f"{emoji} *{title}*\n{detail}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            response = await client.post(webhook_url, json=payload)
        if response.status_code >= 400:
            log.warning("alert_slack_post_fallo", status_code=response.status_code)
    except httpx.HTTPError as exc:
        log.warning("alert_slack_post_error", error=str(exc))


_UNA_VEZ_POR_DIA_S = 24 * 60 * 60


async def send_alert_once(
    key: str, title: str, detail: str, *, severity: Severity = "warning", window_s: int = _UNA_VEZ_POR_DIA_S
) -> None:
    """`send_alert` como mucho una vez por `key` cada `window_s` (por defecto, un día).

    Para condiciones que un cron vuelve a ver en cada corrida (un token vencido, un número
    en RED): sin esto alertan cada hora y terminan silenciando el canal (ADR-0102). Si
    Redis falla, alerta igual: mejor de más que callar una condición real.
    """
    from core.memory.working import get_redis

    try:
        primera_vez = await get_redis().set(f"alerta_una_vez:{key}", "1", nx=True, ex=window_s)
    except Exception:
        log.exception("alerta_una_vez_sin_redis", key=key)
        primera_vez = True
    if primera_vez:
        await send_alert(title, detail, severity=severity)
