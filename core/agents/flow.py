"""Flujo de atención declarativo por pack, personalizable por tenant (ADR-0054).

Un flujo es una lista ordenada de **secciones** (saludo, cotización, pedido...). Cada
sección agrupa tools por momento de la atención y lleva un `purpose` corto. El pack
declara el flujo default en `flow.json` (versionado con el pack); el tenant puede
tener su propia copia en `TENANTS_ROOT/<slug>/flow.json` (sembrada al habilitar el
pack). Si el archivo del tenant existe, es autoritativo; si no, se usa el default de
los packs habilitados.

No toca el contrato congelado: es un artefacto de pack + tenant. Los nombres de tools
se referencian como strings (ningún pack importa a otro, invariante nº5) y se
resuelven contra las tools realmente disponibles; un nombre desconocido se ignora con
warning, nunca tumba el turno.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

FLOW_FILENAME = "flow.json"
MAX_FLOW_BYTES = 64 * 1024
MAX_SECTIONS = 20
MAX_TOOLS_PER_SECTION = 40

_SECTION_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,80}$")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class FlowSection:
    id: str
    label: str
    purpose: str
    pack: str
    tools: list[str]


@dataclass
class ResolvedFlow:
    sections: list[FlowSection]
    source: str  # "tenant" | "pack"
    unknown_tools: list[str] = field(default_factory=list)

    @property
    def tool_names(self) -> list[str]:
        """Nombres de tools del flujo, en orden y sin repetir."""
        seen: set[str] = set()
        names: list[str] = []
        for section in self.sections:
            for name in section.tools:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        return names


# ------------------------------------------------------------------ discovery
def _flow_dirs() -> list[tuple[str, Path]]:
    """(pack_id, carpeta) de los packs con `flow.json`, más el core si lo tuviera.

    El id del pack es el nombre de la carpeta con guiones → guiones bajos, mismo
    criterio que `_pack_playbook_refs` (`core/onboarding.py`).
    """
    dirs: list[tuple[str, Path]] = []
    core_dir = REPO_ROOT / "core"
    if (core_dir / FLOW_FILENAME).exists():
        dirs.append(("core", core_dir))
    for pattern in ("packages/pack-*", "plugins/*"):
        for folder in sorted(REPO_ROOT.glob(pattern)):
            if (folder / FLOW_FILENAME).exists():
                dirs.append((folder.name.replace("-", "_"), folder))
    return dirs


def pack_flow_path(pack_id: str) -> Path | None:
    """Ruta del `flow.json` de un pack, o None si no declara flujo."""
    for pid, folder in _flow_dirs():
        if pid == pack_id:
            return folder / FLOW_FILENAME
    return None


def _assert_safe_slug(slug: str) -> None:
    """Slug seguro como path (evita traversal fuera de `TENANTS_ROOT/<slug>/`).

    Reutiliza `_validate_slug` de onboarding (misma regla que el alta), porque
    `tenant_flow_path`/`seed_tenant_flow` son API de lectura/escritura a
    filesystem: mismo criterio ya aplicado a los playbooks (ADR-0026,
    `core/agents/playbooks.py::_SAFE_PLAYBOOK_NAME`).
    """
    from core.onboarding import _validate_slug

    _validate_slug(slug)


def tenant_flow_path(slug: str) -> Path:
    """Ruta del `flow.json` del tenant (`TENANTS_ROOT/<slug>/flow.json`)."""
    from core.onboarding import tenant_dir

    _assert_safe_slug(slug)
    return tenant_dir(slug) / FLOW_FILENAME


# ------------------------------------------------------------------ parseo
def _parse_sections(raw: Any) -> list[FlowSection]:
    """Valida y normaliza las secciones. Descarta las malformadas (fail-safe)."""
    if not isinstance(raw, dict):
        return []
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list):
        return []

    sections: list[FlowSection] = []
    seen_ids: set[str] = set()
    for item in sections_raw[:MAX_SECTIONS]:
        if not isinstance(item, dict):
            continue
        section_id = str(item.get("id") or "").strip()
        if not _SECTION_ID_RE.match(section_id) or section_id in seen_ids:
            log.warning("flow_section_invalida", section_id=section_id)
            continue
        tools: list[str] = []
        for raw_tool in (item.get("tools") or [])[:MAX_TOOLS_PER_SECTION]:
            name = str(raw_tool).strip()
            if _TOOL_NAME_RE.match(name) and name not in tools:
                tools.append(name)
        seen_ids.add(section_id)
        sections.append(
            FlowSection(
                id=section_id,
                label=str(item.get("label") or section_id)[:120],
                purpose=str(item.get("purpose") or "")[:600],
                pack=str(item.get("pack") or "")[:60],
                tools=tools,
            )
        )
    return sections


def _read_flow_file(path: Path) -> list[FlowSection] | None:
    """Lee y parsea un `flow.json`. None si no existe, está roto o es muy grande."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FLOW_BYTES:
        log.warning("flow_too_large", path=str(path))
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log.warning("flow_invalid_json", path=str(path))
        return None
    return _parse_sections(data)


