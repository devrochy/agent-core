"""El script `migrar_datos_cliente_agentes` carga los `datos.py` de los cuatro packs de especialidad (HU-CRM-28, HU-AOS-93)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from core.datos_cliente.registro import registro_datos


def test_el_script_registra_las_operaciones_de_cobranza() -> None:
    ruta = Path(__file__).resolve().parents[1] / "scripts" / "migrar_datos_cliente_agentes.py"
    spec = importlib.util.spec_from_file_location("migrar_datos_cliente_agentes", ruta)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    script._preparar_registro()

    ids = {a.id for a in registro_datos.de_packs({"pack_cobranza"})}
    assert "cobranza.promesa_de_pago" in ids
