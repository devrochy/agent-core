"""Sentry: integración OPCIONAL, mismo patrón que `tracing.py` (OTel) --
sin `SENTRY_ENABLED`/`SENTRY_DSN`, `configure_sentry()` es no-op.

`_before_send` redacta tokens de Meta (`EAA...`) como RED DE ÚLTIMA
INSTANCIA -- el código de este repo ya no debería mandarle un token a
Sentry por construcción (`graph_fetch` solo pone el token en el header
Authorization, nunca en un valor que termine en un evento capturado), pero
un event de excepción puede arrastrar un `locals()` con el token en algún
frame. Esto es la segunda línea de defensa, no la primera.
"""

from __future__ import annotations

import re
from typing import Any

from core.config import get_settings

_META_TOKEN_RE = re.compile(r"EAA[A-Za-z0-9]{20,}")
# Claves que se tapan además de las de sentry-sdk (`token`, `secret`, `password`...), en el
# cuerpo del request y en las variables de cada frame, también dentro de dicts anidados: el
# PIN de verificación en dos pasos de un número viaja en el cuerpo de
# `POST .../numbers/{id}/register` y en locales como `pin`, `previous_pin` o `body` (el modelo
# del request lo lleva como `SecretStr`, que no sale en su repr).
_EXTRA_DENYLIST = [
    "pin", "new_pin", "previous_pin", "app_secret", "access_token", "verify_token",
    # Embedded Signup (HU-SH-05): el canje lleva `client_secret` y `code` en `params`; el token
    # de app es "app_id|app_secret"; los PIN del wizard viven en `nuevo` y `guardado`.
    "client_secret", "code", "codigo", "attempt", "app_token", "input_token", "params", "nuevo", "guardado",
    # Tools declarativas (HU-AOS-12): headers sensibles y los locales que los llevan (el spec
    # v2 todavía puede tener literales en `extensions.tools`; los secretos de la instalación
    # `core` se descifran en `valores`/`actuales`).
    "authorization", "cookie", "x-api-key", "x_api_key", "headers", "extensions", "secretos",
    "valores", "actuales", "nuevos",
]
_configured = False


def _redact_meta_tokens(text: str) -> str:
    return _META_TOKEN_RE.sub("[redacted]", text)


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    import json

    try:
        raw = json.dumps(event)
    except (TypeError, ValueError):
        return event
    redacted = _redact_meta_tokens(raw)
    if redacted == raw:
        return event
    try:
        return json.loads(redacted)
    except (TypeError, ValueError):
        return event


def _event_scrubber():  # type: ignore[no-untyped-def]
    from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber

    return EventScrubber(denylist=[*DEFAULT_DENYLIST, *_EXTRA_DENYLIST], recursive=True)


def configure_sentry() -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    if not settings.sentry_enabled or not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn, before_send=_before_send, send_default_pii=False, event_scrubber=_event_scrubber()
    )
    _configured = True