def load_tenant_flow(slug: str) -> list[FlowSection] | None:
    """Secciones del `flow.json` del tenant, o None si no existe/es inválido."""
    return _read_flow_file(tenant_flow_path(slug))


def load_pack_flows(pack_ids: Iterable[str]) -> list[FlowSection]:
    """Secciones default de los packs indicados, en el orden de `pack_ids`."""
    sections: list[FlowSection] = []
    for pack_id in pack_ids:
        path = pack_flow_path(pack_id)
        if path is None:
            continue
        sections.extend(_read_flow_file(path) or [])
    return sections


def _dedupe_sections(sections: list[FlowSection]) -> list[FlowSection]:
    """Una sección por `id` (la primera gana, para que el tenant pise al pack)."""
    seen: set[str] = set()
    result: list[FlowSection] = []
    for section in sections:
        if section.id in seen:
            continue
        seen.add(section.id)
        result.append(section)
    return result


# ------------------------------------------------------------------ resolución
async def _resolve_slug(spec: Any) -> str | None:
    try:
        from core.shared.tenant import tenant_slug as _tenant_slug

        return await _tenant_slug(uuid.UUID(spec.tenantId))
    except Exception:
        log.warning("flow_tenant_slug_resolve_failed", tenant_id=str(getattr(spec, "tenantId", "")), exc_info=True)
        return None


async def resolve_flow(
    spec: Any,
    *,
    slug: str | None = None,
    available: set[str] | None = None,
) -> ResolvedFlow | None:
    """Resuelve el flujo del tenant. None si no hay flujo declarado.

    `available`: si se pasa, filtra las tools a las realmente disponibles para el
    tenant (packs habilitados + declarativas) y registra el resto en
    `unknown_tools`. Con `available=None` no filtra (para validación/CLI).
    """
    from core.agents.spec import enabled_business_packs

    if slug is None:
        slug = await _resolve_slug(spec)

    tenant_sections = load_tenant_flow(slug) if slug else None
    if tenant_sections:
        sections = tenant_sections
        source = "tenant"
    else:
        sections = load_pack_flows(sorted(enabled_business_packs(spec)))
        source = "pack"

    sections = _dedupe_sections(sections)
    if not sections:
        return None

    unknown: list[str] = []
    resolved: list[FlowSection] = []
    for section in sections:
        tools = section.tools
        if available is not None:
            tools = [t for t in section.tools if t in available]
            unknown.extend(t for t in section.tools if t not in available)
        if not tools:
            continue
        resolved.append(replace(section, tools=tools))

    if not resolved:
        return None
    if unknown:
        log.info("flow_unknown_tools", tenant_id=str(getattr(spec, "tenantId", "")), tools=sorted(set(unknown)))
    return ResolvedFlow(sections=resolved, source=source, unknown_tools=sorted(set(unknown)))


def render_flow(flow: ResolvedFlow) -> str:
    """Texto del flujo para el bloque del system prompt (estable por tenant)."""
    lines: list[str] = []
    for index, section in enumerate(flow.sections, start=1):
        lines.append(f"{index}. {section.label}")
        if section.purpose:
            lines.append(f"   {section.purpose}")
        lines.append(f"   Herramientas: {', '.join(section.tools)}")
    return "\n".join(lines)


# ------------------------------------------------------------------ siembra
def seed_tenant_flow(slug: str, pack_ids: list[str]) -> list[str]:
    """Crea/actualiza `TENANTS_ROOT/<slug>/flow.json` desde los packs. Idempotente.

    Reemplaza solo las secciones cuyo `pack` está en `pack_ids` (las de esos packs);
    conserva las secciones propias del tenant (cualquier otro `pack`, ej. "tenant").
    Devuelve los pasos ejecutados (mismo estilo que `onboard_tenant`).
    """
    path = tenant_flow_path(slug)
    existing = _read_flow_file(path) or []
    syncing = set(pack_ids)
    preserved = [s for s in existing if s.pack not in syncing]
    seeded = load_pack_flows(pack_ids)

    merged = seeded + preserved
    if not merged:
        return []
    payload = {"version": 1, "sections": [asdict(s) for s in merged]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [
        f"flow.json sembrado ({len(seeded)} secciones de packs, {len(preserved)} propias)",
    ]
