"""`ConnectionWizard` (tabla `connection_wizards`, tiene RLS del propio
tenant -- NO es una tabla de sistema) nunca debe recibir escrituras fuera de
`core/gateway/meta_onboarding.py`, y ninguna función de ese módulo acepta un
parámetro `code`/`token` para persistir -- mismo patrón de aislamiento que
`tests/test_webhook_events_solo_desde_webhooks.py` y
`tests/test_meta_app_subscriptions_solo_desde_su_modulo.py`, pero acá la
regla es sobre secretos, no sobre RLS (que sí existe para esta tabla)."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILES = {
    ROOT / "core" / "gateway" / "meta_onboarding.py",
    ROOT / "core" / "db" / "models.py",  # define el modelo, no lo usa
}
ALLOWED_DIRS = (
    ROOT / "alembic" / "versions",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "packages",
)

_PATTERN = re.compile(r"\bConnectionWizard\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_connection_wizard_solo_se_escribe_desde_meta_onboarding():
    offenders = []
    for path in _python_files():
        if path in ALLOWED_FILES or any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _PATTERN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"ConnectionWizard referenciado fuera de core/gateway/meta_onboarding.py: {offenders} -- "
        "ver ADR-0030, todo acceso debe pasar por ese módulo."
    )


def test_ninguna_funcion_publica_de_meta_onboarding_acepta_code_o_token_como_parametro():
    """Regla dura por diseño de firma: si alguien intenta agregar un
    parámetro `code`/`token` a una función que persiste (para "optimizar" y
    guardarlo en `context`), este test lo bloquea antes de que llegue a
    revisión humana."""
    import core.gateway.meta_onboarding as mod

    offenders = []
    for name, fn in vars(mod).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if inspect.getmodule(fn) is not mod:
            continue
        params = set(inspect.signature(fn).parameters)
        forbidden = {p for p in params if p in {"token", "access_token"} or (p == "code" and name != "exchange_code")}
        if forbidden:
            offenders.append((name, forbidden))
    assert offenders == [], f"Funciones con parámetros prohibidos: {offenders}"
