"""Contrato `procedure/1` (ADR-0113, HU-AOS-03).

Un procedimiento (superpoder) es el recorrido declarativo y versionado que publica una
habilidad: disparadores, datos, etapas con sus acciones permitidas, confirmación, cierre,
recuperación, afirmaciones y escenarios de prueba. El motor que lo ejecuta llega después
(HU-AOS-05); este paquete solo fija la forma.

CONGELADO como el resto de `agentcore_contracts`: cambiar la forma exige un ADR y un
incremento major (ADR-0011). Entra como 2.1.0 porque es aditivo; el 3.0.0 de ADR-0113
llega con AgentSpec v3 (HU-AOS-12). Este módulo no importa de ningún otro paquete.

Los nombres de campo van en camelCase, igual que el resto de los contratos. Todos los
modelos rechazan campos desconocidos (`extra="forbid"`): un campo mal escrito es un
error del autor, no un dato que se ignora en silencio.

Los patrones y topes son un superconjunto de los de `flow.json` v2
(`core/agents/flow.py`): toda sección que el flujo acepta se convierte en un
procedimiento válido.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ─────────────────────────────────────────────────────────────
# Formatos
# ─────────────────────────────────────────────────────────────

# Nombre de etapa o de dato. Igual que el id de sección de `flow.json` v2.
NAME_PATTERN = r"^[a-z0-9_]{1,40}$"
# Nombre de acción (tool). Igual que el de `flow.json` v2.
ACTION_PATTERN = r"^[a-zA-Z0-9_]{1,80}$"
# Dueño: `core`, `tenant` o el id de una habilidad (`pack_commerce`).
OWNER_PATTERN = r"^[a-z0-9_]{1,60}$"
# `<dueño>.<nombre>`: `pack_commerce.contratar_plan`, `core.saludo`, `tenant.planes`.
PROCEDURE_ID_PATTERN = r"^[a-z0-9_]{1,60}\.[a-z0-9_]{1,40}$"
SEMVER_PATTERN = r"^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$"
# Prefijo del id de un botón o de una fila de lista de WhatsApp (`plan:`, `hora:`).
INTERACTIVE_PREFIX_PATTERN = r"^[a-z0-9_-]{1,30}:$"
# Quién llena un dato sin pasar por el LLM:
# - `action:<acción>`: el resultado de la acción (solo el servidor);
# - `interactive:<prefijo>:`: el payload del botón o la lista que toca el cliente;
# - `content:<tipo>`: lo que envía el cliente (ubicación, imagen, documento, audio).
SET_BY_PATTERN = (
    r"^(action:[a-zA-Z0-9_]{1,80}"
    r"|interactive:[a-z0-9_-]{1,30}:"
    r"|content:(location|image|document|audio))$"
)
# Función del servidor de la habilidad que valida o resuelve un dato.
RESOLVER_PATTERN = r"^[a-z0-9_]{1,60}\.[a-z0-9_]{1,60}$"
# Fuente de una afirmación: `action:<acción>[.<campo>]` o `slot:<dato>`.
SOURCE_PATTERN = r"^(action:[a-zA-Z0-9_]{1,80}(\.[a-zA-Z0-9_]{1,60})?|slot:[a-z0-9_]{1,40})$"
# Fuente de candidatos: `action:<acción>[.<campo>]` o `resolver:<dueño>.<función>`.
CANDIDATES_SOURCE_PATTERN = (
    r"^(action:[a-zA-Z0-9_]{1,80}(\.[a-zA-Z0-9_]{1,60})?"
    r"|resolver:[a-z0-9_]{1,60}\.[a-z0-9_]{1,60})$"
)
# Evento de dominio: `<dueño>.<evento>` (`pack_payments.pago_confirmado`).
EVENT_PATTERN = r"^[a-z0-9_]{1,60}\.[a-z0-9_.]{1,80}$"
EVAL_PATTERN = r"^[a-z0-9_-]{1,80}$"

ContentKind = Literal["location", "image", "document", "audio"]
SlotType = Literal[
    "string", "integer", "number", "boolean", "date", "time", "datetime", "location", "media"
]
SlotFormat = Literal["email", "uri", "phone"]
DataClass = Literal["public", "pii", "payment"]
Confirmation = Literal["explicit", "interactive_button"]
Risk = Literal["low", "high"]
ClaimKind = Literal["amount", "link", "status"]
ActionErrorRecovery = Literal["explicar_y_reintentar_una_vez", "explicar", "relevo_humano"]
TimeoutRecovery = Literal["explicar", "relevo_humano"]

Name = Annotated[str, Field(pattern=NAME_PATTERN)]
ActionName = Annotated[str, Field(pattern=ACTION_PATTERN)]
EventName = Annotated[str, Field(pattern=EVENT_PATTERN)]
EvalName = Annotated[str, Field(pattern=EVAL_PATTERN)]
Phrase = Annotated[str, Field(min_length=1, max_length=300)]
EnumValue = Annotated[str, Field(min_length=1, max_length=100)]
Source = Annotated[str, Field(pattern=SOURCE_PATTERN)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─────────────────────────────────────────────────────────────
# Disparadores
# ─────────────────────────────────────────────────────────────


class Triggers(_Strict):
    # Qué quiere el cliente cuando este procedimiento aplica. Dato de la habilidad.
    description: str = Field(default="", max_length=1000)
    # Frases de ejemplo (no regex, ADR-0025).
    examples: list[Phrase] = Field(default=[], max_length=50)
    # Un botón o fila de lista cuyo id empieza así inicia el procedimiento.
    interactivePayloadPrefix: str | None = Field(default=None, pattern=INTERACTIVE_PREFIX_PATTERN)


# ─────────────────────────────────────────────────────────────
# Datos (slots)
# ─────────────────────────────────────────────────────────────


class SlotCondition(_Strict):
    # Condición de igualdad sobre otro dato: `{"slot": "metodo_entrega", "equals": "domicilio"}`.
    slot: str = Field(pattern=NAME_PATTERN)
    equals: Annotated[str, Field(max_length=100)] | int | float | bool


class Candidates(_Strict):
    # De dónde salen las opciones que el cliente elige (una lista de WhatsApp tiene
    # hasta 10 filas). La elección llega por el payload de `setBy: interactive:`.
    source: str = Field(pattern=CANDIDATES_SOURCE_PATTERN)
    max: int = Field(default=10, ge=1, le=10)


class Slot(_Strict):
    type: SlotType | None = None
    enum: list[EnumValue] | None = Field(default=None, min_length=1, max_length=50)
    format: SlotFormat | None = None
    default: Any = None
    required: bool = False
    # Obligatorio solo si se cumple la condición (la dirección, solo con domicilio).
    requiredWhen: SlotCondition | None = None
    # El cliente confirma el valor antes de seguir.
    confirm: bool = False
    dataClass: DataClass = "public"
    resolver: str | None = Field(default=None, pattern=RESOLVER_PATTERN)
    setBy: str | None = Field(default=None, pattern=SET_BY_PATTERN)
    candidates: Candidates | None = None


# ─────────────────────────────────────────────────────────────
# Etapas
# ─────────────────────────────────────────────────────────────


class ExitWhen(_Strict):
    # Datos que deben estar llenos (los `requiredWhen` que no aplican cuentan como llenos).
    slots: list[Name] = Field(default=[], max_length=40)
    # Datos que el cliente confirmó.
    confirmed: list[Name] = Field(default=[], max_length=40)
    # Acción de la etapa que debe terminar (con `ok`, si se indica).
    action: str | None = Field(default=None, pattern=ACTION_PATTERN)
    ok: bool | None = None


class Stage(_Strict):
    id: str = Field(pattern=NAME_PATTERN)
    goal: str = Field(default="", max_length=1000)
    # Acciones permitidas en la etapa: fuera de ellas (y de las del core y los comandos)
    # el motor no ofrece ni ejecuta nada.
    actions: list[ActionName] = Field(default=[], max_length=60)
    # Acciones de lectura que deben cumplirse antes de avanzar.
    preconditions: list[ActionName] = Field(default=[], max_length=10)
    confirmation: Confirmation | None = None
    risk: Risk = "low"
    # Sin `exitWhen` la etapa es terminal.
    exitWhen: ExitWhen | None = None
    # Etapa siguiente al salir; por defecto, la que sigue en la lista.
    next: str | None = Field(default=None, pattern=NAME_PATTERN)


# ─────────────────────────────────────────────────────────────
# Cierre, recuperación y afirmaciones
# ─────────────────────────────────────────────────────────────


class Completion(_Strict):
    # Eventos de dominio que completan la tarea.
    events: list[EventName] = Field(default=[], max_length=20)
    returnControl: bool = True


class Recovery(_Strict):
    actionError: ActionErrorRecovery = "explicar"
    timeout: TimeoutRecovery = "relevo_humano"


class Claim(_Strict):
    kind: ClaimKind
    # Fuentes del servidor que respaldan lo que se dice (monto, link).
    mustMatch: list[Source] = Field(default=[], max_length=10)
    # Frases literales que dan por hecha una acción (no regex, ADR-0025).
    phrases: list[Phrase] = Field(default=[], max_length=30)
    # Qué tiene que existir para poder decirlo (`slot:pedido_id`, `action:crear_pedido`).
    requires: str | None = Field(default=None, pattern=SOURCE_PATTERN)


# ─────────────────────────────────────────────────────────────
# Procedimiento
# ─────────────────────────────────────────────────────────────


class Procedure(_Strict):
    contract: Literal["procedure/1"] = "procedure/1"
    id: str = Field(pattern=PROCEDURE_ID_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    title: str = Field(max_length=200)
    triggers: Triggers = Field(default_factory=Triggers)
    slots: dict[Name, Slot] = Field(default={}, max_length=40)
    stages: list[Stage] = Field(min_length=1, max_length=30)
    completion: Completion | None = None
    recovery: Recovery | None = None
    claims: list[Claim] = Field(default=[], max_length=30)
    # Escenarios de usuario simulado de la habilidad (gate de pass^k, ADR-0119).
    evals: list[EvalName] = Field(default=[], max_length=50)

    @property
    def owner(self) -> str:
        """Dueño del procedimiento: la parte antes del punto del `id`."""
        return self.id.split(".", 1)[0]


__all__ = [
    "Candidates",
    "Claim",
    "ClaimKind",
    "Completion",
    "Confirmation",
    "ContentKind",
    "DataClass",
    "ExitWhen",
    "Procedure",
    "Recovery",
    "Risk",
    "Slot",
    "SlotCondition",
    "SlotFormat",
    "SlotType",
    "Stage",
    "Triggers",
]
