"""Servicio del checklist de producción (HU-CONN-07): estado mostrado y serialización."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.admin.production_readiness_service import (
    STALE_ERROR,
    effective_status,
    serialize_checks,
)
from core.gateway.meta_preflight import PreflightCheck


def test_una_corrida_activa_vieja_se_muestra_como_fallida():
    vieja = datetime.now(UTC) - timedelta(minutes=30)

    assert effective_status("queued", vieja, None) == ("failed", STALE_ERROR)
    assert effective_status("running", vieja, None) == ("failed", STALE_ERROR)


def test_una_corrida_reciente_o_terminada_conserva_su_estado():
    ahora = datetime.now(UTC)
    vieja = ahora - timedelta(minutes=30)

    assert effective_status("queued", ahora, None) == ("queued", None)
    assert effective_status("done", vieja, None) == ("done", None)
    assert effective_status("failed", vieja, "x") == ("failed", "x")


def test_serializar_lleva_texto_y_fecha_de_cada_check():
    checked = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    items = serialize_checks(
        [PreflightCheck(key="token", ok=True, detail="Token válido", checked_at=checked)],
        {"token": "Token de acceso"},
    )

    assert items == [
        {
            "key": "token",
            "label": "Token de acceso",
            "ok": True,
            "detail": "Token válido",
            "repair_action": None,
            "checked_at": "2026-10-09T12:00:00+00:00",
        }
    ]
