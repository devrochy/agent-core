"""HU-CFG-01: `DebounceMode` y `DEBOUNCE_DELAY_MS` no se desfasan (D-SPEC-37-6)."""

from __future__ import annotations

from typing import get_args

from pydantic import TypeAdapter

from core.config import DEBOUNCE_DELAY_MS, DebounceMode


def test_los_modos_del_tipo_son_los_del_runtime() -> None:
    """Un modo en el dict y no en el tipo (o al revés) se guardaba o se ignoraba sin avisar."""
    assert set(get_args(DebounceMode)) == set(DEBOUNCE_DELAY_MS)


def test_corto_espera_tres_segundos_y_es_valido() -> None:
    assert DEBOUNCE_DELAY_MS["corto"] == 3_000
    assert TypeAdapter(DebounceMode).validate_python("corto") == "corto"
