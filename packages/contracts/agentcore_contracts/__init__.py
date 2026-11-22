"""agentcore-contracts — v4.0.0 (Python).

Contratos congelados de agent-core. Ver docs/adr/0011-contratos-python.md.

- v2.0.0: AgentSpec v2 (ADR-0050).
- v2.1.0: `procedure/1`, aditivo (ADR-0113, HU-AOS-03).
- v3.0.0: `agentspec/3` (`AgentSpecV3`) y `kernelspec/1` (`KernelSpec`), ADR-0113 (HU-AOS-12).
  Se retira el espejo TS de `packages/contracts/src/` (nadie lo consumía: los tipos del panel
  salen del OpenAPI, ADR-0077). `AgentSpec` sigue siendo el **v2**: lo leen el loop legacy,
  los presets y el alta hasta HU-AOS-46; el lector tolerante del core lo convierte a v3.
- v3.1.0: `AgentSpecV3.knowledge` (`KnowledgeConfigV3`), opcional con default: un minor según la
  adenda de ADR-0113 del 2026-10-20 (ADR-0145, HU-AOS-49).
- v3.2.0: `ExposedQuery.action`, opcional con default (adenda HU-AOS-19 de ADR-0113): la acción de
  lectura que responde la consulta.
- v4.0.0: sale el `AgentSpec` v2 y los tipos que solo usaba él (`Persona`, `AgentModel`, `AgentMemory`,
  `PlaybookRef`, `AgentPolicies`, `ToolBinding`, `RagConfig`): ADR-0206 §8, HU-AOS-91. El agente es
  `AgentSpecV3` y el Coordinador `KernelSpec`; `Tone`, `Emojis` y `SideEffect` siguen.
"""

from agentcore_contracts.agentspec import (
    SENSITIVE_HEADERS,
    AgentSpecV3,
    DeclarativeTool,
    GuardrailRule,
    ProcedureRef,
    SecretRef,
    ToolBindingV3,
)
from agentcore_contracts.contracts import (
    AgentTurnResult,
    AudioContent,
    BusinessHourRange,
    BusinessHours,
    ChannelKind,
    Contact,
    ContentPart,
    DocumentContent,
    DomainEvent,
    EscalationPolicy,
    EscalationState,
    Handoff,
    ImageContent,
    InboundMessage,
    InteractiveContent,
    JSONSchema,
    LocationContent,
    Logger,
    OutboundMessage,
    Scope,
    ScopedDb,
    ScopedHttp,
    ScopedSecrets,
    SideEffect,
    TextContent,
    Tone,
    ToolCallTrace,
    ToolContext,
    ToolDefinition,
    ToolError,
    ToolResult,
    Usage,
)
from agentcore_contracts.kernelspec import KernelSpec, RoutingRule, Voice
from agentcore_contracts.procedure import (
    Candidates,
    Claim,
    Completion,
    ContentKind,
    ExitWhen,
    Procedure,
    Recovery,
    Slot,
    SlotCondition,
    Stage,
    Triggers,
)

__version__ = "4.0.0"

__all__ = [
    "SENSITIVE_HEADERS",
    "AgentSpecV3",
    "AgentTurnResult",
    "AudioContent",
    "BusinessHourRange",
    "BusinessHours",
    "Candidates",
    "ChannelKind",
    "Claim",
    "Completion",
    "Contact",
    "ContentKind",
    "ContentPart",
    "DeclarativeTool",
    "DocumentContent",
    "DomainEvent",
    "EscalationPolicy",
    "EscalationState",
    "ExitWhen",
    "GuardrailRule",
    "Handoff",
    "ImageContent",
    "InboundMessage",
    "InteractiveContent",
    "JSONSchema",
    "KernelSpec",
    "LocationContent",
    "Logger",
    "OutboundMessage",
    "Procedure",
    "ProcedureRef",
    "Recovery",
    "RoutingRule",
    "Scope",
    "ScopedDb",
    "ScopedHttp",
    "ScopedSecrets",
    "SecretRef",
    "SideEffect",
    "Slot",
    "SlotCondition",
    "Stage",
    "TextContent",
    "Tone",
    "ToolBindingV3",
    "ToolCallTrace",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolResult",
    "Triggers",
    "Usage",
    "Voice",
]
