"""`webhook_events` no tiene RLS (ADR-0028, tabla de sistema, `tenant_id`
nullable): la única garantía de que no se filtre entre tenants es que ningún
código de aplicación la toque fuera de `core/gateway/webhooks.py`, que
siempre filtra/pasa `tenant_id` explícito. Verificado por grep estático,
mismo patrón que `tests/test_meta_token_store_aislado.py`."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILES = {
    ROOT / "core" / "gateway" / "webhooks.py",
    ROOT / "core" / "db" / "models.py",  # define el modelo, no lo usa
}
ALLOWED_DIRS = (
    ROOT / "alembic" / "versions",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "packages",  # cada pack puede tener su propio concepto de "webhook event"
    # (ej. pack-payments/providers/*), no relacionado con la tabla de Meta
    # y sin acceso a core (invariante nº5: ningún pack importa el core).
)

_PATTERN = re.compile(r"\bWebhookEvent\b|\bwebhook_events\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_webhook_events_solo_se_escribe_desde_webhooks_py():
    offenders = []
    for path in _python_files():
        if path in ALLOWED_FILES or any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _PATTERN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"WebhookEvent/webhook_events referenciado fuera de core/gateway/webhooks.py: {offenders} -- "
        "esta tabla no tiene RLS (ADR-0028); todo acceso debe pasar por ahí, que filtra tenant_id a mano."
    )


def test_admin_views_no_expone_webhook_events():
    """Ningún endpoint del panel debe leer webhook_events sin filtro explícito
    -- hoy la respuesta correcta es que no lo lea en absoluto."""
    views = (ROOT / "core" / "admin" / "views.py").read_text(encoding="utf-8")
    assert "WebhookEvent" not in views
    assert "webhook_events" not in views
