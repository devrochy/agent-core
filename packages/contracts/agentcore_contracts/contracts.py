"""Contratos congelados de agent-core v1.0.0.

CONGELADO. Ver docs/adr/0011-contratos-python.md (reemplaza ADR-0003).
Cualquier cambio de forma requiere un ADR nuevo y un incremento major.
Este paquete NO importa de ningún otro paquete del monorepo.

Port 1:1 de packages/contracts/src/index.ts (TS) a Python. Los nombres de campo
preservan el camelCase del contrato v1.0.0 para no romper la forma congelada.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# ─────────────────────────────────────────────────────────────
# 1. Evento canónico de entrada
# ─────────────────────────────────────────────────────────────

ChannelKind = Literal["whatsapp", "instagram", "messenger", "telegram", "web", "email"]


class TextContent(BaseModel):
    type: Literal["text"]
    text: str


class ImageContent(BaseModel):
    type: Literal["image"]
    url: str
    mimeType: str
    caption: str | None = None


class AudioContent(BaseModel):
    type: Literal["audio"]
    url: str
    mimeType: str
    durationMs: int | None = None


class DocumentContent(BaseModel):
    type: Literal["document"]
    url: str
    mimeType: str
    filename: str | None = None


class LocationContent(BaseModel):
    type: Literal["location"]
    lat: float
    lng: float
    label: str | None = None


class InteractiveContent(BaseModel):
    type: Literal["interactive"]
    payload: dict[str, Any]


ContentPart = (
    TextContent
    | ImageContent
    | AudioContent
    | DocumentContent
    | LocationContent
    | InteractiveContent
)


class Contact(BaseModel):
    externalId: str
    displayName: str | None = None
    phone: str | None = None
    email: str | None = None


class InboundMessage(BaseModel):
    tenantId: str
    channelId: str
    channel: ChannelKind
    # Clave de idempotencia. Ver ADR-0007.
    externalMessageId: str
    # (channelId, contactExternalId). Clave del lock de conversación. Ver ADR-0006.
    conversationKey: str
    contact: Contact
    content: list[ContentPart]
    replyTo: str | None = None
    receivedAt: str
    # Payload original del canal, para auditoría. Nunca se pasa al modelo sin sanitizar.
    raw: Any


# ─────────────────────────────────────────────────────────────
# 2. Definición del agente — declarativa, versionada, sin código
# ─────────────────────────────────────────────────────────────

Tone = Literal["formal", "neutral", "cercano", "informal"]

EscalationState = Literal[
    "none",
    "requested",
    "queued",
    "assigned",
    "in_progress",
    "resolved",
    "abandoned",
    "expired",
]


class BusinessHourRange(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    day: int
    from_: str = Field(alias="from")
    to: str


class BusinessHours(BaseModel):
    tz: str
    ranges: list[BusinessHourRange]


class EscalationPolicy(BaseModel):
    triggers: list[str]
    autoEscalateAfterTurns: int | None = None
    autoEscalateOnLowConfidence: bool | None = None
    businessHours: BusinessHours | None = None
    outOfHoursMessage: str | None = None


class RagConfig(BaseModel):
    # v2.0.0 (ADR-0050): `collections` se eliminó (sin caso de uso). El recall se
    # parametriza por `topK` y `minScore`.
    topK: int
    minScore: float


class ToolBinding(BaseModel):
    tool: str
    enabled: bool
    # v2.0.0 (ADR-0050): `scopes`, `maxCallsPerConversation` y
    # `requiresConfirmation` tienen efecto real en el loop (autorización fina,
    # tope por conversación y human-in-the-loop). Defaults seguros para no
    # romper specs v1.
    scopes: list[str] = []
    maxCallsPerConversation: int | None = None
    requiresConfirmation: bool = False


class Persona(BaseModel):
    role: str
    tone: Tone
    warmth: Literal[0, 1, 2, 3, 4, 5]
    emojis: Literal["none", "sparse", "free"]
    maxLength: int
    forbiddenPhrases: list[str]
    # v2.0.0 (ADR-0050): `locale` se eliminó (el idioma vive en AppSettings.language).


class AgentModel(BaseModel):
    primary: str
    fallback: str | None = None
    router: str | None = None
    temperature: float = 0.3


class AgentMemory(BaseModel):
    # v2.0.0 (ADR-0050): `shortTermTurns` y `rag` tienen efecto real; `summarize`
    # se eliminó (sin caso de uso).
    shortTermTurns: int = 30
    rag: RagConfig | None = None


class PlaybookRef(BaseModel):
    # Índice de playbooks: nombre + descripción. El contenido se carga con load_playbook.
    name: str
    description: str


class AgentPolicies(BaseModel):
    # v2.0.0 (ADR-0050): `escalation` y `confirmBeforeWrite` se eliminaron (la
    # escalación operativa vive en AppSettings; horarios/SLA son Tanda 3).
    maxToolCallsPerTurn: int
    budgetUsdPerConversation: float | None = None


class AgentSpec(BaseModel):
    id: str
    version: str
    tenantId: str
    persona: Persona
    model: AgentModel
    memory: AgentMemory
    playbooks: list[PlaybookRef]
    tools: list[ToolBinding]
    policies: AgentPolicies
    packs: list[str]
    # v2.0.0 (ADR-0050): extensibilidad por tenant sin fork. Datos libres que un
    # motor de hooks/guardrails declarativos consumirá (Tanda 3). RESERVADO: hoy
    # se persiste y viaja en el spec, pero ningún motor lo lee todavía.
    extensions: dict[str, Any] = {}


# ─────────────────────────────────────────────────────────────
# 3. Herramienta
# ─────────────────────────────────────────────────────────────

SideEffect = Literal["read", "write", "irreversible"]

Scope = str

JSONSchema = dict[str, Any]


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool


class ToolResult[T](BaseModel):
    ok: bool
    data: T | None = None
    error: ToolError | None = None


class ToolDefinition[T](Protocol):
    name: str
    version: str
    # La lee el modelo en producción. Qué hace, cuándo usarla, cuándo NO.
    description: str
    input: JSONSchema
    output: JSONSchema
    sideEffect: SideEffect
    scopes: list[Scope]
    timeoutMs: int

    def idempotencyKey(self, input: Any) -> str | None: ...

    async def execute(self, input: Any, ctx: ToolContext) -> ToolResult[T]: ...


# ─────────────────────────────────────────────────────────────
# 4. Contexto de ejecución — lo único que un plugin puede tocar
# ─────────────────────────────────────────────────────────────


class DomainEvent(BaseModel):
    type: str
    tenantId: str
    payload: dict[str, Any]
    occurredAt: str


class ScopedDb(Protocol):
    def query(self, sql: str, params: list[Any] | None = None) -> list[Any]: ...

    def transaction(self, fn: Callable[[ScopedDb], Any]) -> Any: ...


class ScopedSecrets(Protocol):
    def get(self, name: str) -> str | None: ...


class ScopedHttp(Protocol):
    def fetch(self, url: str, init: dict[str, Any] | None = None) -> Any: ...


class Logger(Protocol):
    def debug(self, msg: str, meta: dict[str, Any] | None = None) -> None: ...

    def info(self, msg: str, meta: dict[str, Any] | None = None) -> None: ...

    def warn(self, msg: str, meta: dict[str, Any] | None = None) -> None: ...

    def error(self, msg: str, meta: dict[str, Any] | None = None) -> None: ...


class ToolContext(Protocol):
    tenantId: str
    agentId: str
    conversationId: str
    traceId: str
    # Ya filtrado por tenant vía RLS. No expone el pool crudo.
    db: ScopedDb
    # Solo los secretos declarados en el manifest del pack.
    secrets: ScopedSecrets
    # Allowlist de dominios del manifest.
    http: ScopedHttp
    logger: Logger

    def emit(self, event: DomainEvent) -> None: ...


# ─────────────────────────────────────────────────────────────
# 5. Resultado del turno
# ─────────────────────────────────────────────────────────────


class OutboundMessage(BaseModel):
    channel: ChannelKind
    content: list[ContentPart]
    replyTo: str | None = None


class ToolCallTrace(BaseModel):
    tool: str
    durationMs: int
    ok: bool
    errorCode: str | None = None


class AgentTurnResult(BaseModel):
    messages: list[OutboundMessage]
    stateChanges: list[DomainEvent]
    handoff: Handoff | None = None
    usage: Usage


class Handoff(BaseModel):
    reason: str
    toState: EscalationState


class Usage(BaseModel):
    model: str
    inputTokens: int
    outputTokens: int
    cachedTokens: int
    costUsd: float
    toolCalls: list[ToolCallTrace]


__all__ = [
    "AgentMemory",
    "AgentModel",
    "AgentPolicies",
    "AgentSpec",
    "AgentTurnResult",
    "AudioContent",
    "BusinessHourRange",
    "BusinessHours",
    "ChannelKind",
    "Contact",
    "ContentPart",
    "DocumentContent",
    "DomainEvent",
    "EscalationPolicy",
    "EscalationState",
    "Handoff",
    "ImageContent",
    "InboundMessage",
    "InteractiveContent",
    "JSONSchema",
    "LocationContent",
    "Logger",
    "OutboundMessage",
    "Persona",
    "PlaybookRef",
    "RagConfig",
    "Scope",
    "ScopedDb",
    "ScopedHttp",
    "ScopedSecrets",
    "SideEffect",
    "TextContent",
    "Tone",
    "ToolBinding",
    "ToolCallTrace",
    "ToolContext",
    "ToolDefinition",
    "ToolError",
    "ToolResult",
    "Usage",
]