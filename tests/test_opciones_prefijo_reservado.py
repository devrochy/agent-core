"""SPEC-40 · el prefijo `opt` queda reservado en el linter de procedimientos (unitarios)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agentcore_contracts import Procedure

from core.kernel.procedures.linter import PREFIJOS_OPCIONES, choca_con_opciones, lint

FIXTURES = Path(__file__).parent / "fixtures" / "procedures"


def _raw() -> dict:
    return json.loads((FIXTURES / "contratar_plan.json").read_text(encoding="utf-8"))


def _codigos(raw: dict) -> list[str]:
    return [i.code for i in lint(Procedure.model_validate(raw), {})]


@pytest.mark.parametrize("prefijo", ["opt:", "optc:", "optn:"])
def test_un_prefijo_de_disparador_reservado_se_rechaza(prefijo: str) -> None:
    raw = _raw()
    raw["triggers"]["interactivePayloadPrefix"] = prefijo
    assert "prefijo_reservado" in _codigos(raw)


@pytest.mark.parametrize("prefijo", ["opt:", "optc:", "optn:"])
def test_un_dato_interactivo_con_prefijo_reservado_se_rechaza(prefijo: str) -> None:
    raw = _raw()
    raw["slots"]["libre"] = {"type": "string", "enum": ["a"], "setBy": f"interactive:{prefijo}"}
    issues = [i for i in lint(Procedure.model_validate(raw), {}) if i.code == "prefijo_reservado"]
    assert issues and issues[0].path.endswith(".setBy")


def test_los_prefijos_de_siempre_no_chocan() -> None:
    raw = _raw()
    assert "prefijo_reservado" not in _codigos(raw)
    raw["triggers"]["interactivePayloadPrefix"] = "options:"
    assert "prefijo_reservado" not in _codigos(raw)


def test_el_choque_vale_en_las_dos_direcciones() -> None:
    assert PREFIJOS_OPCIONES == ("opt:", "optc:", "optn:")
    assert all(choca_con_opciones(p) for p in ("o", "op", "opt", "optc", "optn:", "opt:x", "optc:abc"))
    assert not any(choca_con_opciones(p) for p in ("", "options:", "opcion:", "plan:", "confirmar:", "pot:"))
