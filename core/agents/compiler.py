"""Lo compilado de una release por agente (HU-AOS-35, adenda de ADR-0117 §7).

Las versiones son inmutables: la vista v2 del agente en foco, el flujo de la release y los bloques
fijos del prompt se arman UNA vez por `(tenant_id, release_id, agent_version_id)` y se guardan en
`ReleaseCongelada` (`core/releases/cargar.py`), que ya tiene tope de bytes, evicción y la guarda de
`tenant_scope`. Aquí solo vive la función que compila: pura respecto del turno (recibe todo por
parámetro y nunca lee la ranura de `release_vigente` ni el disco del negocio).

- El flujo se resuelve contra una fuente estática: el catálogo de acciones del binario de los
  packs de la release más las declarativas de la versión (no `mcp_host`, ni la salud del plugin, ni
  las conexiones). Lo que falte en un turno lo dice `aviso_no_disponible` en el bloque dinámico.
- Un error de lectura de un playbook o de un `flow.json` que existe lanza `ErrorDeCompilacion`:
  quien llama no cachea y arma ese turno como siempre. Un nombre que no existe es determinista.
- Las variables de los playbooks salen de la release: los `app_settings` congelados (las cuatro
  claves de `CAMPOS_DE_PROMPT`) y la persona de la versión. Si falta alguna clave no se compila
  (el turno leería `AppSettings` en vivo y eso no es estable).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from typing import TYPE_CHECKING, Any

from core.shared.logger import get_logger

if TYPE_CHECKING:
    from agentcore_contracts import AgentSpec

    from core.agents.flow import ResolvedFlow
    from core.shared.release_vigente import AgenteCongelado

log = get_logger(__name__)

PLAYBOOK_NO_DISPONIBLE = "no-disponible"


class ErrorDeCompilacion(Exception):
    """No se pudo compilar de forma determinista (error de lectura, vista inválida o datos que
    faltan en la release): el turno se arma como siempre y no se cachea nada."""


@dataclass(frozen=True)
class Compilado:
    """Lo compilado de un agente de una release. `bloques_fijos` es el prefijo del prompt (sin el
    bloque dinámico) y `flujo` el de la release completa (None si no declara flujo)."""

    _spec: AgentSpec
    flujo: ResolvedFlow | None
    flow_summary: str
    bloques_fijos: tuple[str, ...]
    bytes: int

    def spec(self) -> AgentSpec:
        """Copia de la vista: lo compilado es de todos los turnos de la release y nadie debe
        mutarlo (mismo criterio que las copias de `cargar.py`)."""
        return self._spec.model_copy(deep=True)


def herramientas_estaticas(spec: AgentSpec) -> set[str]:
    """Las herramientas que la release habilita, sin filtros del turno: las acciones del binario
    de los packs habilitados (el core incluido) más las declarativas de la versión."""
    from core.agents.spec import enabled_packs
    from core.kernel.procedures import catalogo_de_acciones
    from core.mcp.declarative import tools_for_spec
    from core.plugin_manager import plugin_manager

    habilitados = enabled_packs(spec)
    catalogo = catalogo_de_acciones(plugin_manager.list_modules())
    nombres = {nombre for nombre, info in catalogo.items() if info.owner in habilitados}
    return nombres | {t.name for t in tools_for_spec(spec)}


async def compilar(
    *,
    tenant_id: uuid.UUID,
    foco: AgenteCongelado,
    kernel: AgenteCongelado | None,
    skills: Iterable[str],
    tenant_plugins: Iterable[str],
    playbooks: Mapping[str, str],
    flow_tenant: Any,
    app_settings: Mapping[str, str | None],
) -> Compilado:
    """Compila el agente `foco` de una release. Lanza `ErrorDeCompilacion` si no se puede de forma
    determinista."""
    from core.agents.flow import ErrorDeLecturaDeFlujo, render_flow, resolve_flow
    from core.agents.playbooks import ErrorDeLectura
    from core.agents.spec_de_release import spec_v2_desde_release
    from core.llm.prompt_builder import PromptContext, bloques_fijos
    from core.shared.release_vigente import CAMPOS_DE_PROMPT

    faltan = [c for c in CAMPOS_DE_PROMPT if c not in app_settings]
    if faltan:
        raise ErrorDeCompilacion(f"la release no congela {faltan}")
    try:
        spec = spec_v2_desde_release(
            foco, kernel, tenant_id=tenant_id, skills=tuple(skills), tenant_plugins=tuple(tenant_plugins)
        )
    except Exception as exc:  # la vista no valida: el turno sigue como siempre (y loggea allí)
        raise ErrorDeCompilacion("vista inválida") from exc
    try:
        flujo = await resolve_flow(
            spec, slug="", available=herramientas_estaticas(spec), flow_tenant=flow_tenant, estricto=True
        )
        flow_summary = render_flow(flujo) if flujo is not None else ""
        # Los mismos valores que el loop pone en `PromptContext` con release (`con_valores_de_release`).
        ctx = PromptContext(
            assistant_name=app_settings["assistant_name"],  # type: ignore[arg-type]
            business_description=app_settings["business_description"],  # type: ignore[arg-type]
            language=app_settings["language"],  # type: ignore[arg-type]
            tone=spec.persona.tone,
            warmth=spec.persona.warmth,
            emojis=spec.persona.emojis,
            max_length=spec.persona.maxLength,
            brand_voice=app_settings["brand_voice"],  # type: ignore[arg-type]
            flow_summary=flow_summary,
        )
        bloques = await bloques_fijos(spec, ctx, tenant_slug="", overrides=playbooks, estricto=True)
    except (ErrorDeLectura, ErrorDeLecturaDeFlujo) as exc:
        raise ErrorDeCompilacion("error de lectura") from exc
    tamano = sum(len(b.encode("utf-8")) for b in bloques) + len(spec.model_dump_json().encode("utf-8"))
    return Compilado(
        _spec=spec, flujo=flujo, flow_summary=flow_summary, bloques_fijos=tuple(bloques), bytes=tamano
    )


@cache
def _textos_no_disponible() -> Mapping[str, str]:
    """Fail-closed: un playbook roto lanza y lo cubre un test de CI."""
    from core.agents.playbooks import load_core_playbook
    from core.kernel.fuera_de_horario import IDIOMAS
    from core.kernel.procedures.textos import secciones

    crudas = secciones(load_core_playbook(PLAYBOOK_NO_DISPONIBLE))
    claves = ["encabezado", *IDIOMAS]
    faltan = [c for c in claves if len(crudas.get(c, [])) != 1]
    if faltan:
        raise ValueError(f"no-disponible incompleto: {faltan}")
    textos = {c: crudas[c][0] for c in claves}
    for idioma in IDIOMAS:
        textos[idioma].format(detalle="x")
    return textos


def aviso_no_disponible(flujo: ResolvedFlow | None, disponibles: set[str], language: str | None) -> str:
    """Las etapas del flujo compilado con herramientas que no están en las candidatas del turno
    (después de la salud y las conexiones, antes del motor). "" si no falta nada."""
    if flujo is None:
        return ""
    from core.kernel.fuera_de_horario import idioma_del_negocio

    partes: list[str] = []
    for seccion in flujo.sections:
        faltan = [t for t in seccion.tools if t not in disponibles]
        if faltan:
            partes.append(f"{seccion.label} ({', '.join(faltan)})")
    if not partes:
        return ""
    t = _textos_no_disponible()
    return f"{t['encabezado']}\n{t[idioma_del_negocio(language)].format(detalle='; '.join(partes))}"


__all__ = [
    "Compilado",
    "ErrorDeCompilacion",
    "aviso_no_disponible",
    "compilar",
    "herramientas_estaticas",
]
