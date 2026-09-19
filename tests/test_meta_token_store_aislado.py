"""`meta_private.meta_tokens` no tiene un service-role separado que la proteja
(este stack no tiene uno -- ver docstring de core/gateway/meta_token_store.py):
la garantía de "nadie más la toca" es de organización de código, verificada acá
por grep estático en vez de por permisos de BD."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_FILES = {
    ROOT / "core" / "gateway" / "meta_token_store.py",
    ROOT / "core" / "db" / "models.py",  # define el modelo, no lo usa
    ROOT / "core" / "shared" / "crypto.py",  # solo lo menciona en un comentario
}
ALLOWED_DIRS = (ROOT / "alembic" / "versions", ROOT / "tests")

_PATTERN = re.compile(r"\bMetaToken\b|\bmeta_tokens\b")


def _python_files():
    for path in ROOT.rglob("*.py"):
        if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
            continue
        yield path


def test_meta_token_solo_se_referencia_desde_su_modulo():
    offenders = []
    for path in _python_files():
        if path in ALLOWED_FILES or any(str(path).startswith(str(d)) for d in ALLOWED_DIRS):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _PATTERN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], (
        f"MetaToken/meta_tokens referenciado fuera de meta_token_store.py: {offenders} -- "
        "el único código que debe tocar la custodia de tokens es core/gateway/meta_token_store.py"
    )


def test_admin_views_no_expone_meta_tokens():
    """Ningún endpoint del panel debe serializar MetaToken -- ni siquiera de forma
    indirecta (ej. un dict con `ciphertext`)."""
    views = (ROOT / "core" / "admin" / "views.py").read_text(encoding="utf-8")
    assert "MetaToken" not in views
    assert "meta_tokens" not in views
