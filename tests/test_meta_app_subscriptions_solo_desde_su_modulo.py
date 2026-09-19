"""`meta_app_subscriptions` no tiene RLS (ADR-0029, tabla de sistema, sin
`tenant_id`): la única garantía de que no se mezcle el estado de distintas
apps es que ningún código de aplicación toque el MODELO fuera de
`core/gateway/meta_subscriptions.py` (escritura) y
`core/tasks/meta_subscriptions_health.py` (escritura, el cron de salud).
`core/admin/views.py` SÍ lee el recurso, pero siempre a través de la API
pública del módulo (`list_app_subscriptions`), nunca importando el modelo
directo -- mismo patrón que `tests/test_webhook_events_solo_desde_webhooks.py`."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILES = {
    ROOT / "core" / "gateway" / "meta_subscriptions.py",
    ROOT / "core" / "tasks" / "meta_subscriptions_health.py",
    ROOT / "core" / "db" / "models.py",  # define el modelo, no lo usa
}
ALLOWED_DIRS = (
    ROOT / "alembic" / "versions",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "packages",
)

_PATTERN = re.compile(r"\bMetaAppSubscription\b|\bmeta_app_subscriptions\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_meta_app_subscriptions_solo_se_escribe_desde_su_modulo():
    offenders = []
    for path in _python_files():
        if path in ALLOWED_FILES or any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _PATTERN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"MetaAppSubscription/meta_app_subscriptions referenciado fuera de su módulo dueño: {offenders} -- "
        "esta tabla no tiene RLS (ADR-0029); todo acceso debe pasar por core/gateway/meta_subscriptions.py."
    )


def test_admin_views_no_importa_el_modelo_directo():
    """El panel SÍ muestra este estado (`/admin/conexiones/webhooks`), pero
    siempre a través de `list_app_subscriptions` -- nunca importando
    `MetaAppSubscription` directo (que lo dejaría sin el punto de control
    único que el ADR exige)."""
    views = (ROOT / "core" / "admin" / "views.py").read_text(encoding="utf-8")
    assert "MetaAppSubscription" not in views
    assert "meta_app_subscriptions" not in views
