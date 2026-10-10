"""Flujo de atención declarativo y compuesto (ADR-0054, enmendado por ADR-0109).

Un flujo es una lista ordenada de **secciones** (saludo, cotización, pedido...). Cada
sección agrupa tools por momento de la atención y lleva un `purpose` corto. Cada
sección tiene un dueño (`pack`):

- `core`: `core/flow.json` (saludo y escalamiento, una sola vez para todo negocio);
- `pack_x`: `packages/pack-x/flow.json`, versionado con el pack;
- `tenant`: secciones propias del negocio en `TENANTS_ROOT/<slug>/flow.json`.

El flujo efectivo se **compone** siempre (HU-FWK-01): core + secciones de todos los
packs habilitados + secciones `tenant`. El archivo del negocio ya no es autoritativo:
guarda sus secciones `tenant` y, si quiere, un override explícito de una sección de
pack o del core (mismo id calificado `pack:id`). Un re-seed nunca toca las `tenant`.

No toca el contrato congelado: es un artefacto de pack + tenant. Los nombres de tools
se referencian como strings (ningún pack importa a otro, invariante nº5) y se
resuelven contra las tools realmente disponibles; un nombre desconocido se ignora con
warning, nunca tumba el turno.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.shared.logger import get_logger

if TYPE_CHECKING:
    from agentcore_contracts import Procedure

log = get_logger(__name__)

FLOW_FILENAME = "flow.json"
MAX_FLOW_BYTES = 64 * 1024
MAX_SECTIONS = 20
MAX_TOOLS_PER_SECTION = 40

_SECTION_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_]{1,80}$")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


CORE = "core"
TENANT = "tenant"
# Secciones que solo declara el core: un pack que las traiga se ignora (HU-FWK-01:
# dos packs con "saludo" chocaban y el primero en orden alfabético ganaba).
CORE_SECTION_IDS = frozenset({"saludo", "escalamiento"})
# Ids que los packs usaban antes de ADR-0109: el archivo de un negocio puede tener
# copias con estos ids. Se leen con el id nuevo para no confundirlas con secciones
# propias del negocio.
LEGACY_SECTION_IDS: dict[tuple[str, str], tuple[str, str]] = {
    ("pack_commerce", "saludo"): ("pack_commerce", "descubrimiento"),
    ("pack_cobranza", "saludo"): ("pack_cobranza", "verificacion"),
}


@dataclass(frozen=True)
class FlowSection:
    id: str
    label: str
    purpose: str
    pack: str
    tools: list[str]

    @property
    def qualified_id(self) -> str:
        """`pack:id`: dos packs pueden tener una sección con el mismo id sin chocar."""
        return f"{self.pack}:{self.id}"


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


def load_core_flow() -> list[FlowSection]:
    """Secciones del core (`core/flow.json`), con dueño `core`."""
    path = REPO_ROOT / "core" / FLOW_FILENAME
    return [replace(s, pack=CORE) for s in (_read_flow_file(path) or [])]


def load_pack_flows(pack_ids: Iterable[str]) -> list[FlowSection]:
    """Secciones default de los packs indicados, en el orden de `pack_ids`.

    El dueño de una sección es el pack de cuyo `flow.json` sale (no lo que diga el
    campo `pack`). Las secciones reservadas al core (`saludo`, `escalamiento`, o con
    `pack: "core"`) se ignoran: el core las declara una sola vez.
    """
    sections: list[FlowSection] = []
    for pack_id in pack_ids:
        if pack_id == CORE:
            continue
        path = pack_flow_path(pack_id)
        if path is None:
            continue
        for section in _read_flow_file(path) or []:
            if section.pack == CORE or section.id in CORE_SECTION_IDS:
                log.info("flow_seccion_del_core_en_pack", pack=pack_id, section_id=section.id)
                continue
            sections.append(replace(section, pack=pack_id))
    return sections


def all_pack_ids() -> list[str]:
    """Packs que declaran `flow.json` (sin el core)."""
    return [pid for pid, _ in _flow_dirs() if pid != CORE]


def _canonical(section: FlowSection) -> FlowSection:
    """Sección de un archivo de negocio con su id actual (alias de ADR-0109).

    Primero los alias (el `saludo` de commerce es su `descubrimiento`, no el del
    core); después, cualquier `saludo`/`escalamiento` de un pack es del core.
    """
    nuevo = LEGACY_SECTION_IDS.get((section.pack, section.id))
    if nuevo is not None:
        return replace(section, pack=nuevo[0], id=nuevo[1])
    if section.pack == CORE or (section.pack != TENANT and section.id in CORE_SECTION_IDS):
        return replace(section, pack=CORE)
    return section


def _dedupe_sections(sections: list[FlowSection]) -> list[FlowSection]:
    """Una sección por id calificado (`pack:id`); la primera gana."""
    seen: set[str] = set()
    result: list[FlowSection] = []
    for section in sections:
        if section.qualified_id in seen:
            continue
        seen.add(section.qualified_id)
        result.append(section)
    return result


def split_tenant_sections(
    tenant_sections: list[FlowSection], defaults: list[FlowSection]
) -> tuple[dict[str, FlowSection], list[FlowSection]]:
    """Separa el archivo del negocio en overrides (mismo id calificado que una sección
    del core o de un pack) y secciones propias (`tenant`).

    Una sección etiquetada con un pack pero cuyo id ese pack no declara es propia del
    negocio (p. ej. `planes` de `solvershub`, que estaba marcada `pack_commerce`).
    """
    by_qid = {s.qualified_id: s for s in defaults}
    conocidas = _known_tool_names(defaults)
    overrides: dict[str, FlowSection] = {}
    own: list[FlowSection] = []
    for raw in tenant_sections:
        section = _canonical(raw)
        default = by_qid.get(section.qualified_id) if section.pack != TENANT else None
        if default is None:
            own.append(replace(section, pack=TENANT))
        elif any(t in conocidas and t not in default.tools for t in section.tools):
            # Personalización real (trae tools de otras secciones): override.
            overrides.setdefault(section.qualified_id, section)
        # Si no, es una copia vieja del default (la sembraba el alta): se ignora y
        # gana la sección del pack, que siempre está al día.
    return overrides, _dedupe_sections(own)


def compose_flow(
    core: list[FlowSection],
    packs: list[FlowSection],
    tenant_sections: list[FlowSection],
    *,
    all_defaults: list[FlowSection] | None = None,
) -> list[FlowSection]:
    """Flujo efectivo: core (menos escalamiento) → packs → propias → escalamiento.

    `all_defaults` son las secciones de TODOS los packs (no solo los habilitados): un
    override de un pack deshabilitado no se convierte en sección propia, se descarta.
    """
    known = [*core, *(all_defaults if all_defaults is not None else packs)]
    overrides, own = split_tenant_sections(tenant_sections, known)

    def _con_override(section: FlowSection) -> FlowSection:
        return overrides.get(section.qualified_id, section)

    inicio = [_con_override(s) for s in core if s.id != "escalamiento"]
    fin = [_con_override(s) for s in core if s.id == "escalamiento"]
    medio = [_con_override(s) for s in packs]
    return _dedupe_sections([*inicio, *medio, *own, *fin])


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

    tenant_sections = (load_tenant_flow(slug) if slug else None) or []
    sections = compose_flow(
        load_core_flow(),
        load_pack_flows(sorted(enabled_business_packs(spec))),
        tenant_sections,
        all_defaults=load_pack_flows(all_pack_ids()),
    )
    # `source` informa si el negocio aporta algo propio (secciones u overrides).
    source = "tenant" if tenant_sections else "pack"
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


# ------------------------------------------------------------------ conversión a procedure/1
# Versión de los procedimientos abiertos que salen del flujo: no los publica ninguna
# habilidad, son el puente del lector tolerante (ADR-0113 §5).
OPEN_PROCEDURE_VERSION = "0.0.0"
_OWNER_UNSAFE_RE = re.compile(r"[^a-z0-9_]")


def _procedure_owner(pack: str) -> str:
    """Dueño del procedimiento a partir del `pack` de la sección (`core`, `tenant` o el
    id de la habilidad), normalizado al formato del contrato."""
    owner = _OWNER_UNSAFE_RE.sub("_", pack.lower())[:60]
    return owner or TENANT


def flow_to_procedures(
    sections: Iterable[FlowSection],
    *,
    catalogo: Mapping[str, Any] | None = None,
    confirmed_actions: Iterable[str] = (),
) -> list[Procedure]:
    """Convierte el flujo compuesto (ADR-0109) en procedimientos `procedure/1` abiertos
    (ADR-0113 §5, HU-AOS-03): uno por sección, con una sola etapa terminal que permite
    exactamente las acciones de la sección, en el mismo orden. No hay datos, salida ni
    afirmaciones: nada cambia de conducta, solo de forma.

    - `catalogo` (acciones del binario, `core.kernel.procedures.catalogo_de_acciones`):
      si se pasa, `requires` declara las habilidades ajenas cuyas acciones usa la
      sección por nombre.
    - `confirmed_actions`: acciones con `requiresConfirmation` en el binding v2 del
      negocio. El procedimiento abierto las hereda en `confirmActions` de su etapa, que
      el linter acepta como confirmación (ADR-0113 §5).

    La entrada debe ser el flujo ya resuelto y filtrado (`ResolvedFlow.sections`), así el
    procedimiento no ofrece acciones que el loop no ofrece. Es total: toda sección que
    `_parse_sections` acepta se convierte (los formatos del contrato son un superconjunto
    de los del flujo). Sin cambio de conducta: hoy nadie la llama en runtime.
    """
    from agentcore_contracts import Procedure, Stage, Triggers

    confirmadas = set(confirmed_actions)
    procedures: list[Procedure] = []
    for section in sections:
        owner = _procedure_owner(section.pack)
        requires: list[str] = []
        if catalogo is not None:
            for tool in section.tools:
                info = catalogo.get(tool)
                dueno = getattr(info, "owner", None)
                if dueno and dueno not in (CORE, owner) and dueno not in requires:
                    requires.append(dueno)
        procedures.append(
            Procedure(
                id=f"{owner}.{section.id}",
                version=OPEN_PROCEDURE_VERSION,
                title=section.label,
                requires=requires[:20],
                triggers=Triggers(description=section.purpose),
                stages=[
                    Stage(
                        id=section.id,
                        goal=section.purpose,
                        actions=list(section.tools),
                        confirmActions=[t for t in section.tools if t in confirmadas],
                    )
                ],
            )
        )
    return procedures


def confirmed_actions_v2(spec: Any) -> set[str]:
    """Acciones con `requiresConfirmation` en los bindings v2 habilitados del spec."""
    return {
        binding.tool
        for binding in getattr(spec, "tools", None) or []
        if getattr(binding, "enabled", False) and getattr(binding, "requiresConfirmation", False)
    }


# ------------------------------------------------------------------ siembra y migración
@dataclass
class FlowMigration:
    """Qué cambia en el `flow.json` de un negocio al normalizarlo (ADR-0109)."""

    copias_eliminadas: list[str] = field(default_factory=list)
    pasan_a_tenant: list[str] = field(default_factory=list)
    overrides: list[str] = field(default_factory=list)
    tools_desconocidas: dict[str, list[str]] = field(default_factory=dict)
    propias: list[str] = field(default_factory=list)
    cambia: bool = False

    def resumen(self) -> list[str]:
        lineas = [
            f"copias de packs eliminadas: {', '.join(self.copias_eliminadas) or '—'}",
            f"pasan a sección propia (tenant): {', '.join(self.pasan_a_tenant) or '—'}",
            f"overrides que se conservan: {', '.join(self.overrides) or '—'}",
            f"secciones propias: {', '.join(self.propias) or '—'}",
        ]
        for qid, tools in self.tools_desconocidas.items():
            lineas.append(f"tools que ningún flow.json declara en {qid}: {', '.join(tools)}")
        return lineas


def _known_tool_names(defaults: list[FlowSection]) -> set[str]:
    return {t for s in defaults for t in s.tools}


def plan_tenant_flow(
    tenant_sections: list[FlowSection], defaults: list[FlowSection]
) -> tuple[list[FlowSection], FlowMigration]:
    """Normaliza las secciones del archivo de un negocio contra los defaults del core
    y de TODOS los packs. Devuelve las secciones que deben quedar y el reporte.

    - Copia de una sección del core o de un pack (mismo id calificado, y sus tools son
      del default o no existen en ningún flow.json) → se elimina: el runtime compone
      la sección del pack, que siempre está al día.
    - Mismo id calificado con tools de otras secciones (personalización real) →
      override: se conserva.
    - Id que su pack no declara, o etiquetada `tenant` → sección propia.
    """
    by_qid = {s.qualified_id: s for s in defaults}
    conocidas = _known_tool_names(defaults)
    migracion = FlowMigration()
    quedan: list[FlowSection] = []
    for raw in tenant_sections:
        section = _canonical(raw)
        default = by_qid.get(section.qualified_id) if section.pack != TENANT else None
        if default is None:
            propia = replace(section, pack=TENANT)
            if raw.pack != TENANT:
                migracion.pasan_a_tenant.append(f"{raw.qualified_id} → {propia.qualified_id}")
            migracion.propias.append(propia.qualified_id)
            quedan.append(propia)
            continue
        desconocidas = [t for t in section.tools if t not in conocidas]
        if desconocidas:
            migracion.tools_desconocidas[section.qualified_id] = desconocidas
        extras = [t for t in section.tools if t in conocidas and t not in default.tools]
        if extras:
            migracion.overrides.append(section.qualified_id)
            quedan.append(section)
        else:
            migracion.copias_eliminadas.append(raw.qualified_id)
    quedan = _dedupe_sections(quedan)
    migracion.cambia = [asdict(s) for s in quedan] != [asdict(s) for s in tenant_sections]
    return quedan, migracion


def normalize_tenant_flow(slug: str, *, apply: bool) -> FlowMigration:
    """Normaliza `TENANTS_ROOT/<slug>/flow.json` (ver `plan_tenant_flow`).

    Con `apply=False` solo informa (dry-run). Idempotente: aplicarlo dos veces no
    cambia nada la segunda. Nunca toca las secciones propias del negocio.
    """
    path = tenant_flow_path(slug)
    existentes = _read_flow_file(path)
    if existentes is None:
        return FlowMigration()
    defaults = [*load_core_flow(), *load_pack_flows(all_pack_ids())]
    quedan, migracion = plan_tenant_flow(existentes, defaults)
    if apply and migracion.cambia:
        payload = {"version": 2, "sections": [asdict(s) for s in quedan]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return migracion


def seed_tenant_flow(slug: str, pack_ids: list[str]) -> list[str]:
    """Al habilitar packs: el flujo del pack se compone en runtime, así que ya no se
    copia al archivo del negocio. Solo normaliza el archivo si existe (quita copias
    viejas, nunca toca las secciones propias). Idempotente.
    """
    _assert_safe_slug(slug)
    if not any(pack_flow_path(p) for p in pack_ids):
        return []
    migracion = normalize_tenant_flow(slug, apply=True)
    pasos = [f"flujo de {', '.join(sorted(pack_ids))} compuesto desde los packs"]
    if migracion.copias_eliminadas:
        pasos.append(f"flow.json del negocio: {len(migracion.copias_eliminadas)} copias de packs quitadas")
    return pasos
