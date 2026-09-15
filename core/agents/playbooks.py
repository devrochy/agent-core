"""Playbooks: índice + carga just-in-time (ADR-0014).

Un playbook es un `.md` versionado en `core/playbooks/` (defaults del framework) o
en `playbooks/` dentro de un pack. `load_playbook` SOLO devuelve contenido si el
nombre está en la allowlist `AgentSpec.playbooks` del tenant (anti-inyección) y
respeta el límite de tamaño. El contenido entra como bloque del system prompt.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from agentcore_contracts import AgentSpec

from core.agents.spec import load_active_spec
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_SIZE_BYTES = 8 * 1024  # ADR-0014: límite por playbook (anti-bloat/inyección)

CORE_PLAYBOOKS = Path(__file__).resolve().parent.parent / "playbooks"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def playbook_roots() -> list[Path]:
    roots = [CORE_PLAYBOOKS]
    for pattern in ("plugins/*/playbooks", "packages/pack-*/playbooks"):
        roots.extend(sorted(REPO_ROOT.glob(pattern)))
    return roots


def _strip_frontmatter(content: str) -> str:
    if content.startswith("---\n"):
        parts = content.split("---\n", 2)
        if len(parts) == 3:
            return parts[2].lstrip("\n")
    return content


async def load_playbook(
    spec_or_tenant: AgentSpec | uuid.UUID | str | None, name: str
) -> str | None:
    """Carga el contenido del playbook `name` si está en la allowlist del spec.

    Acepta un `AgentSpec` (evals/tests) o un tenant (resuelve su spec activo).
    """
    if isinstance(spec_or_tenant, AgentSpec):
        spec = spec_or_tenant
    else:
        spec = await load_active_spec(spec_or_tenant)
    allowed = {p.name for p in spec.playbooks}
    if name not in allowed:
        log.info("playbook_not_allowed", playbook=name, tenant_id=str(spec.tenantId))
        return None

    for root in playbook_roots():
        candidate = root / f"{name}.md"
        if candidate.exists():
            # Fail-open ante un error de IO real (permisos, encoding, disco):
            # quien llama (ej. FALLBACK_REPLY/ESCALATION_REPLY en loop.py/
            # consumer.py) trata `None` como "usar el texto fijo de código",
            # no como excepción que tumbe el turno -- un playbook roto no
            # puede ser peor que el mecanismo de seguridad que reemplaza
            # (auditor-multitenant, ítem 4 de la fila 170 del ROADMAP).
            try:
                content = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                log.warning("playbook_read_failed", playbook=name, exc_info=True)
                return None
            if len(content.encode("utf-8")) > MAX_SIZE_BYTES:
                log.warning("playbook_too_large", playbook=name)
                return None
            return _strip_frontmatter(content)
    log.info("playbook_not_found", playbook=name)
    return None


async def list_playbooks() -> list[dict[str, str]]:
    """Índice de playbooks disponibles en disco (para el panel)."""
    result: list[dict[str, str]] = []
    for root in playbook_roots():
        for file in sorted(root.glob("*.md")):
            result.append({"name": file.stem, "path": str(file)})
    return result