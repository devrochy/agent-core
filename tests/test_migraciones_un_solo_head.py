"""Cada árbol de Alembic (core y packs) tiene un solo head (HU-SH-04).

Varias HU en paralelo eligen el número siguiente a la head de `develop`. Si dos se mergean con
el mismo `down_revision`, quedan dos heads y `agent-core db migrate` falla en el despliegue.
Este test lo frena en el CI de la PR que llega segunda: esa renumera.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
ARBOLES = [ROOT, *sorted(p.parent for p in (ROOT / "packages").glob("pack-*/alembic.ini"))]


@pytest.mark.parametrize("raiz", ARBOLES, ids=lambda p: p.name if p != ROOT else "core")
def test_un_solo_head(raiz: Path) -> None:
    config = Config(str(raiz / "alembic.ini"))
    config.set_main_option("script_location", str(raiz / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, f"{raiz.name}: varias heads {heads}; renumerar la migración que llegó segunda"


def test_la_cadena_del_core_llega_a_la_0061() -> None:
    """HU-SH-04 entra antes que HU-SH-06: su 0061 cuelga de la 0059 (orden fijado)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    revision = ScriptDirectory.from_config(config).get_revision("0061")
    assert revision is not None and revision.down_revision == "0059"
