"""Tarea `core.twilio.costo_mensaje` (ADR-0186 §11): guarda el costo de un mensaje de Twilio en estado final.

Se encola desde el webhook de estados con un minuto de espera (el precio "may not be immediately available"). Sin
precio todavía o con Twilio caído, se reintenta: a los 5, 10 y 15 minutos. Agotados los intentos, el mensaje queda
sin costo (se loguea): nunca bloquea nada.
"""

from __future__ import annotations

import uuid

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.celery_app import celery

log = get_logger(__name__)

MAX_INTENTOS = 3
_ESPERA_S = 300
TAREA = "core.twilio.costo_mensaje"


@celery.task(bind=True, max_retries=MAX_INTENTOS, default_retry_delay=_ESPERA_S, name=TAREA)
def costo_mensaje(self, tenant_id: str, connection_id: str, message_sid: str) -> str:
    from core.gateway.twilio.costos import registrar_costo

    async def _correr() -> str:
        tid = uuid.UUID(tenant_id)
        async with tenant_scope(tid):
            return await registrar_costo(tid, uuid.UUID(connection_id), message_sid)

    resultado = run_async(_correr())
    if resultado in ("sin_precio", "error_transitorio"):
        if self.request.retries >= MAX_INTENTOS:
            log.warning("twilio_costo_sin_precio_final", tenant_id=tenant_id, resultado=resultado)
            return resultado
        raise self.retry(countdown=_ESPERA_S * (self.request.retries + 1))
    return resultado


def encolar_costo(tenant_id: uuid.UUID, connection_id: uuid.UUID, message_sid: str) -> None:
    """Best-effort: si el broker no responde, el mensaje se queda sin costo; el webhook no falla por eso."""
    try:
        costo_mensaje.apply_async(
            args=[str(tenant_id), str(connection_id), message_sid], countdown=60
        )
    except Exception:
        log.warning("twilio_costo_no_encolado", tenant_id=str(tenant_id), exc_info=True)
