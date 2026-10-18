"""HU-AOS-23 · reglas puras de la consola de conexiones: salud y alcance de la pausa."""

from __future__ import annotations

import pytest

from core.gateway.consola_conexiones import (
    PAUSA_CHAT_WEB,
    PAUSA_CONEXION,
    PAUSA_CUENTA_META,
    alcance_de_pausa,
    salud,
)


@pytest.mark.parametrize(
    ("estado", "esperado"),
    [
        ("connected", "ok"),
        ("degraded", "degraded"),
        ("error", "error"),
        ("revoked", "paused"),  # la pausa de ADR-0122
        ("draft", "pending"),
        ("pending", "pending"),
        (None, "error"),  # cuenta borrada o desconocida: nunca "ok"
    ],
)
def test_con_cuenta_de_meta_manda_el_estado_de_la_cuenta(estado: str | None, esperado: str) -> None:
    # `active` de la fila canónica no cuenta: la pausa es la de la cuenta.
    assert salud(active=True, meta_status=estado, tiene_cuenta=True) == esperado
    assert salud(active=False, meta_status=estado, tiene_cuenta=True) == esperado


def test_sin_cuenta_manda_active() -> None:
    assert salud(active=True, meta_status=None, tiene_cuenta=False) == "ok"
    assert salud(active=False, meta_status=None, tiene_cuenta=False) == "paused"


def test_alcance_de_la_pausa() -> None:
    assert alcance_de_pausa(tiene_cuenta=True, type_id="whatsapp_meta") == PAUSA_CUENTA_META
    assert alcance_de_pausa(tiene_cuenta=False, type_id="webchat") == PAUSA_CHAT_WEB
    assert alcance_de_pausa(tiene_cuenta=False, type_id="whatsapp_meta") == PAUSA_CONEXION
    assert alcance_de_pausa(tiene_cuenta=False, type_id=None) == PAUSA_CONEXION
