"""HU-CFG-05 (SPEC-37): limpieza de palabras de baja y validación de la política de WhatsApp Web."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.admin.envios_segmento import limpiar_palabras_de_baja
from core.api.routers.settings import BajasUpdate, WaWebPolicyUpdate


def test_limpiar_quita_vacios_y_repetidos_sin_importar_mayusculas_tildes_ni_signos() -> None:
    assert limpiar_palabras_de_baja(["baja", " STOP ", "Stop", "no  más", "NO MAS", "", "   ", "¡¡!!"]) == [
        "baja",
        "STOP",
        "no más",
    ]
    assert limpiar_palabras_de_baja([]) == []


def test_bajas_update_acota_cantidad_y_largo() -> None:
    BajasUpdate(palabras=["baja"] * 50)
    with pytest.raises(ValidationError):
        BajasUpdate(palabras=["baja"] * 51)
    with pytest.raises(ValidationError, match="60 caracteres"):
        BajasUpdate(palabras=["x" * 61])


def _politica(**cambios) -> dict:
    base = {
        "mode": "reply_only",
        "send_min_delay_ms": 1500,
        "send_max_delay_ms": 4000,
        "daily_outbound_cap": 50,
        "warmup_enabled": True,
    }
    return {**base, **cambios}


def test_politica_waweb_valida_rangos_modo_y_coherencia_de_retardos() -> None:
    WaWebPolicyUpdate(**_politica())
    WaWebPolicyUpdate(**_politica(send_min_delay_ms=2000, send_max_delay_ms=2000))
    for malo in (
        {"mode": "inventado"},
        {"send_min_delay_ms": 60_001},
        {"send_max_delay_ms": 120_001},
        {"daily_outbound_cap": 10_001},
        {"send_min_delay_ms": 3000, "send_max_delay_ms": 2000},
    ):
        with pytest.raises(ValidationError) as exc:
            WaWebPolicyUpdate(**_politica(**malo))
        assert next(iter(malo)) in str(exc.value) or "send_max_delay_ms" in str(exc.value)
