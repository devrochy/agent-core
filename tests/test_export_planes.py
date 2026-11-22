"""HU-BILL-28 · export versionado del catálogo (`scripts/export_planes.py` → `docs/planes.json`). Sin base de datos.

- el JSON commiteado coincide con lo que genera el script (sin deriva respecto a `plans.py`);
- los precios v3 son mensuales: no hay cobro anual (decisión del dueño, 2026-11-22);
- `None` en topes y features sale como `null`, y no se publica «≈ conversaciones».
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "export_planes.py"
JSON = ROOT / "docs" / "planes.json"


def _script():
    spec = importlib.util.spec_from_file_location("export_planes", SCRIPT)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_planes_json_al_dia_con_el_catalogo():
    assert JSON.read_text(encoding="utf-8") == _script().render(), (
        "docs/planes.json no coincide con core/billing/plans.py: corre `python scripts/export_planes.py`."
    )


def test_precios_v3_mensuales_sin_anual():
    planes = {p["id"]: p for p in json.loads(_script().render())["planes"]}
    assert list(planes) == ["free", "basic", "premium", "empresarial"]
    assert {k: p["price_cop"] for k, p in planes.items()} == {
        "free": 0,
        "basic": 129_000,
        "premium": 299_000,
        "empresarial": 799_000,
    }
    assert all("price_cop_anual" not in p for p in planes.values())
    assert planes["free"]["trial_days"] == 30
    assert all(p["trial_days"] == 0 for k, p in planes.items() if k != "free")


def test_ilimitado_es_null_y_sin_conversaciones_estimadas():
    texto = _script().render()
    empresarial = next(p for p in json.loads(texto)["planes"] if p["id"] == "empresarial")
    assert empresarial["limits"]["agentes"] is None
    assert empresarial["features"]["historial_dias"] is None
    assert "conversaciones" not in texto
    assert texto.endswith("}\n")
