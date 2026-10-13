"""Roles de plataforma de un admin (ADR-0047, ADR-0134).

- `master`: el dueño de la plataforma. Bypass total y gestión de cuentas.
- `tester` (SPEC-30, D-SPEC-30-2): **actúa como master en todo salvo Colaboradores**, y
  solo si `QA_TESTERS_ENABLED` está activa (entorno de test). Con la variable apagada
  (producción) la cuenta no inicia sesión ni conserva sesiones, y no se puede crear.

Regla para el resto del código: los chequeos de **operación** (negocios, Meta, búsqueda,
sync, onboarding, app de campo) preguntan `actua_como_master`; los de **gestión de
cuentas** (colaboradores, invitaciones a negocios, testers, crear masters, importar el
catálogo de pruebas) siguen siendo `role == "master"`.
"""

from __future__ import annotations

from typing import Any

from core.config import get_settings

ROL_MASTER = "master"
ROL_TESTER = "tester"
ROLES_DE_PLATAFORMA = frozenset({ROL_MASTER, ROL_TESTER})

# Lo único que un tester nunca usa: gestionar cuentas.
SECCIONES_VEDADAS_AL_TESTER: frozenset[str] = frozenset({"collaborators"})


def testers_habilitados() -> bool:
    return bool(get_settings().qa_testers_enabled)


def es_tester(admin: Any) -> bool:
    return getattr(admin, "role", None) == ROL_TESTER


def es_master(admin: Any) -> bool:
    return getattr(admin, "role", None) == ROL_MASTER


def actua_como_master(admin: Any) -> bool:
    """Master, o tester con el rol habilitado en este entorno."""
    return es_master(admin) or (es_tester(admin) and testers_habilitados())


def rol_actua_como_master(role: str | None) -> bool:
    """Lo mismo que `actua_como_master`, para quien solo tiene el texto del rol."""
    return role == ROL_MASTER or (role == ROL_TESTER and testers_habilitados())


def cuenta_habilitada(admin: Any) -> bool:
    """¿La cuenta puede iniciar sesión o usar una sesión? Activa, y si es tester, con el rol
    habilitado. Lo llaman el login del panel, la validación de sesión y la app de campo."""
    if admin is None or not getattr(admin, "active", False):
        return False
    return not es_tester(admin) or testers_habilitados()


def acceso_de_plataforma(admin: Any, section: str) -> str | None:
    """Acceso que da el rol de plataforma a una sección; `None` si el rol no decide.

    Master: `write` en todo. Tester habilitado: `write` en todo salvo las secciones
    vedadas, que quedan **denegadas** (`"none"`, distinto de `None`: no hay que seguir
    buscando permisos). Tester sin habilitar: denegado en todo."""
    if es_master(admin):
        return "write"
    if es_tester(admin):
        if not testers_habilitados() or section in SECCIONES_VEDADAS_AL_TESTER:
            return "none"
        return "write"
    return None
