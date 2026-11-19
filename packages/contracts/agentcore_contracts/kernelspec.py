"""Contrato `kernelspec/1`: el Coordinador de una conexión (ADR-0113 §3, HU-AOS-12).

Un agente de `kind = 'core'` versiona un KernelSpec (en `agent_versions`, con
`contract = 'kernelspec/1'`). Es la única fuente de la voz de la conexión, del
enrutamiento, de los límites de colaboración del turno y de los presupuestos por
conversación y del back-office (ADR-0113 §4). Hasta la release 1 de una conexión
(ADR-0117 §2), el lector tolerante del core lo deriva en memoria de la `persona` v2.

- Las reglas de enrutamiento son un DSL **sin regex**: prefijo de payload interactivo,
  palabras clave, un dato del cliente, la etapa en foco, el canal y una franja horaria.
- Los defaults son los del diseño de SPEC-26 (§5.2). Sin ids de modelo literales como
  default: un modelo que rota no puede obligar a un major (ADR-0086).

CONGELADO como el resto de `agentcore_contracts` (ADR-0011): `extra="forbid"` en todos los
modelos; la política para campos opcionales nuevos está en la adenda de ADR-0113
(2026-10-10). No importa de ningún otro paquete del monorepo.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentcore_contracts.contracts import ChannelKind, Tone
from agentcore_contracts.procedure import (
    INTERACTIVE_PREFIX_PATTERN,
    NAME_PATTERN,
    PROCEDURE_ID_PATTERN,
    Phrase,
)

# Key de un agente (`agents.key`, migración 0069).
AGENT_KEY_PATTERN = r"^[a-z0-9_]{2,40}$"
# Hora local `HH:MM` (24 h).
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
# Playbook del core o del negocio (mismo criterio que `agentspec.PLAYBOOK_NAME_PATTERN`).
PLAYBOOK_PATTERN = r"^[a-zA-Z0-9_-]{1,80}$"

Emojis = Literal["none", "sparse", "free"]
Confirmation = Literal["explicit", "interactive_button"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────────────────────────────────────
# Voz y episodio
# ─────────────────────────────────────────────────────────────


class Voice(_Strict):
    # Rol, tono, calidez y emojis de la conexión: los agentes la heredan.
    role: str = Field(default="asistente", min_length=1, max_length=200)
    tone: Tone = "cercano"
    warmth: Literal[0, 1, 2, 3, 4, 5] = 3
    emojis: Emojis = "sparse"


class Episode(_Strict):
    # Tramo sin pausas mayores que esto: libera al dueño y reasigna la release (ADR-0117).
    idleMinutes: int = Field(default=720, ge=5, le=60 * 24 * 30)


# ─────────────────────────────────────────────────────────────
# Enrutamiento
# ─────────────────────────────────────────────────────────────


class CustomerFact(_Strict):
    namespace: str = Field(pattern=NAME_PATTERN)
    key: str = Field(pattern=NAME_PATTERN)
    equals: Annotated[str, Field(max_length=100)] | int | float | bool


class FlowStage(_Strict):
    procedure: str = Field(pattern=PROCEDURE_ID_PATTERN)
    stage: str = Field(pattern=NAME_PATTERN)


class WithinHours(_Strict):
    # Franja en la zona horaria del negocio (ADR-0091). `from` > `to` cruza la medianoche.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from", pattern=HHMM_PATTERN)
    to: str = Field(pattern=HHMM_PATTERN)


class RoutingWhen(_Strict):
    # Todas las condiciones presentes deben cumplirse (AND). Al menos una.
    interactivePayloadPrefix: str | None = Field(default=None, pattern=INTERACTIVE_PREFIX_PATTERN)
    keywordAny: list[Phrase] | None = Field(default=None, min_length=1, max_length=30)
    customerFact: CustomerFact | None = None
    flowStage: FlowStage | None = None
    channel: ChannelKind | None = None
    withinHours: WithinHours | None = None

    @model_validator(mode="after")
    def _al_menos_una(self) -> RoutingWhen:
        if all(getattr(self, campo) is None for campo in type(self).model_fields):
            raise ValueError("la regla necesita al menos una condición")
        return self


class RoutingRule(_Strict):
    id: str = Field(pattern=NAME_PATTERN)
    when: RoutingWhen
    # Exactamente uno: a qué agente pasa o qué procedimiento inicia.
    agent: str | None = Field(default=None, pattern=AGENT_KEY_PATTERN)
    start: str | None = Field(default=None, pattern=PROCEDURE_ID_PATTERN)

    @model_validator(mode="after")
    def _un_destino(self) -> RoutingRule:
        if (self.agent is None) == (self.start is None):
            raise ValueError("la regla necesita exactamente uno de `agent` o `start`")
        return self


class TriggerThresholds(_Strict):
    embeddings: bool = True
    minScore: float = Field(default=0.45, ge=0, le=1)
    strongScore: float = Field(default=0.80, ge=0, le=1)
    margin: float = Field(default=0.15, ge=0, le=1)
    maxCandidates: int = Field(default=6, ge=1, le=20)

    @model_validator(mode="after")
    def _orden(self) -> TriggerThresholds:
        if self.strongScore < self.minScore:
            raise ValueError("strongScore no puede ser menor que minScore")
        return self


class Coprocessor(_Strict):
    # LLM acotado que desempata candidatos. `model` None = el del agente por defecto.
    whenCandidatesAbove: int = Field(default=8, ge=1, le=50)
    model: str | None = Field(default=None, max_length=100)
    thinking: bool = False
    maxCostUsd: float = Field(default=0.001, ge=0, le=1)
    playbook: str = Field(default="enrutamiento", pattern=PLAYBOOK_PATTERN)


class OwnerStability(_Strict):
    maxOwnerChangesPer6Turns: int = Field(default=2, ge=0, le=6)


class Routing(_Strict):
    defaultAgent: str = Field(default="general", pattern=AGENT_KEY_PATTERN)
    rules: list[RoutingRule] = Field(default=[], max_length=50)
    triggers: TriggerThresholds = Field(default_factory=TriggerThresholds)
    coprocessor: Coprocessor = Field(default_factory=Coprocessor)
    owner: OwnerStability = Field(default_factory=OwnerStability)
    shadowMode: bool = False


# ─────────────────────────────────────────────────────────────
# Procedimientos, modelo, colaboración y composición
# ─────────────────────────────────────────────────────────────


class KernelProcedures(_Strict):
    maxStack: int = Field(default=2, ge=1, le=5)
    # Solo puede exigir confirmación además de la etapa; lo irreversible siempre se confirma.
    confirmIrreversibleWith: Confirmation = "interactive_button"


class HighRiskStage(_Strict):
    thinking: bool = True
    # None = el modelo del agente; solo se declara lo que cambia en etapas de riesgo alto.
    model: str | None = Field(default=None, max_length=100)


class ModelPolicy(_Strict):
    highRiskStage: HighRiskStage = Field(default_factory=HighRiskStage)


class A2ALimits(_Strict):
    # Límites del turno; cada enlace (`agent_links`) solo puede bajarlos (ADR-0116).
    maxDepth: int = Field(default=3, ge=1, le=5)
    maxCallsPerTurn: int = Field(default=6, ge=0, le=20)
    turnBudgetUsd: float = Field(default=0.05, ge=0, le=10)
    turnBudgetTokens: int = Field(default=40_000, ge=0, le=1_000_000)


class Composition(_Strict):
    style: Literal["single_message"] = "single_message"
    maxQuestions: int = Field(default=1, ge=0, le=5)


class KernelPolicies(_Strict):
    # Uno solo por conversación: la conversación es de la conexión y tiene N agentes.
    budgetUsdPerConversation: float | None = Field(default=0.5, ge=0, le=1000)
    # Tope mensual del back-office de la conexión; solo baja `costo_llm_usd` del plan.
    backofficeBudgetUsdMonthly: float | None = Field(default=20, ge=0, le=100_000)


class KernelSpec(_Strict):
    contract: Literal["kernelspec/1"] = "kernelspec/1"
    voice: Voice = Field(default_factory=Voice)
    episode: Episode = Field(default_factory=Episode)
    routing: Routing = Field(default_factory=Routing)
    procedures: KernelProcedures = Field(default_factory=KernelProcedures)
    modelPolicy: ModelPolicy = Field(default_factory=ModelPolicy)
    a2a: A2ALimits = Field(default_factory=A2ALimits)
    composition: Composition = Field(default_factory=Composition)
    # Playbook de respuestas del Coordinador (textos de cierre y avisos).
    responses: str = Field(default="respuestas", pattern=PLAYBOOK_PATTERN)
    policies: KernelPolicies = Field(default_factory=KernelPolicies)


__all__ = [
    "A2ALimits",
    "Composition",
    "Coprocessor",
    "CustomerFact",
    "Episode",
    "FlowStage",
    "HighRiskStage",
    "KernelPolicies",
    "KernelProcedures",
    "KernelSpec",
    "ModelPolicy",
    "OwnerStability",
    "Routing",
    "RoutingRule",
    "RoutingWhen",
    "TriggerThresholds",
    "Voice",
    "WithinHours",
]
