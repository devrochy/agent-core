"""`platform_meta_app_requirements` es una tabla de SISTEMA sin RLS ni `tenant_id` (requisitos
de Meta de la app de la plataforma, ADR-0128 §8): la única garantía de que solo el master la
toca es que ningún código fuera de `core/admin/platform_meta_es.py` (que exige master) use el
modelo. Mismo patrón que `tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILES = {
    ROOT / "core" / "admin" / "platform_meta_es.py",
    ROOT / "core" / "db" / "models.py",  # define el modelo, no lo usa
}
ALLOWED_DIRS = (
    ROOT / "alembic" / "versions",
    ROOT / "tests",
    ROOT / "docs",
)

_PATTERN = re.compile(r"\bPlatformMetaAppRequirement\b|\bplatform_meta_app_requirements\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_platform_meta_app_requirements_solo_desde_su_modulo():
    offenders = []
    for path in _python_files():
        if path in ALLOWED_FILES or any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        if _PATTERN.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"PlatformMetaAppRequirement referenciado fuera de core/admin/platform_meta_es.py: {offenders} -- "
        "es una tabla de sistema sin RLS que solo debe tocar el master (ADR-0128)."
    )


def test_el_servicio_exige_master():
    import asyncio
    import uuid

    import pytest

    from core.admin.platform_meta_es import RequisitoError, estado_app_meta, marcar_requisito

    with pytest.raises(RequisitoError):
        asyncio.run(estado_app_meta("colaborador"))
    with pytest.raises(RequisitoError):
        asyncio.run(marcar_requisito("owner", uuid.uuid4(), "app_review", done=True, notes=None))
