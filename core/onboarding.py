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


# Carpeta de lo que quedó en disco de un negocio purgado cuyo slug se vuelve a dar de alta.
CUARENTENA = ".purgados"


def carpeta_del_tenant(slug: str) -> Path:
    """`TENANTS_ROOT/<slug>/` validado: nunca la raíz ni algo fuera de ella."""
    _validate_slug(slug)
    raiz = tenants_root().resolve()
    carpeta = (raiz / slug).resolve()
    if carpeta.parent != raiz:
        raise ValueError(f"Carpeta de tenant fuera de TENANTS_ROOT: {slug!r}")
    return carpeta


def borrar_carpeta_del_tenant(slug: str) -> bool:
    """Borra `TENANTS_ROOT/<slug>/` (docs, playbooks, evidencias, conocimiento) al purgar el
    negocio (ADR-0145 §6). True si había algo que borrar.

    Primero la renombra a `TENANTS_ROOT/.purgados/<slug>-<sello>` (atómico en el mismo volumen):
    desde ahí el slug queda libre y limpio aunque el borrado falle a la mitad. Después la borra.
    Un error del renombre se propaga (el slug sigue con archivos: alerta). Si falla el borrado,
    lo que queda está en `.purgados/` y no lo hereda nadie."""
    import shutil
    from datetime import UTC, datetime

    carpeta = carpeta_del_tenant(slug)
    if not carpeta.exists():
        return False
    destino_dir = tenants_root() / CUARENTENA
    destino_dir.mkdir(parents=True, exist_ok=True)
    apartada = destino_dir / f"{slug}-purgado-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    carpeta.rename(apartada)
    shutil.rmtree(apartada, ignore_errors=True)
    if apartada.exists():
        log.warning("tenant_carpeta_purgada_sin_borrar", slug=slug, destino=apartada.name)
    return True


# Subcarpetas con datos de clientes que un negocio NUEVO nunca hereda: documentos de
# conocimiento (ADR-0145) y evidencias de cobranza. `docs/` no: se puede preparar antes del alta.
CARPETAS_DE_DATOS = ("conocimiento", "evidencias")


def cuarentena_de_datos_huerfanos(slug: str) -> list[Path]:
    """Al dar de alta un negocio NUEVO con un slug que ya tiene datos de clientes en disco (la
    purga no los pudo borrar, o la base de desarrollo se recreó), los mueve a
    `TENANTS_ROOT/.purgados/` para que el negocio nuevo no herede archivos de otro. Devuelve los
    destinos (vacío si no había nada)."""
    from datetime import UTC, datetime

    carpeta = carpeta_del_tenant(slug)
    movidas: list[Path] = []
    sello = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    for nombre in CARPETAS_DE_DATOS:
        datos = carpeta / nombre
        if not datos.is_dir() or not any(datos.iterdir()):
            continue
        destino_dir = tenants_root() / CUARENTENA
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / f"{slug}-{nombre}-{sello}"
        datos.rename(destino)
        movidas.append(destino)
        log.warning("tenant_datos_huerfanos_en_cuarentena", slug=slug, carpeta=nombre, destino=destino.name)
    return movidas


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

async def sembrar_catalogo(tenant_id: uuid.UUID, data: dict) -> list[str]:
    """Importa un `catalog.json` de pack_commerce al negocio. Idempotente.

    Lo delega en `seed_data` del pack (ADR-0110): el core no importa packs."""
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    module = plugin_manager.get("pack_commerce")
    if module is None:
        raise RuntimeError("pack_commerce no está instalado")
    return await module.seed_data(tenant_id, data)


async def sembrar_packs(tenant_id: uuid.UUID, slug: str) -> list[str]:
    """Seed genérico por pack (ADR-0083, issue #154): `TENANTS_ROOT/<slug>/<pack>.json` de
    cada pack instalado que tenga uno (commerce usa `catalog.json`, por eso se excluye).
    Un archivo que no es JSON válido se ignora con un aviso en el log."""
    from core.plugin_manager import plugin_manager

    _validate_slug(slug)
    pasos: list[str] = []
    for module in plugin_manager.list_modules():
        if module.id == "pack_commerce":
            continue
        path = tenant_dir(slug) / f"{module.id}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("seed_pack_json_invalido", pack=module.id, slug=slug)
            continue
        pasos.extend(await module.seed_data(tenant_id, data))
        pasos.append(f"{module.id}.json importado")
    return pasos


async def sembrar_artefactos(tenant_id: uuid.UUID, slug: str, *, voz_de_marca: bool = False) -> list[str]:
    """Lo que hace `agent-core tenant seed`: catálogo, seed de cada pack y voz de marca desde
    `TENANTS_ROOT/<slug>/` (HU-SH-01). Lo usan el CLI y el runner de journeys, que arma una
    réplica del asistente de un negocio con sus artefactos (HU-SH-08)."""
    _validate_slug(slug)
    pasos: list[str] = []
    data = load_tenant_catalog(slug)
    if data is not None:
        if data.get("pack") == "pack_commerce":
            pasos.extend(await sembrar_catalogo(tenant_id, data))
        else:
            log.warning("seed_catalogo_sin_pack_conocido", slug=slug, pack=str(data.get("pack")))
    pasos.extend(await sembrar_packs(tenant_id, slug))
    if await ensure_brand_voice(tenant_id, slug, sobrescribir=voz_de_marca):
        pasos.append("brand_voice sembrada desde docs/voz-de-marca.md")
    return pasos
