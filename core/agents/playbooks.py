"""Playbooks: índice + carga just-in-time (ADR-0014).

Un playbook es un `.md` versionado en `core/playbooks/` (defaults del framework) o
en `playbooks/` dentro de un pack. `load_playbook` SOLO devuelve contenido si el
nombre está en la allowlist `AgentSpec.playbooks` del tenant (anti-inyección) y
respeta el límite de tamaño. El contenido entra como bloque del system prompt.

Un tenant puede OVERRIDEAR el contenido de un playbook ya permitido (ADR-0026):
`TENANTS_ROOT/<slug>/playbooks/<name>.md`, si existe, gana sobre el default del
pack/core -- gestionado por filesystem (mismo nivel que `catalog.json`/`docs/`/
`evals/` del tenant), no por el panel admin (ADR-0017 sigue vigente: sin
autoservicio de instrucciones en el panel). La allowlist sigue siendo el único
control de acceso; el override no agrega nombres nuevos, solo cambia DÓNDE se
busca uno ya autorizado.
"""

from __future__ import annotations

import functools
import re
import uuid
from pathlib import Path

from agentcore_contracts import AgentSpec

from core.agents.spec import load_active_spec
from core.shared.logger import get_logger

log = get_logger(__name__)

MAX_SIZE_BYTES = 8 * 1024  # ADR-0014: límite por playbook (anti-bloat/inyección)

# `PlaybookRef.name` es un `str` libre en el contrato (sin regex/validator) --
# la allowlist responde "¿puede este tenant pedir este nombre?", no "¿es este
# nombre un filename seguro?". Sin este chequeo, un `name` con "/"/".." que
# llegara al índice de un spec (ej. vía el fallback `raw_json` de /admin/spec)
# podría escapar `tenant_dir(slug)/playbooks/` -- o cualquier root de
# `playbook_roots()` -- y leer un archivo fuera de lo previsto, incluido el
# override PRIVADO de OTRO tenant (auditor-multitenant, ADR-0026). Mismos
# caracteres que ya usan todos los nombres de playbook reales del repo.
_SAFE_PLAYBOOK_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")

CORE_PLAYBOOKS = Path(__file__).resolve().parent.parent / "playbooks"
# Playbooks del kernel (ADR-0114 §2): fuera de `playbook_roots()`, ver `load_core_playbook`.
CORE_KERNEL_PLAYBOOKS = CORE_PLAYBOOKS / "kernel"
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
    spec_or_tenant: AgentSpec | uuid.UUID | str | None,
    name: str,
    *,
    tenant_slug: str | None = None,
) -> str | None:
    """Carga el contenido del playbook `name` si está en la allowlist del spec.

    Acepta un `AgentSpec` (evals/tests) o un tenant (resuelve su spec activo).
    `tenant_slug`: si el caller ya lo resolvió (ej. `build_system_blocks`,
    que arma varios playbooks por turno), evita una query repetida por cada
    uno -- si no se pasa, esta función lo resuelve ella misma (una query,
    aceptable en un call-site que no está en un loop).
    """
    from core.shared.tenant import get_current_tenant_id

    scope = get_current_tenant_id()
    if isinstance(spec_or_tenant, AgentSpec):
        spec = spec_or_tenant
    else:
        # La allowlist sale del spec del negocio pedido y el override del `tenant_scope`:
        # si fueran negocios distintos se mezclarían.
        if spec_or_tenant is not None and scope is not None and str(spec_or_tenant) != str(scope):
            raise PermissionError("load_playbook: el negocio pedido no es el del tenant_scope")
        spec = await load_active_spec(spec_or_tenant)
    allowed = {p.name for p in spec.playbooks}
    if name not in allowed:
        log.info("playbook_not_allowed", playbook=name, tenant_id=str(scope))
        return None
    if not _SAFE_PLAYBOOK_NAME.match(name):
        log.warning("playbook_name_unsafe", playbook=name, tenant_id=str(scope))
        return None

    # Override por tenant (ADR-0026): gana sobre el default del pack/core si
    # existe. Import local (mismo criterio que el resto del módulo) para no
    # acoplar el import de este archivo a core.onboarding/core.shared.tenant.
    # `tenant_slug`: `None` = "no me lo pasaron, resolverlo acá"; `""` =
    # "el caller ya lo intentó y no hay slug" (no reintentar la query por
    # cada playbook del loop de build_system_blocks si la BD está fallando).
    # El negocio sale del `tenant_scope`, nunca de `spec.tenantId`.
    slug = tenant_slug
    if slug is None:
        from core.shared.tenant import current_tenant_slug

        try:
            slug = await current_tenant_slug()
        except Exception:
            # La BD no responde: el override es una CAPA EXTRA, no la fuente
            # de verdad -- degradar al default del pack/core en vez de tumbar
            # el turno, mismo criterio fail-open que el resto de esta función.
            # Se loggea igual (a diferencia de un `pass` silencioso) para que
            # un fallo persistente de BD no quede invisible.
            log.warning("tenant_slug_resolve_failed", playbook=name, exc_info=True)
            slug = None
    if slug:
        from core.onboarding import tenant_dir

        override = tenant_dir(slug) / "playbooks" / f"{name}.md"
        if override.exists():
            try:
                content = override.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                log.warning(
                    "tenant_playbook_read_failed", playbook=name, tenant_slug=slug, exc_info=True
                )
            else:
                if len(content.encode("utf-8")) <= MAX_SIZE_BYTES:
                    return _strip_frontmatter(content)
                log.warning("tenant_playbook_too_large", playbook=name, tenant_slug=slug)

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


@functools.cache
def load_core_playbook(name: str) -> str:
    """Playbook del KERNEL (`core/playbooks/kernel/<name>.md`), sin allowlist ni override.

    Son textos que el kernel usa siempre, como los rótulos de la tarjeta de estado
    (ADR-0114 §2), y viajan con el binario. No están en `playbook_roots()` (la carpeta no
    se recorre: `glob("*.md")` no es recursivo), así que ningún negocio los puede pedir
    en su allowlist ni reemplazar con un override de `TENANTS_ROOT`.

    Fail-closed: un nombre inseguro, un archivo que falta o que pasa el tope lanzan (y un
    test de CI lo cubre), en lugar de devolver `None` y dejar al kernel sin sus rótulos.
    """
    if not _SAFE_PLAYBOOK_NAME.fullmatch(name):
        raise ValueError(f"nombre de playbook del kernel inseguro: {name!r}")
    content = (CORE_KERNEL_PLAYBOOKS / f"{name}.md").read_text(encoding="utf-8")
    if len(content.encode("utf-8")) > MAX_SIZE_BYTES:
        raise ValueError(f"playbook del kernel demasiado grande: {name}")
    return _strip_frontmatter(content)


async def list_playbooks() -> list[dict[str, str]]:
    """Índice de playbooks disponibles en disco (para el panel)."""
    result: list[dict[str, str]] = []
    for root in playbook_roots():
        for file in sorted(root.glob("*.md")):
            result.append({"name": file.stem, "path": str(file)})
    return result