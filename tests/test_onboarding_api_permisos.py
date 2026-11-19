"""`GET /admin/onboarding` (HU-SH-06): qué ve y qué puede hacer cada usuario en un paso."""

from __future__ import annotations

import types
import uuid

from core.api.routers.onboarding import _puede_actuar, _puede_ver


def _principal(role: str, permisos: dict[str, str | None]):  # type: ignore[no-untyped-def]
    return types.SimpleNamespace(admin=types.SimpleNamespace(role=role, id=uuid.uuid4()), permissions=permisos)


def test_sin_lectura_de_la_seccion_no_ve_la_cantidad_ni_actua() -> None:
    sin_lectura = _principal("colaborador", {"pack_commerce": None})
    assert (_puede_ver(sin_lectura, "pack_commerce"), _puede_actuar(sin_lectura, "pack_commerce")) == (False, False)


def test_lectura_ve_y_escritura_actua() -> None:
    lector = _principal("colaborador", {"pack_commerce": "read"})
    dueno = _principal("colaborador", {"pack_commerce": "write"})
    assert (_puede_ver(lector, "pack_commerce"), _puede_actuar(lector, "pack_commerce")) == (True, False)
    assert (_puede_ver(dueno, "pack_commerce"), _puede_actuar(dueno, "pack_commerce")) == (True, True)


def test_master_ve_y_actua_y_sin_seccion_nadie() -> None:
    master = _principal("master", {})
    assert _puede_ver(master, "plan") and _puede_actuar(master, "plan")
    assert not _puede_ver(master, None) and not _puede_actuar(master, None)
