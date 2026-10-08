"""`scripts/changelog.py`: los fragmentos de `changelog.d/` se consolidan en una versión."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "changelog_script", Path(__file__).resolve().parents[1] / "scripts" / "changelog.py"
)
assert _SPEC and _SPEC.loader
changelog = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(changelog)

ACTUAL = """# Changelog

## Sin versión — Arreglo viejo (2026-10-07)

- algo de antes.

## v0.7.0 — Operación (2026-09-23)

### Rollback
- texto.
"""


def test_consolida_fragmentos_y_sin_version_bajo_la_version_nueva() -> None:
    fragmentos = [("2026-10-08-b.md", "## Cambio B (2026-10-08)\n\n- b.\n"), ("2026-10-08-a.md", "## Cambio A (2026-10-08)\n\n- a.\n")]
    nuevo = changelog.consolidar(ACTUAL, fragmentos, "v0.8.0", "Suscripciones", date(2026, 10, 9))
    assert nuevo.startswith("# Changelog\n\n## v0.8.0 — Suscripciones (2026-10-09)\n")
    assert nuevo.index("### Cambio B") < nuevo.index("### Cambio A") < nuevo.index("### Arreglo viejo")
    assert "Sin versión" not in nuevo
    assert "## v0.7.0 — Operación (2026-09-23)" in nuevo  # lo publicado no se toca


def test_fragmento_sin_encabezado_se_rechaza() -> None:
    with pytest.raises(ValueError, match="encabezado"):
        changelog.consolidar(ACTUAL, [("x.md", "- sin título\n")], "v0.8.0", "X", date(2026, 10, 9))


def test_sin_nada_que_consolidar_es_error() -> None:
    publicado = "# Changelog\n\n## v0.7.0 — Operación (2026-09-23)\n\n- x.\n"
    with pytest.raises(ValueError):
        changelog.consolidar(publicado, [], "v0.8.0", "X", date(2026, 10, 9))


def test_los_fragmentos_reales_tienen_encabezado() -> None:
    for archivo in changelog.fragmentos():
        changelog._como_subseccion(archivo.read_text(encoding="utf-8"), archivo.name)
