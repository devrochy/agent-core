"""Artefactos de los tenants (multi-tenant, ADR-0001, ADR-0085).

Los artefactos de cada tenant (catálogo, evals, documentación, voz de marca) viven en
`TENANTS_ROOT/<slug>/` (default `./tenants`, gitignored) — fuera del código del
framework, para que agent-core no mezcle documentación ni datos de sus clientes.

El alta de un negocio (`onboard_tenant`) vive en `core/alta_negocio.py` (ADR-0121): este
módulo lo usan también los packs (`tenant_dir`), así que no puede llevarla.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select

from core.config import get_settings
from core.db.models import AppSettings
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def tenants_root() -> Path:
    """Directorio de tenants (TENANTS_ROOT). Relativo al repo si no es absoluto."""
    raw = get_settings().tenants_root
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def tenant_dir(slug: str) -> Path:
    """Carpeta de artefactos del tenant: `TENANTS_ROOT/<slug>/`."""
    return tenants_root() / slug


def ensure_tenant_docs(slug: str, name: str, business_description: str) -> Path:
    """Crea `TENANTS_ROOT/<slug>/docs/` con un README stub (idempotente).

    La documentación específica del tenant vive aquí, no en docs/ del framework.
    """
    docs = tenant_dir(slug) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    readme = docs / "README.md"
    if not readme.exists():
        readme.write_text(
            "\n".join(
                [
                    f"# {name}",
                    "",
                    "Documentación específica de este tenant. Aquí vive la identidad, "
                    "decisiones de configuración y changelog del asistente — separada "
                    "de la documentación del framework (docs/).",
                    "",
                    f"Negocio: {business_description or '—'}",
                    "",
                    "## Estructura",
                    "",
                    "- `catalog.json` — datos del tenant importables a la BD "
                    "(`agent-core tenant seed <slug>`).",
                    "- `docs/` — documentación específica (este README).",
                    "- `evals/` — golden set de conversaciones del tenant.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    return docs


def load_tenant_catalog(slug: str) -> dict | None:
    """Lee `TENANTS_ROOT/<slug>/catalog.json`. None si no existe o es inválido."""
    catalog = tenant_dir(slug) / "catalog.json"
    if not catalog.exists():
        return None
    try:
        return json.loads(catalog.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("tenant_catalog_invalid", slug=slug, error=str(exc))
        return None


def _pack_playbook_refs(packs: list[str]) -> list[dict[str, str]]:
    """Playbooks de cada pack habilitado (allowlist del spec).

    Al habilitar un pack, sus playbooks (packages/pack-*/playbooks o
    plugins/*/playbooks) entran al índice `AgentSpec.playbooks` — genérico y por
    tenant (antes se hacía con una migración por cliente, ej. 0005).
    """
    from core.agents.playbooks import playbook_roots

    refs: list[dict[str, str]] = []
    for root in playbook_roots():
        folder = root.parent.name.replace("-", "_")
        if folder not in packs:
            continue
        for file in sorted(root.glob("*.md")):
            refs.append({"name": file.stem, "description": file.stem})
    return refs


def _validate_slug(slug: str) -> None:
    """Slug seguro para usar como path (TENANTS_ROOT/<slug>/). Evita traversal."""
    import re

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug or ""):
        raise ValueError(f"Slug inválido (solo minúsculas, números y guiones): {slug!r}")


async def ensure_brand_voice(tenant_id: uuid.UUID, slug: str, *, sobrescribir: bool = False) -> bool:
    """Siembra `AppSettings.brand_voice` desde `TENANTS_ROOT/<slug>/docs/voz-de-marca.md`
    si está vacío (#158). La voz de marca es la fuente de verdad de políticas; un
    asistente sin ella alucina. Devuelve True si la sembró.

    `sobrescribir=True` (HU-SH-01) la reemplaza aunque ya tenga texto: el archivo
    versionado del negocio manda y el runbook lo reaplica tras corregirlo.
    """
    _validate_slug(slug)  # nunca salir de TENANTS_ROOT con un slug armado (auditoría HU-SH-01)
    voice_path = tenant_dir(slug) / "docs" / "voz-de-marca.md"
    if not voice_path.exists():
        return False
    content = voice_path.read_text(encoding="utf-8").strip()
    if not content:
        return False
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            settings = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tenant_id,
                        AppSettings.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if settings is None:
                settings = AppSettings(tenant_id=tenant_id)
                session.add(settings)
                await session.flush()
            if settings.brand_voice and settings.brand_voice.strip():
                if not sobrescribir or settings.brand_voice.strip() == content:
                    return False
            settings.brand_voice = content
    return True