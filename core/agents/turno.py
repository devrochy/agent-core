"""El agente del turno sobre `AgentSpecV3` y `KernelSpec` (ADR-0206 §1, HU-AOS-90).

El loop ejecuta la versión congelada del agente en foco (`agentspec/3`, o la vista de recepción del Coordinador,
ADR-0205 §5) y el KernelSpec del Coordinador de la release de la conversación. `AgenteDelTurno` los guarda tal cual
(validados) y expone las rutas que lee el turno, sin convertirlos a ningún otro contrato:

- `tools`, `playbooks`, `model` y `memory` son los del `AgentSpecV3` (mismos nombres que leía el loop);
- `persona` junta la voz del `KernelSpec.voice` (rol, tono, calidez y emojis) con el tope y las frases prohibidas
  del agente; `policies` junta el tope de tools del agente con el presupuesto por conversación del `KernelSpec`;
- `guardrails` y `declarativas` salen de los campos tipados del v3 (`guardrails`, `declarativeTools`), no de
  `extensions`;
- `packs` son las habilidades congeladas de la release (`skills` + `tenant_plugins`); vacío en la recepción;
- `version` es el rótulo del `usage_ledger`: `{key}@v3:{version}` o `coordinador@recepcion:{version}`.

Reemplaza la vista v2 de `spec_de_release` (retirada). Puro: no lee base ni disco. Fail-closed: un contenido congelado
que no valida el contrato es `VistaInvalida`. Una tool declarativa con referencias a secretos en la URL o el body se
omite: `core/mcp/declarative.py` solo resuelve referencias en headers (incluirla mandaría el placeholder a un tercero).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agentcore_contracts import AgentSpecV3, KernelSpec
from pydantic import ValidationError

from core.agents.credenciales import referencias_en_body, referencias_en_url
from core.shared import release_vigente
from core.shared.logger import get_logger
from core.shared.release_vigente import AgenteCongelado

log = get_logger(__name__)

PREFIJO_RECEPCION = "coordinador@recepcion"


class TienePlaybooks(Protocol):
    """Lo que necesita `load_playbook` para la allowlist: el agente del turno, un `AgentSpec` (evals) o
    `SoloRespuestas`."""

    @property
    def playbooks(self) -> Sequence[Any]: ...


class VistaInvalida(RuntimeError):
    """El contenido congelado de la release no cabe en los contratos v3 (o falta el Coordinador)."""


@dataclass(frozen=True)
class PersonaDelTurno:
    role: str
    tone: str
    warmth: int
    emojis: Literal["none", "sparse", "free"]
    maxLength: int
    forbiddenPhrases: tuple[str, ...]


@dataclass(frozen=True)
class PoliticasDelTurno:
    maxToolCallsPerTurn: int
    budgetUsdPerConversation: float | None


@dataclass(frozen=True)
class AgenteDelTurno:
    v3: AgentSpecV3
    kernel: KernelSpec
    agente: AgenteCongelado
    packs: tuple[str, ...] = ()
    omitidas: frozenset[str] = field(default_factory=frozenset)

    # ---- lo que lee el turno (mismas rutas que antes)
    @property
    def recepcion(self) -> bool:
        """El Coordinador atiende con su vista de recepción (ADR-0205 §5)."""
        return self.agente.role == "core"

    @property
    def version(self) -> str:
        if self.recepcion:
            return f"{PREFIJO_RECEPCION}:{self.agente.version}"
        return f"{self.agente.key}@v3:{self.agente.version}"

    @property
    def tools(self) -> list[Any]:
        return self.v3.tools

    @property
    def playbooks(self) -> list[Any]:
        return self.v3.playbooks

    @property
    def model(self) -> Any:
        return self.v3.model

    @property
    def memory(self) -> Any:
        return self.v3.memory

    @property
    def persona(self) -> PersonaDelTurno:
        voz = self.kernel.voice
        return PersonaDelTurno(
            role=voz.role,
            tone=voz.tone,
            warmth=voz.warmth,
            emojis=voz.emojis,
            maxLength=self.v3.persona.maxLength,
            forbiddenPhrases=tuple(self.v3.persona.forbiddenPhrases),
        )

    @property
    def policies(self) -> PoliticasDelTurno:
        return PoliticasDelTurno(
            maxToolCallsPerTurn=self.v3.policies.maxToolCallsPerTurn,
            budgetUsdPerConversation=self.kernel.policies.budgetUsdPerConversation,
        )

    @property
    def guardrails(self) -> list[dict[str, Any]]:
        """Las reglas del agente en la forma que valida `run_guardrails`."""
        return [g.model_dump(mode="json", exclude_none=True) for g in self.v3.guardrails]

    @property
    def declarativas(self) -> list[dict[str, Any]]:
        """Las tools declarativas del agente en la forma que ejecuta `core/mcp/declarative.py` (sin las omitidas)."""
        salida: list[dict[str, Any]] = []
        for t in self.v3.declarativeTools:
            if t.name in self.omitidas:
                continue
            salida.append(
                {
                    "name": t.name,
                    "description": t.description,
                    "method": t.method,
                    "url": t.url,
                    "allowed_domains": list(t.allowedDomains),
                    # Un `SecretRef` queda como `{"secret": nombre}`: `declarative.py` lo resuelve al ejecutar.
                    "headers": {k: (v if isinstance(v, str) else {"secret": v.secret}) for k, v in t.headers.items()},
                    "parameters": t.parameters,
                    "body": t.body,
                }
            )
        return salida

    def tamano_en_bytes(self) -> int:
        """Bytes aproximados para el tope de la caché de compilados."""
        return len(self.v3.model_dump_json()) + len(self.kernel.model_dump_json())


@dataclass(frozen=True)
class _Playbook:
    name: str


@dataclass(frozen=True)
class SoloRespuestas:
    """Allowlist mínima para leer el playbook `respuestas` fuera del turno (p. ej. el escalamiento del ingreso, que
    corre antes de fijar la release): el texto y el override del negocio, sin pasar por `agent_specs` (ADR-0206 §4)."""

    playbooks: tuple[_Playbook, ...] = (_Playbook("respuestas"),)


def validar_componentes(agente: AgenteCongelado, kernel: AgenteCongelado | None) -> tuple[AgentSpecV3, KernelSpec]:
    if kernel is None:
        raise VistaInvalida("la release no trae el KernelSpec del Coordinador")
    try:
        v3 = AgentSpecV3.model_validate(dict(agente.content))
        ks = KernelSpec.model_validate(dict(kernel.content))
    except ValidationError as exc:
        rutas = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        raise VistaInvalida(f"la versión congelada no pasa el contrato: {', '.join(rutas)[:300]}") from None
    return v3, ks


def declarativas_omitidas(v3: AgentSpecV3) -> list[str]:
    """Nombres de las tools declarativas que el turno NO ofrece: las que llevan referencias a secretos en la URL o en
    el body (el handler solo resuelve headers; ADR-0206 §2)."""
    return sorted(t.name for t in v3.declarativeTools if referencias_en_url(t.url) or referencias_en_body(t.body))


def agente_del_turno(
    agente: AgenteCongelado,
    kernel: AgenteCongelado | None,
    *,
    skills: tuple[str, ...] | list[str],
    tenant_plugins: tuple[str, ...] | list[str],
) -> AgenteDelTurno:
    """El agente del turno desde la versión congelada del foco y el KernelSpec de la release."""
    v3, ks = validar_componentes(agente, kernel)
    omitidas = frozenset(declarativas_omitidas(v3))
    if omitidas:
        log.warning("declarativa_omitida_refs_url_body", agent_key=agente.key, tools=sorted(omitidas))
    # La vista de recepción del Coordinador no tiene packs (ADR-0205 §5).
    packs = () if agente.role == "core" else tuple(sorted({*skills, *tenant_plugins}))
    return AgenteDelTurno(v3=v3, kernel=ks, agente=agente, packs=packs, omitidas=omitidas)


def del_turno() -> AgenteDelTurno | None:
    """El agente del turno de la release vigente, o None sin release (el turno ya cerró en silencio)."""
    actual = release_vigente.vigente()
    if actual is None or actual.foco is None:
        return None
    turno = agente_del_turno(
        actual.foco, release_vigente.kernel_del_turno(), skills=actual.skills, tenant_plugins=actual.tenant_plugins
    )
    log.info("turn_spec_from_release", agent_key=actual.foco.key, agent_version=actual.foco.version)
    return turno


__all__ = [
    "AgenteDelTurno",
    "PersonaDelTurno",
    "PoliticasDelTurno",
    "SoloRespuestas",
    "TienePlaybooks",
    "VistaInvalida",
    "agente_del_turno",
    "declarativas_omitidas",
    "del_turno",
    "validar_componentes",
]
