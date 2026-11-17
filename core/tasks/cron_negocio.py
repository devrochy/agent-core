"""Ayudas para los crons de negocio (ADR-0201).

Cada cron de negocio, dentro de su `tenant_scope`, pregunta `debe_correr()` y, si hizo trabajo,
deja el resumen con `registrar()`. La política (encendido/apagado, hora local y ventana) vive en
`core/admin/crons_negocio_service.py`; aquí solo se ofrece una puerta corta para los jobs.

El registro es best-effort: un fallo de la base no tumba el cron. El historial detallado
(`tenant_cron_runs`) se agrega en la fase de historial y solo guarda ejecuciones con efecto.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from core.admin import crons_negocio_service as svc


async def debe_correr(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    ahora: datetime | None = None,
) -> bool:
    """¿La política del negocio permite ejecutar este cron ahora?"""
    return (await svc.politica(tenant_id, cron_name, ahora=ahora)).debe_correr


async def motivo(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    ahora: datetime | None = None,
) -> str | None:
    """Motivo de omisión (`deshabilitado_por_negocio`, `fuera_de_hora`, `fuera_de_ventana`) o None."""
    return (await svc.politica(tenant_id, cron_name, ahora=ahora)).motivo


async def registrar(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    acciones: int = 0,
    resumen: str | None = None,
    status: str = "success",
    forzar: bool = False,
    trigger: str = "scheduled",
) -> None:
    """Deja el resumen de la última ejecución y, si tuvo efecto, la fila del historial."""
    await svc.registrar_ultima(
        tenant_id, cron_name, status=status, acciones=acciones, resumen=resumen, forzar=forzar
    )
    await svc.registrar_ejecucion(
        tenant_id, cron_name, status=status, acciones=acciones, resumen=resumen, trigger=trigger
    )


async def registrar_fallo(
    tenant_id: uuid.UUID,
    cron_name: str,
    *,
    error_class: str,
    resumen: str | None = None,
) -> None:
    """Un negocio que falló: queda en su historial (la tarjeta y el master lo ven)."""
    await svc.registrar_ultima(
        tenant_id, cron_name, status="failed", acciones=0, resumen=resumen or "Falló"
    )
    await svc.registrar_ejecucion(
        tenant_id, cron_name, status="failed", acciones=0, resumen=resumen, error_class=error_class
    )
