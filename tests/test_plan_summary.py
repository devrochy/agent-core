"""Plan del negocio activo en `GET /admin/me` (HU-BILL-15).

El shell del panel y de la app necesitan saber si el negocio está en Free y cuántos días
le quedan sin abrir otra petición ni calcular consumo. `plan_summary` es una lectura
barata de la suscripción: no consulta estado operativo ni métricas.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing import enforcement

TENANT = uuid.uuid4()


def _suscripcion(
    plan_id: str | None,
    status: str,
    *,
    trial_days: int | None = None,
    period_days: int | None = None,
) -> SimpleNamespace:
    ahora = datetime.now(UTC)
    return SimpleNamespace(
        plan_id=plan_id,
        status=status,
        # +1 h para que `(deadline - now).days` no pierda un día por los microsegundos del test.
        trial_ends_at=ahora + timedelta(days=trial_days, hours=1) if trial_days is not None else None,
        period_end=ahora + timedelta(days=period_days, hours=1) if period_days is not None else None,
        dia_ancla=None,
    )


@pytest.fixture
def con_suscripcion(monkeypatch: pytest.MonkeyPatch):
    """Sustituye la lectura de BD por la suscripción que pida cada test."""

    def _con(sub: Any) -> None:
        async def _leer(_tenant: uuid.UUID, *, session: Any = None) -> Any:
            return sub

        monkeypatch.setattr(enforcement, "get_active_subscription", _leer)

    return _con


async def test_free_en_prueba_trae_dias_restantes(con_suscripcion) -> None:
    con_suscripcion(_suscripcion("free", "trialing", trial_days=10))

    resumen = await enforcement.plan_summary(TENANT)

    assert resumen is not None
    assert (resumen.plan_id, resumen.plan_name, resumen.status) == ("free", "Free", "trialing")
    assert resumen.has_plan is True
    assert resumen.days_left == 10


async def test_plan_pago_trae_el_nombre_del_catalogo(con_suscripcion) -> None:
    con_suscripcion(_suscripcion("basic", "active", period_days=20))

    resumen = await enforcement.plan_summary(TENANT)

    assert resumen is not None
    assert (resumen.plan_id, resumen.plan_name) == ("basic", "Basic")
    assert resumen.days_left == 20


async def test_free_vencido_no_tiene_dias_negativos(con_suscripcion) -> None:
    con_suscripcion(_suscripcion("free", "expired", period_days=-1))

    resumen = await enforcement.plan_summary(TENANT)

    assert resumen is not None
    assert resumen.status == "expired"
    assert resumen.days_left == 0


async def test_sin_suscripcion_es_un_negocio_sin_plan(con_suscripcion) -> None:
    con_suscripcion(None)

    resumen = await enforcement.plan_summary(TENANT)

    assert resumen is not None
    assert resumen.has_plan is False
    assert resumen.status == "sin_suscripcion"
    assert resumen.plan_id is None


async def test_sin_negocio_activo_no_hay_plan(con_suscripcion) -> None:
    con_suscripcion(_suscripcion("free", "trialing", trial_days=10))

    assert await enforcement.plan_summary(None) is None
