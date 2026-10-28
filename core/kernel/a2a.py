"""Guarda A2A y ledger de las operaciones entre agentes (ADR-0116 §3-§4, adenda HU-AOS-18).

Cada operación de un agente sobre otro (`delegate`, `handoff`, `query`) pasa por `evaluar`, que es
**pura y determinista** (sin LLM ni base): mira el grafo congelado en la release, los límites del
KernelSpec del Coordinador y el estado del turno, y devuelve la primera razón para negarla:

1. `turn_closed`  el turno ya quedó en `loop_blocked` o `budget_exhausted`;
2. `cycle`        el destino ya está en la pila de llamadas del turno (o es el mismo agente);
3. `no_link`      no hay enlace `from → to`, o no permite el modo o la intención;
4. `depth`        la profundidad pasaría `min(enlace.max_depth, a2a.maxDepth)`;
5. `repeat`       la misma operación con el mismo payload ya se pidió en el turno;
6. `calls`        el turno llegó a `a2a.maxCallsPerTurn` (las denegaciones también cuentan);
7. `owner_frozen` un `handoff` con los cambios de dueño en el límite del router (HU-AOS-17);
8. `data_class`   la operación lleva una clase de datos que el enlace no permite;
9. `budget`       la reserva (el presupuesto completo del enlace) pasaría lo que le queda al turno.

`registrar` escribe la decisión en `a2a_events` y actualiza la proyección `a2a_conversations` en
**su propia transacción** (un turno que falla no se lleva la auditoría); `liquidar` cierra una
operación aceptada con su gasto real (`usage_ledger` con `kind = 'agent_run'` y `a2a_call_id`).

El payload nunca se guarda: `payload_hash` es un HMAC con una clave derivada de `APP_SECRET_KEY` y
`payload_redacted` solo la forma (claves y tipos, acotados). Lo que devuelve otro agente es dato,
nunca instrucción (invariante 7).

Las operaciones que llaman a la guarda (`encargar`, `consultar`, `pasar_a_agente`) llegan con su HU;
hoy los únicos consumidores son los tests de caos.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text

from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

Modo = Literal["delegate", "handoff", "query"]
Razon = Literal[
    "turn_closed", "cycle", "no_link", "depth", "repeat", "calls", "owner_frozen", "data_class", "budget",
    "no_ledger",
]
_CONTEXTO_CLAVE = b"a2a.payload_hash.v1"
_DEADLINE_SIN_ENLACE_MS = 15_000
_PROFUNDIDAD_MAXIMA = 5
# Forma del payload redactado: profundidad, claves por nivel y largo de clave.
_REDACCION = (3, 20, 40)
_CLAVE_PERSONAL = re.compile(r"\d{6,}|@")
# La intención es un identificador, no texto libre: sin esta forma se niega y no se guarda.
_INTENCION = re.compile(r"^[a-z0-9_.:-]{1,80}$")
_INTENCION_INVALIDA = "invalida"


# ------------------------------------------------------------------ datos
@dataclass(frozen=True)
class Enlace:
    from_agent_id: uuid.UUID
    to_agent_id: uuid.UUID
    modes: frozenset[str]
    intents: frozenset[str]
    max_depth: int
    timeout_ms: int
    budget_usd: Decimal
    budget_tokens: int
    data_classes: frozenset[str]


@dataclass(frozen=True)
class GrafoCongelado:
    """El grafo de la release y qué agente es cada instancia de la conexión."""

    enlaces: Mapping[tuple[uuid.UUID, uuid.UUID], Enlace]
    agente_de: Mapping[uuid.UUID, uuid.UUID]
    version_id: uuid.UUID | None = None

    @classmethod
    def de_release(cls, release: Any) -> GrafoCongelado:
        """Desde una `ReleaseCongelada`: el grafo del manifiesto, nunca las tablas vivas."""
        agente_de = {c.instance_id: c.agent_id for c in release.componentes}
        crudo = release.a2a
        if crudo is None:
            return cls({}, agente_de)
        return cls.desde_manifiesto(crudo, agente_de)

    @classmethod
    def desde_manifiesto(cls, crudo: Mapping[str, Any], agente_de: Mapping[uuid.UUID, uuid.UUID]) -> GrafoCongelado:
        enlaces: dict[tuple[uuid.UUID, uuid.UUID], Enlace] = {}
        for e in crudo.get("links") or []:
            enlace = Enlace(
                from_agent_id=uuid.UUID(str(e["from_agent_id"])),
                to_agent_id=uuid.UUID(str(e["to_agent_id"])),
                modes=frozenset(e.get("modes") or ()),
                intents=frozenset(e.get("intents") or ()),
                max_depth=int(e.get("max_depth", 3)),
                timeout_ms=int(e.get("timeout_ms", _DEADLINE_SIN_ENLACE_MS)),
                budget_usd=Decimal(str(e.get("budget_usd", "0"))),
                budget_tokens=int(e.get("budget_tokens", 0)),
                data_classes=frozenset(e.get("data_classes") or ("public", "internal")),
            )
            enlaces[(enlace.from_agent_id, enlace.to_agent_id)] = enlace
        version = crudo.get("version_id")
        return cls(enlaces, dict(agente_de), uuid.UUID(str(version)) if version else None)

    def sin(self, desde: uuid.UUID, hacia: uuid.UUID) -> GrafoCongelado:
        """El mismo grafo sin el enlace `desde → hacia`: la guarda lo niega con `no_link` (lo usa
        el contrato expuesto, HU-AOS-19)."""
        a, b = self.agente_de.get(desde), self.agente_de.get(hacia)
        enlaces = {par: e for par, e in self.enlaces.items() if par != (a, b)}
        return GrafoCongelado(enlaces, self.agente_de, self.version_id)

    def enlace(self, desde: uuid.UUID, hacia: uuid.UUID) -> Enlace | None:
        a, b = self.agente_de.get(desde), self.agente_de.get(hacia)
        if a is None or b is None:
            return None
        return self.enlaces.get((a, b))


@dataclass(frozen=True)
class Limites:
    """Los del turno, del KernelSpec congelado (`a2a` y `routing.owner`)."""

    max_depth: int = 3
    max_calls: int = 6
    budget_usd: Decimal = Decimal("0.05")
    budget_tokens: int = 40_000
    max_cambios_de_dueno: int = 2

    @classmethod
    def de_release(cls, release: Any) -> Limites:
        a2a = release.limites_a2a()
        return cls(
            max_depth=a2a.maxDepth,
            max_calls=a2a.maxCallsPerTurn,
            budget_usd=Decimal(str(a2a.turnBudgetUsd)),
            budget_tokens=a2a.turnBudgetTokens,
            max_cambios_de_dueno=release.ruteo().owner.maxOwnerChangesPer6Turns,
        )

    @classmethod
    def del_kernel(cls, contenido: Mapping[str, Any] | None) -> Limites:
        """Desde el KernelSpec congelado (el contenido del Coordinador en la ranura del turno). Un
        bloque que no valida da los valores por defecto (el build ya lo valida)."""
        from agentcore_contracts.kernelspec import A2ALimits, OwnerStability
        from pydantic import ValidationError

        contenido = contenido or {}
        crudo_a2a = contenido.get("a2a")
        ruteo = contenido.get("routing")
        crudo_dueno = ruteo.get("owner") if isinstance(ruteo, Mapping) else None
        try:
            a2a = A2ALimits.model_validate(crudo_a2a if isinstance(crudo_a2a, Mapping) else {})
            dueno = OwnerStability.model_validate(crudo_dueno if isinstance(crudo_dueno, Mapping) else {})
        except ValidationError:
            log.error("a2a_limites_invalidos")
            a2a, dueno = A2ALimits(), OwnerStability()
        return cls(
            max_depth=a2a.maxDepth,
            max_calls=a2a.maxCallsPerTurn,
            budget_usd=Decimal(str(a2a.turnBudgetUsd)),
            budget_tokens=a2a.turnBudgetTokens,
            max_cambios_de_dueno=dueno.maxOwnerChangesPer6Turns,
        )


@dataclass(frozen=True)
class Operacion:
    from_instance_id: uuid.UUID
    to_instance_id: uuid.UUID
    modo: Modo
    intencion: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    data_classes: tuple[str, ...] = ("internal",)


@dataclass(frozen=True)
class Abierta:
    """Una operación aceptada que espera `liquidar`, con su reserva."""

    enlace: Enlace
    desde: uuid.UUID
    hacia: uuid.UUID
    modo: str
    depth: int


@dataclass
class EstadoDelTurno:
    """Lo que la guarda recuerda dentro de un turno. Vive en memoria del turno."""

    tenant_id: uuid.UUID
    connection_id: uuid.UUID
    conversation_id: uuid.UUID | None
    turn_id: uuid.UUID | None
    release_id: uuid.UUID | None = None
    # Cambios de dueño que leyó el router en este turno (`cambios_de_dueno`); None = no se pudo leer.
    cambios_de_dueno: int | None = 0
    pila: list[uuid.UUID] = field(default_factory=list)
    llamadas: int = 0
    repetidos: set[tuple[Any, ...]] = field(default_factory=set)
    reservado_usd: Decimal = Decimal(0)
    reservado_tokens: int = 0
    gastado_usd: Decimal = Decimal(0)
    gastado_tokens: int = 0
    cerrado: str | None = None
    a2a_conversation_id: uuid.UUID | None = None
    abiertas: dict[uuid.UUID, Abierta] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    permitido: bool
    razon: Razon | None
    call_id: uuid.UUID
    depth: int
    payload_hash: str
    enlace: Enlace | None = None
    pila: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class Gasto:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int | None = None

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ------------------------------------------------------------------ payload
def _clave(tenant_id: uuid.UUID) -> bytes:
    """Derivada por negocio: el mismo payload da hashes distintos en dos negocios."""
    from core.config import get_settings

    contexto = _CONTEXTO_CLAVE + tenant_id.bytes
    return hmac.new(get_settings().app_secret_key.encode(), contexto, hashlib.sha256).digest()


def payload_hash(tenant_id: uuid.UUID, payload: Mapping[str, Any]) -> str:
    """HMAC-SHA256 (64 hex) del payload canónico. Rotar `APP_SECRET_KEY` cambia los hashes."""
    canonico = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hmac.new(_clave(tenant_id), canonico.encode(), hashlib.sha256).hexdigest()


def intencion_registrable(intencion: str) -> str:
    return intencion if _INTENCION.match(intencion) else _INTENCION_INVALIDA


def redactar(valor: Any, nivel: int = 0) -> Any:
    """Solo la forma: claves y tipos, nunca valores. Acotada en profundidad, claves y largo; las
    claves que parecen datos personales (dígitos largos, `@`) se ocultan."""
    profundidad, por_nivel, largo = _REDACCION
    if isinstance(valor, Mapping):
        if nivel >= profundidad:
            return "object"
        salida: dict[str, Any] = {}
        ocultas = 0
        for i, (k, v) in enumerate(sorted(valor.items(), key=lambda kv: str(kv[0]))):
            if i >= por_nivel:
                salida["…"] = f"+{len(valor) - por_nivel}"
                break
            clave = str(k)
            if _CLAVE_PERSONAL.search(clave):
                ocultas += 1
                clave = f"«clave{ocultas}»"
            salida[clave[:largo]] = redactar(v, nivel + 1)
        return salida
    if isinstance(valor, list | tuple):
        return [redactar(valor[0], nivel + 1)] if valor and nivel < profundidad else "array"
    if isinstance(valor, bool):
        return "boolean"
    if isinstance(valor, int | float | Decimal):
        return "number"
    if valor is None:
        return "null"
    return "string"


# ------------------------------------------------------------------ guarda
def evaluar(estado: EstadoDelTurno, op: Operacion, grafo: GrafoCongelado, limites: Limites) -> Decision:
    """La decisión sobre una operación, sin efectos: no toca la base ni el estado."""
    huella = payload_hash(estado.tenant_id, op.payload)
    pila = list(estado.pila)
    if not pila:
        pila = [op.from_instance_id]
    elif op.from_instance_id in pila:
        # Las llamadas de más arriba ya volvieron: la pila sigue desde quien pide.
        pila = pila[: pila.index(op.from_instance_id) + 1]
    else:
        pila = [*pila, op.from_instance_id]
    depth = len(pila)
    call_id = uuid.uuid4()

    def no(razon: Razon, enlace: Enlace | None = None) -> Decision:
        return Decision(False, razon, call_id, min(depth, _PROFUNDIDAD_MAXIMA), huella, enlace, (*pila, op.to_instance_id))

    if estado.cerrado is not None:
        return no("turn_closed")
    if op.to_instance_id in pila:
        return no("cycle")
    enlace = grafo.enlace(op.from_instance_id, op.to_instance_id)
    if not _INTENCION.match(op.intencion):
        return no("no_link", enlace)
    if enlace is None or op.modo not in enlace.modes or (enlace.intents and op.intencion not in enlace.intents):
        return no("no_link", enlace)
    if depth > min(enlace.max_depth, limites.max_depth):
        return no("depth", enlace)
    if (op.from_instance_id, op.to_instance_id, op.modo, op.intencion, huella) in estado.repetidos:
        return no("repeat", enlace)
    if estado.llamadas >= limites.max_calls:
        return no("calls", enlace)
    if op.modo == "handoff" and (
        estado.cambios_de_dueno is None or estado.cambios_de_dueno >= limites.max_cambios_de_dueno
    ):
        return no("owner_frozen", enlace)
    if not set(op.data_classes) <= enlace.data_classes:
        return no("data_class", enlace)
    usado_usd = estado.gastado_usd + estado.reservado_usd
    usado_tokens = estado.gastado_tokens + estado.reservado_tokens
    if usado_usd + enlace.budget_usd > limites.budget_usd or usado_tokens + enlace.budget_tokens > limites.budget_tokens:
        return no("budget", enlace)
    return Decision(True, None, call_id, depth, huella, enlace, (*pila, op.to_instance_id))


def aplicar(estado: EstadoDelTurno, op: Operacion, decision: Decision) -> None:
    """Lleva la decisión al estado del turno (en memoria). La llama `registrar`."""
    estado.llamadas += 1
    estado.repetidos.add((op.from_instance_id, op.to_instance_id, op.modo, op.intencion, decision.payload_hash))
    if decision.razon == "cycle":
        estado.cerrado = "loop_blocked"
    elif decision.razon == "budget":
        estado.cerrado = "budget_exhausted"
    if decision.permitido and decision.enlace is not None:
        estado.pila = list(decision.pila)
        estado.reservado_usd += decision.enlace.budget_usd
        estado.reservado_tokens += decision.enlace.budget_tokens
        estado.abiertas[decision.call_id] = Abierta(
            decision.enlace, op.from_instance_id, op.to_instance_id, op.modo, decision.depth
        )


# ------------------------------------------------------------------ ledger
_TIPO_POR_RAZON = {"cycle": "loop_detected", "budget": "budget_exhausted"}


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("a2a: fuera del tenant_scope del negocio")


async def _cabecera_confirmada(session: Any, estado: EstadoDelTurno) -> bool:
    if estado.turn_id is None or estado.conversation_id is None:
        return False
    fila = (
        await session.execute(
            text("SELECT 1 FROM core_decisions WHERE tenant_id = :t AND id = :d AND conversation_id = :c"),
            {"t": estado.tenant_id, "d": estado.turn_id, "c": estado.conversation_id},
        )
    ).first()
    return fila is not None


async def _siguiente_seq(session: Any, estado: EstadoDelTurno) -> int:
    await session.execute(
        text("SELECT 1 FROM a2a_conversations WHERE tenant_id = :t AND id = :a FOR UPDATE"),
        {"t": estado.tenant_id, "a": estado.a2a_conversation_id},
    )
    return (
        await session.execute(
            text("SELECT coalesce(max(seq), 0) + 1 FROM a2a_events WHERE tenant_id = :t AND a2a_conversation_id = :a"),
            {"t": estado.tenant_id, "a": estado.a2a_conversation_id},
        )
    ).scalar_one()


async def _abrir_conversacion(session: Any, estado: EstadoDelTurno, op: Operacion, decision: Decision, limites: Limites) -> None:
    timeout = decision.enlace.timeout_ms if decision.enlace is not None else _DEADLINE_SIN_ENLACE_MS
    nueva = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO a2a_conversations (id, tenant_id, connection_id, conversation_id, turn_id, trigger, "
            "root_mode, initiator_instance_id, max_depth, budget_usd, budget_tokens, deadline_at) VALUES "
            "(:id, :t, :c, :conv, :turn, 'turn', :modo, :ini, :d, :usd, :tok, :fin)"
        ),
        {
            "id": nueva,
            "t": estado.tenant_id,
            "c": estado.connection_id,
            "conv": estado.conversation_id,
            "turn": estado.turn_id,
            "modo": op.modo,
            "ini": decision.pila[0] if decision.pila else op.from_instance_id,
            "d": limites.max_depth,
            "usd": limites.budget_usd,
            "tok": limites.budget_tokens,
            "fin": datetime.now(UTC) + timedelta(milliseconds=timeout),
        },
    )
    estado.a2a_conversation_id = nueva


async def _evento(
    session: Any,
    estado: EstadoDelTurno,
    *,
    tipo: str,
    call_id: uuid.UUID,
    modo: str,
    depth: int,
    desde: uuid.UUID,
    hacia: uuid.UUID | None,
    intencion: str,
    huella: str | None = None,
    redactado: Any = None,
    resultado: Any = None,
    razon: str | None = None,
    gasto: Gasto | None = None,
) -> None:
    seq = await _siguiente_seq(session, estado)
    await session.execute(
        text(
            "INSERT INTO a2a_events (tenant_id, a2a_conversation_id, seq, call_id, type, mode, depth, "
            "from_instance_id, to_instance_id, intent, payload_redacted, payload_hash, result_redacted, reason, "
            "tokens_in, tokens_out, cost_usd, latency_ms) VALUES (:t, :a, :seq, :call, :tipo, :modo, :d, :desde, "
            ":hacia, :intent, cast(:red as jsonb), :hash, cast(:res as jsonb), :razon, :tin, :tout, :usd, :ms)"
        ),
        {
            "t": estado.tenant_id,
            "a": estado.a2a_conversation_id,
            "seq": seq,
            "call": call_id,
            "tipo": tipo,
            "modo": modo,
            "d": max(1, min(depth, _PROFUNDIDAD_MAXIMA)),
            "desde": desde,
            "hacia": hacia,
            "intent": intencion_registrable(intencion),
            "red": json.dumps(redactado) if redactado is not None else None,
            "hash": huella,
            "res": json.dumps(resultado) if resultado is not None else None,
            "razon": razon,
            "tin": gasto.input_tokens if gasto else 0,
            "tout": gasto.output_tokens if gasto else 0,
            "usd": gasto.cost_usd if gasto else 0,
            "ms": gasto.latency_ms if gasto else None,
        },
    )


async def registrar(
    estado: EstadoDelTurno, op: Operacion, grafo: GrafoCongelado, limites: Limites
) -> Decision:
    """Evalúa, aplica al estado y escribe el evento y la proyección en una transacción propia.
    Sin cabecera del turno en `core_decisions` niega sin escribir (`no_ledger` en el log)."""
    _exigir_scope(estado.tenant_id)
    decision = evaluar(estado, op, grafo, limites)
    if op.from_instance_id not in grafo.agente_de:
        # Quien pide no es de la release: ni siquiera se puede registrar (FK compuesta).
        log.warning("a2a_origen_desconocido", connection_id=str(estado.connection_id), reason="no_ledger")
        estado.llamadas += 1
        return Decision(False, "no_ledger", decision.call_id, decision.depth, decision.payload_hash, None, decision.pila)
    async with get_session() as session:
        if not await _cabecera_confirmada(session, estado):
            log.warning("a2a_sin_cabecera", connection_id=str(estado.connection_id), reason="no_ledger")
            estado.llamadas += 1
            return Decision(False, "no_ledger", decision.call_id, decision.depth, decision.payload_hash, None, decision.pila)
        if estado.a2a_conversation_id is None:
            await _abrir_conversacion(session, estado, op, decision, limites)
        tipo = "accepted" if decision.permitido else _TIPO_POR_RAZON.get(decision.razon or "", "denied")
        await _evento(
            session,
            estado,
            tipo=tipo,
            call_id=decision.call_id,
            modo=op.modo,
            depth=decision.depth,
            desde=op.from_instance_id,
            # Un destino que no es de la release no se nombra (la FK compuesta lo rechazaría).
            hacia=op.to_instance_id if op.to_instance_id in grafo.agente_de else None,
            intencion=op.intencion,
            huella=decision.payload_hash,
            redactado=redactar(op.payload),
            razon=decision.razon,
        )
        aplicar(estado, op, decision)
        await session.execute(
            text(
                "UPDATE a2a_conversations SET calls = calls + 1, depth_reached = greatest(depth_reached, :d), "
                "status = coalesce(:estado, status), ended_at = CASE WHEN :estado IS NULL THEN ended_at ELSE now() END "
                "WHERE tenant_id = :t AND id = :a"
            ),
            {
                "t": estado.tenant_id,
                "a": estado.a2a_conversation_id,
                "d": min(decision.depth, _PROFUNDIDAD_MAXIMA),
                "estado": estado.cerrado,
            },
        )
    if decision.razon == "cycle":
        await _avisar_bucle(estado, decision)
    log.info(
        "a2a_decision",
        permitido=decision.permitido,
        reason=decision.razon,
        mode=op.modo,
        depth=decision.depth,
        call_id=str(decision.call_id),
    )
    return decision


async def _avisar_bucle(estado: EstadoDelTurno, decision: Decision) -> None:
    from core.shared.events import ON_A2A_LOOP_DETECTED, event_bus

    await event_bus.emit(
        ON_A2A_LOOP_DETECTED,
        tenant_id=str(estado.tenant_id),
        connection_id=str(estado.connection_id),
        conversation_id=str(estado.conversation_id) if estado.conversation_id else None,
        call_id=str(decision.call_id),
        pila=[str(i) for i in decision.pila],
    )


async def liquidar(
    estado: EstadoDelTurno,
    call_id: uuid.UUID,
    *,
    ok: bool,
    gasto: Gasto | None = None,
    resultado: Mapping[str, Any] | None = None,
) -> None:
    """Cierra una operación aceptada: evento `completed`/`failed` con el gasto real, proyección y,
    si hubo LLM, la fila `agent_run` de `usage_ledger`, todo en una transacción. Libera la reserva."""
    _exigir_scope(estado.tenant_id)
    abierta = estado.abiertas.pop(call_id, None)
    if abierta is None:
        raise KeyError(f"operación A2A no abierta: {call_id}")
    enlace, destino = abierta.enlace, abierta.hacia
    usd = gasto.cost_usd if gasto else Decimal(0)
    tokens = gasto.tokens if gasto else 0
    async with get_session() as session:
        await _evento(
            session,
            estado,
            tipo="completed" if ok else "failed",
            call_id=call_id,
            modo=abierta.modo,
            depth=abierta.depth,
            desde=abierta.desde,
            hacia=destino,
            intencion="resultado",
            resultado=redactar(resultado) if resultado is not None else None,
            razon=None if ok else "error",
            gasto=gasto,
        )
        await session.execute(
            text(
                "UPDATE a2a_conversations SET spent_usd = spent_usd + :usd, spent_tokens = spent_tokens + :tok "
                "WHERE tenant_id = :t AND id = :a"
            ),
            {"t": estado.tenant_id, "a": estado.a2a_conversation_id, "usd": usd, "tok": tokens},
        )
        if gasto is not None and gasto.tokens > 0:
            await session.execute(
                text(
                    "INSERT INTO usage_ledger (id, tenant_id, conversation_id, model, input_tokens, output_tokens, "
                    "latency_ms, cost_usd, turn_id, connection_id, agent_instance_id, release_id, kind, a2a_call_id, "
                    "cache_write_tokens) "
                    # `cache_write_tokens = 0`: significado de la 0114 (HU-AOS-35), sin caché en el gasto A2A.
                    "VALUES (:id, :t, :conv, :m, :tin, :tout, :ms, :usd, :turn, :c, :inst, :rel, 'agent_run', :call, 0)"
                ),
                {
                    "id": uuid.uuid4(),
                    "t": estado.tenant_id,
                    "conv": estado.conversation_id,
                    "m": gasto.model[:100],
                    "tin": gasto.input_tokens,
                    "tout": gasto.output_tokens,
                    "ms": gasto.latency_ms or 0,
                    "usd": usd,
                    "turn": estado.turn_id,
                    "c": estado.connection_id,
                    "inst": destino,
                    "rel": estado.release_id,
                    "call": call_id,
                },
            )
    estado.reservado_usd -= enlace.budget_usd
    estado.reservado_tokens -= enlace.budget_tokens
    estado.gastado_usd += usd
    estado.gastado_tokens += tokens
    if estado.pila and estado.pila[-1] == destino:
        estado.pila.pop()


def liberar(estado: EstadoDelTurno, call_id: uuid.UUID) -> Abierta:
    """Suelta la reserva de una operación aceptada que no gasta LLM (el handoff, HU-AOS-19) sin
    escribir nada: su cierre lo escribe quien la termina (`cerrar_handoff`)."""
    abierta = estado.abiertas.pop(call_id)
    estado.reservado_usd -= abierta.enlace.budget_usd
    estado.reservado_tokens -= abierta.enlace.budget_tokens
    if estado.pila and estado.pila[-1] == abierta.hacia:
        estado.pila.pop()
    return abierta


async def cerrar_handoff(
    session: Any,
    estado: EstadoDelTurno,
    *,
    call_id: uuid.UUID,
    desde: uuid.UUID,
    hacia: uuid.UUID,
    aplicado: bool,
    razon: str | None = None,
) -> None:
    """Fase 2 del handoff (HU-AOS-19) en la transacción de quien llama: `handoff_applied` o
    `cancelled`, y la conversación A2A queda `completed` o `failed`."""
    _exigir_scope(estado.tenant_id)
    if estado.a2a_conversation_id is None:
        return
    await _evento(
        session,
        estado,
        tipo="handoff_applied" if aplicado else "cancelled",
        call_id=call_id,
        modo="handoff",
        depth=1,
        desde=desde,
        hacia=hacia,
        intencion="handoff",
        razon=razon,
    )
    await session.execute(
        text(
            "UPDATE a2a_conversations SET status = CASE WHEN status = 'open' THEN :estado ELSE status END, "
            "ended_at = coalesce(ended_at, now()) WHERE tenant_id = :t AND id = :a"
        ),
        {"t": estado.tenant_id, "a": estado.a2a_conversation_id, "estado": "completed" if aplicado else "failed"},
    )


async def cerrar_turno(estado: EstadoDelTurno) -> None:
    """Al terminar el turno: la conversación A2A que siga abierta queda `completed`."""
    if estado.a2a_conversation_id is None:
        return
    _exigir_scope(estado.tenant_id)
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE a2a_conversations SET status = 'completed', ended_at = now() "
                "WHERE tenant_id = :t AND id = :a AND status = 'open'"
            ),
            {"t": estado.tenant_id, "a": estado.a2a_conversation_id},
        )


__all__ = [
    "Abierta",
    "Decision",
    "Enlace",
    "EstadoDelTurno",
    "Gasto",
    "GrafoCongelado",
    "Limites",
    "Operacion",
    "aplicar",
    "cerrar_handoff",
    "cerrar_turno",
    "evaluar",
    "liberar",
    "liquidar",
    "payload_hash",
    "redactar",
    "registrar",
]
