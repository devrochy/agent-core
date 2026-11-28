"""Tarea `core.feedback.aviso_reporte_nuevo` (SPEC-54, HU-FB-04, ADR-0213 §7.6): avisa al equipo de SolversHub
que entró un reporte de cliente.

El centro de notificaciones (ADR-0167) es por negocio y no tiene audiencia de plataforma, así que el aviso es
un correo de sistema (`send_system_email`) a `FEEDBACK_AVISO_EMAIL`. Se encola después del commit del envío;
si el broker no responde, el reporte igual queda en la bandeja. Si el SMTP falla, se reintenta 3 veces y luego
solo se registra. El correo lleva el negocio, el tipo y el área; el título y la descripción se leen en el
panel (son texto del cliente).
"""

from __future__ import annotations

import uuid

from core.shared.async_bridge import run_async
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.tasks.celery_app import celery

log = get_logger(__name__)

MAX_INTENTOS = 3
_ESPERA_S = 120
TAREA = "core.feedback.aviso_reporte_nuevo"


async def enviar_aviso(tenant_id: uuid.UUID, report_id: uuid.UUID) -> str:
    """`enviado`, `sin_destino` (sin `FEEDBACK_AVISO_EMAIL`), `sin_smtp`, `no_existe` o lanza si el SMTP falla."""
    from sqlalchemy import select, text

    from core.config import get_settings
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.gateway.system_email import send_system_email

    settings = get_settings()
    destino = settings.feedback_aviso_email.strip()
    if not destino:
        log.info("feedback.aviso_sin_destino", report_id=str(report_id))
        return "sin_destino"
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            fila = (
                await session.execute(
                    text("SELECT tipo, area FROM feedback_reports WHERE id = :id"), {"id": report_id}
                )
            ).first()
            negocio = (await session.execute(select(Tenant.name).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if fila is None:
        log.warning("feedback.aviso_reporte_no_existe", report_id=str(report_id), tenant_id=str(tenant_id))
        return "no_existe"
    enlace = f"{settings.panel_base_url.rstrip('/')}/plataforma/feedback/{report_id}" if settings.panel_base_url else ""
    cuerpo = (
        "Entró un reporte nuevo de un cliente.\n\n"
        f"Negocio: {negocio or tenant_id}\n"
        f"Tipo: {fila.tipo}\n"
        f"Área: {fila.area}\n\n"
        + (f"Revísalo en la bandeja: {enlace}\n" if enlace else "Revísalo en la bandeja de reportes del panel.\n")
    )
    enviado = await send_system_email(destino, f"Reporte nuevo de cliente ({fila.tipo})", cuerpo)
    return "enviado" if enviado is not None else "sin_smtp"


@celery.task(bind=True, max_retries=MAX_INTENTOS, default_retry_delay=_ESPERA_S, name=TAREA)
def aviso_reporte_nuevo(self, tenant_id: str, report_id: str) -> str:
    try:
        return run_async(enviar_aviso(uuid.UUID(tenant_id), uuid.UUID(report_id)))
    except Exception as exc:
        if self.request.retries >= MAX_INTENTOS:
            log.warning("feedback.aviso_fallo_final", report_id=report_id, error=type(exc).__name__)
            return "fallo"
        raise self.retry(countdown=_ESPERA_S * (self.request.retries + 1)) from None


def encolar_aviso(tenant_id: uuid.UUID, report_id: uuid.UUID) -> None:
    """Best-effort, después del commit del envío: si el broker no responde, solo se registra."""
    try:
        aviso_reporte_nuevo.delay(str(tenant_id), str(report_id))
    except Exception:
        log.warning("feedback.aviso_no_encolado", report_id=str(report_id), exc_info=True)


__all__ = ["aviso_reporte_nuevo", "encolar_aviso", "enviar_aviso"]
