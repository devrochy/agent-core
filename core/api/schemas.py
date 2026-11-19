"""Modelos compartidos de la API `/api/v1` (ADR-0077)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class TenantOption(BaseModel):
    """Tenant al que el admin tiene acceso (selector de negocio)."""

    id: uuid.UUID
    name: str
    role: str
    active: bool


class UsageMetric(BaseModel):
    """Consumo + límite de una métrica del plan (barras de progreso).

    `used`/`limit` son `float` porque las métricas de costo (USD) traen decimales.
    """

    key: str
    label: str
    unit: str
    scope: str
    used: float
    limit: float | None
    percent: float | None
    exceeded: bool
    remaining: float | None = None


# ------------------------------------------------ checklist de producción (HU-CONN-07)
class ReadinessItem(BaseModel):
    """Un ítem del checklist. `ok`: `None` = no se pudo evaluar."""

    key: str
    label: str
    ok: bool | None = None
    detail: str | None = None
    repair_action: str | None = None
    checked_at: datetime | None = None


class ReadinessRun(BaseModel):
    """Última corrida guardada. `status`: `never` (nunca se revisó), `queued`, `running`,
    `done` o `failed`. `ready` solo es `true` con todos los ítems en verde."""

    run_id: uuid.UUID | None = None
    status: Literal["never", "queued", "running", "done", "failed"] = "never"
    ready: bool | None = None
    items: list[ReadinessItem] = []
    error: str | None = None
    requested_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def readiness_run_out(run: Any | None) -> dict[str, Any]:
    """Campos de `ReadinessRun` a partir de una fila (de conexión o de plataforma)."""
    from core.admin.production_readiness_service import effective_status

    if run is None:
        return {}
    status, error = effective_status(run.status, run.requested_at, run.error)
    return {
        "run_id": run.id,
        "status": status,
        "ready": run.ready if status == "done" else None,
        "items": [ReadinessItem(**item) for item in (run.checks or [])],
        "error": error,
        "requested_at": run.requested_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }
