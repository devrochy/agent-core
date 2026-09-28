"""Modelos compartidos de la API `/api/v1` (ADR-0077)."""

from __future__ import annotations

import uuid

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
