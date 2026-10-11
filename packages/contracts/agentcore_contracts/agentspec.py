"""Contrato `agentspec/3`: un agente especialista o de back-office (ADR-0113 §2, HU-AOS-12).

Entra en el major 3.0.0. El AgentSpec v2 (`AgentSpec`, ADR-0050) sigue en `contracts.py` sin
cambios: el loop legacy, los presets y el alta lo leen y lo escriben hasta HU-AOS-46, y el
lector tolerante del core lo convierte a este contrato.

- `kind` vive en la tabla `agents`, no aquí: un agente `core` versiona un KernelSpec v1
  (`kernelspec.py`). La identidad (`id`, `tenantId`, `key`, versión) la da la fila.
- `declarativeTools` no contiene secretos: un header sensible (`Authorization`, `Cookie`,
  `X-Api-Key`) se declara por referencia, `{"secret": "<nombre>"}`, y su valor vive cifrado en
  la instalación `core` del scope (ADR-0118 §5). Un valor literal en esos headers no valida.
- `extensions` sigue reservado y sin semántica ("el contrato no miente", ADR-0050).

CONGELADO como el resto de `agentcore_contracts` (ADR-0011). Todos los modelos rechazan campos
desconocidos (`extra="forbid"`); la política para campos opcionales nuevos está en la adenda de
ADR-0113 (2026-10-10). Nombres de campo en camelCase. No importa de ningún otro paquete.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agentcore_contracts.procedure import (
    ACTION_PATTERN,
    NAME_PATTERN,
    PROCEDURE_ID_PATTERN,
    Phrase,
)

# ─────────────────────────────────────────────────────────────
# Formatos
# ─────────────────────────────────────────────────────────────

# Rango de versión de un procedimiento: `^1.0`, `~1.2.3`, `=0.0.0` o `1.2.3` (exacta).
# `=0.0.0` es el procedimiento abierto que arma el lector tolerante desde el flujo v2.
VERSION_RANGE_PATTERN = r"^[\^~=]?(0|[1-9][0-9]{0,5})(\.(0|[1-9][0-9]{0,5})){0,2}$"
# Nombre de un secreto de la instalación `core` (`transportadora_token`).
SECRET_NAME_PATTERN = r"^[a-z0-9_]{1,60}$"
# Id de una consulta expuesta (`ventas.pedido_abierto`).
QUERY_ID_PATTERN = r"^[a-z0-9_]{1,40}\.[a-z0-9_]{1,40}$"
# Nombre de una tool declarativa (el mismo que `core/mcp/declarative.py`).
DECLARATIVE_NAME_PATTERN = r"^[a-zA-Z0-9_]{1,60}$"
# Nombre de header HTTP (token de RFC 9110).
HEADER_NAME_PATTERN = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,80}$"
# Playbook: el mismo criterio que `core/agents/playbooks.py` (`_SAFE_PLAYBOOK_NAME`), con tope.
PLAYBOOK_NAME_PATTERN = r"^[a-zA-Z0-9_-]{1,80}$"

# Headers que nunca llevan un valor literal: solo `{"secret": "<nombre>"}` (ADR-0113 §2 y su
# adenda: los tres del ADR más las variantes habituales de credencial por header).
SENSITIVE_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "cookie",
        "x-api-key",
        "proxy-authorization",
        "api-key",
        "x-auth-token",
        "x-access-token",
        "x-api-token",
        "private-token",
    }
)

ReadScope = Literal["customer", "connection", "tenant"]
GuardrailKind = Literal["forbidden_phrase", "required_tool", "max_length"]
GuardrailAction = Literal["retry", "escalate"]
HttpMethod = Literal["GET", "POST"]

ShortText = Annotated[str, StringConstraints(max_length=1000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────────────────────────────────────
# Persona, modelo, memoria y playbooks
# ─────────────────────────────────────────────────────────────


class AgentPersonaV3(_Strict):
    # Rol, tono, calidez y emojis son la voz de la conexión (KernelSpec.voice).
    inheritConnectionVoice: bool = True
    # Tope blando: un reintento propio, nunca escala (adenda de ADR-0050, 2026-10-10).
    # 0 = sin tope (como en v2).
    maxLength: int = Field(default=500, ge=0, le=10_000)
    forbiddenPhrases: list[Annotated[str, StringConstraints(max_length=1000)]] = Field(
        default=[], max_length=100
    )


class AgentModelV3(_Strict):
    # Los mismos campos que en v2 (ADR-0050).
    primary: str = Field(min_length=1, max_length=100)
    fallback: str | None = Field(default=None, max_length=100)
    router: str | None = Field(default=None, max_length=100)
    temperature: float = Field(default=0.3, ge=0, le=2)


class RagConfigV3(_Strict):
    topK: int = Field(ge=1, le=100)
    minScore: float = Field(ge=0, le=1)


class AgentMemoryV3(_Strict):
    shortTermTurns: int = Field(default=30, ge=1, le=500)
    rag: RagConfigV3 | None = None
    readScopes: list[ReadScope] = Field(default=["customer", "connection", "tenant"], max_length=3)
    writeNamespaces: list[Annotated[str, Field(pattern=NAME_PATTERN)]] = Field(default=[], max_length=20)


class PlaybookRefV3(_Strict):
    # La allowlist de playbooks de ADR-0014 pasa a ser la de cada agente.
    name: str = Field(pattern=PLAYBOOK_NAME_PATTERN)
    description: str = Field(default="", max_length=300)
    # Habilidad que publica el playbook; None = core o `TENANTS_ROOT` del negocio.
    pack: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,60}$")


# ─────────────────────────────────────────────────────────────
# Procedimientos y lo que el agente expone
# ─────────────────────────────────────────────────────────────


class ProcedureOverrides(_Strict):
    # Los textos de un procedimiento solo se sobrescriben aquí (ADR-0113 §4).
    examples: list[Phrase] = Field(default=[], max_length=50)
    description: str | None = Field(default=None, max_length=1000)


class ProcedureRef(_Strict):
    id: str = Field(pattern=PROCEDURE_ID_PATTERN)
    version: str = Field(pattern=VERSION_RANGE_PATTERN)
    overrides: ProcedureOverrides | None = None


class ExposedQuery(_Strict):
    id: str = Field(pattern=QUERY_ID_PATTERN)
    description: str = Field(default="", max_length=500)
    outputSchema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class Exposes(_Strict):
    # Procedimientos que otros agentes pueden encargarle (ADR-0116).
    procedures: list[Annotated[str, Field(pattern=PROCEDURE_ID_PATTERN)]] = Field(default=[], max_length=50)
    consultas: list[ExposedQuery] = Field(default=[], max_length=30)


# ─────────────────────────────────────────────────────────────
# Acciones, guardrails y tools declarativas
# ─────────────────────────────────────────────────────────────


class ToolBindingV3(_Strict):
    # Consentimiento, límites y acciones fuera de etapa (fail-closed, ADR-0118 §3).
    tool: str = Field(pattern=ACTION_PATTERN)
    enabled: bool = True
    scopes: list[Annotated[str, StringConstraints(max_length=100)]] = Field(default=[], max_length=20)
    maxCallsPerConversation: int | None = Field(default=None, ge=1, le=10_000)
    # Solo para acciones fuera de etapa: dentro de un procedimiento confirma la etapa.
    requiresConfirmation: bool = False


class GuardrailRule(_Strict):
    # La forma que normaliza `validate_custom_rules` (ADR-0051), ahora de primer nivel.
    name: str = Field(min_length=1, max_length=60)
    kind: GuardrailKind
    action: GuardrailAction = "retry"
    phrases: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]] = Field(
        default=[], max_length=50
    )
    tool: str | None = Field(default=None, pattern=ACTION_PATTERN)
    value: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def _campos_por_tipo(self) -> GuardrailRule:
        if self.kind in ("forbidden_phrase", "required_tool") and not self.phrases:
            raise ValueError("forbidden_phrase y required_tool necesitan al menos una frase")
        if self.kind == "required_tool" and not self.tool:
            raise ValueError("required_tool necesita `tool`")
        if self.kind == "max_length" and self.value is None:
            raise ValueError("max_length necesita `value`")
        return self


class SecretRef(_Strict):
    # Nombre del secreto en la configuración cifrada de la instalación `core` del scope.
    secret: str = Field(pattern=SECRET_NAME_PATTERN)


# Sin CR, LF ni NUL: un salto de línea en un header es inyección de headers.
HeaderValue = Annotated[str, StringConstraints(max_length=8000, pattern=r"^[^\r\n\x00]*$")] | SecretRef


class DeclarativeTool(_Strict):
    # Tool HTTP del negocio sin código (ADR-0052), ahora de primer nivel.
    name: str = Field(pattern=DECLARATIVE_NAME_PATTERN)
    description: str = Field(default="", max_length=400)
    method: HttpMethod = "GET"
    url: str = Field(pattern=r"^https?://", max_length=2000)
    # FQDN públicos (anti-SSRF): el validador del core los comprueba (`public_fqdn`).
    allowedDomains: list[Annotated[str, StringConstraints(min_length=1, max_length=253)]] = Field(
        default=[], max_length=20
    )
    headers: dict[Annotated[str, Field(pattern=HEADER_NAME_PATTERN)], HeaderValue] = Field(
        default={}, max_length=30
    )
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    body: dict[str, Any] = {}

    @field_validator("headers")
    @classmethod
    def _sin_secretos_literales(cls, headers: dict[str, Any]) -> dict[str, Any]:
        literales = sorted(
            name for name, value in headers.items() if name.lower() in SENSITIVE_HEADERS and isinstance(value, str)
        )
        if literales:
            raise ValueError(
                f"los headers {', '.join(literales)} no admiten valores literales: "
                'decláralos como {"secret": "<nombre>"}'
            )
        return headers

    @model_validator(mode="after")
    def _secreto_solo_por_https(self) -> DeclarativeTool:
        if self.url.startswith("http://") and any(isinstance(v, SecretRef) for v in self.headers.values()):
            raise ValueError("una tool con secretos solo llama por https://: la credencial no viaja en claro")
        return self


# ─────────────────────────────────────────────────────────────
# Políticas y agente
# ─────────────────────────────────────────────────────────────


class AgentPoliciesV3(_Strict):
    # El presupuesto por conversación es del KernelSpec: la conversación tiene N agentes.
    maxToolCallsPerTurn: int = Field(default=10, ge=1, le=100)


class AgentSpecV3(_Strict):
    contract: Literal["agentspec/3"] = "agentspec/3"
    description: str = Field(default="", max_length=1000)
    persona: AgentPersonaV3 = Field(default_factory=AgentPersonaV3)
    model: AgentModelV3
    playbooks: list[PlaybookRefV3] = Field(default=[], max_length=60)
    procedures: list[ProcedureRef] = Field(default=[], max_length=60)
    exposes: Exposes = Field(default_factory=Exposes)
    memory: AgentMemoryV3 = Field(default_factory=AgentMemoryV3)
    tools: list[ToolBindingV3] = Field(default=[], max_length=300)
    guardrails: list[GuardrailRule] = Field(default=[], max_length=100)
    declarativeTools: list[DeclarativeTool] = Field(default=[], max_length=50)
    policies: AgentPoliciesV3 = Field(default_factory=AgentPoliciesV3)
    # Reservado y sin semántica (ADR-0050): ningún motor lo lee.
    extensions: dict[str, Any] = {}


__all__ = [
    "SENSITIVE_HEADERS",
    "AgentMemoryV3",
    "AgentModelV3",
    "AgentPersonaV3",
    "AgentPoliciesV3",
    "AgentSpecV3",
    "DeclarativeTool",
    "ExposedQuery",
    "Exposes",
    "GuardrailAction",
    "GuardrailKind",
    "GuardrailRule",
    "HttpMethod",
    "PlaybookRefV3",
    "ProcedureOverrides",
    "ProcedureRef",
    "RagConfigV3",
    "ReadScope",
    "SecretRef",
    "ToolBindingV3",
]
