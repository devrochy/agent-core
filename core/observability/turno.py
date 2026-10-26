"""Depurador del turno (HU-AOS-28, ADR-0169 §4): todo lo que pasó en un turno, en una lectura.

Junta lo que el kernel ya deja por `turn_id` (todas con índice `(tenant_id, turn_id)`):
- `core_decisions` (la decisión; su `id` es el turno) y `turn_outcomes` (resultado, verificador);
- `audit_log` (comandos del hilo, decisiones del motor, `tool:*`, escalación, guardrails);
- `task_events` (cambios de estado de las tareas);
- `a2a_conversations` / `a2a_events` (operaciones entre agentes, ya redactadas al escribir);
- `messages` salientes y `usage_ledger` (costo por tipo de llamado).

`leer` devuelve lo guardado tal cual; `redactar` (pura) aplica la regla de ADR-0169 §2-§3 según los
permisos de quien mira. Corre en el `tenant_scope` del negocio y filtra `tenant_id` explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.shared.redaccion import OCULTO, enmascarar, enmascarar_texto
from core.shared.tenant import get_current_tenant_id
from core.shared.test_mode import TEST_CHANNEL

# Tope de filas por bloque: un turno normal tiene unas pocas; el tope protege la respuesta.
MAX_FILAS = 200


@dataclass(frozen=True)
class Agente:
    instance_id: uuid.UUID
    nombre: str


@dataclass(frozen=True)
class Paso:
    at: datetime
    tipo: str  # comando | accion | motor | escalacion | verificador | guardrail | otro
    actor: str
    nombre: str
    accion: str
    entrada: Any = None
    salida: Any = None
    # Hay entrada o salida, pero quien mira no tiene `observability` (lo marca `redactar`).
    detalle_oculto: bool = False
    # Resultado sin valores (visible sin `observability`): ¿salió bien? y los códigos de error.
    ok: bool | None = None
    codigos: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EventoDeTarea:
    at: datetime
    task_id: uuid.UUID
    procedure_id: str | None
    seq: int
    tipo: str
    actor: str
    data: dict[str, Any]


@dataclass(frozen=True)
class EventoA2A:
    seq: int
    tipo: str
    modo: str | None
    intencion: str | None
    desde: uuid.UUID | None
    hacia: uuid.UUID | None
    payload: Any
    resultado: Any
    motivo: str | None
    costo_usd: float | None


@dataclass(frozen=True)
class OperacionA2A:
    id: uuid.UUID
    estado: str
    modo: str | None
    llamadas: int
    gasto_usd: float | None
    eventos: list[EventoA2A] = field(default_factory=list)


@dataclass(frozen=True)
class Envio:
    id: uuid.UUID
    at: datetime
    estado: str | None
    texto: str | None
    plantilla: str | None
    error: str | None


@dataclass(frozen=True)
class Costo:
    tipo: str
    modelo: str | None
    llamados: int
    tokens_entrada: int
    tokens_salida: int
    costo_usd: float | None


@dataclass(frozen=True)
class Turno:
    turn_id: uuid.UUID
    conversation_id: uuid.UUID | None
    connection_id: uuid.UUID | None
    at: datetime
    latencia_ms: int | None
    release_id: uuid.UUID | None
    release_number: int | None
    carril: str | None
    mensajes_entrantes: list[uuid.UUID]
    resultado: str  # routed | blocked | escalated | ignored (la decisión)
    bloqueado_por: str | None
    origen_ruta: str | None
    regla: str | None
    candidatos: list[dict[str, Any]]
    confianza: float | None
    agente_activo: Agente | None
    agente_final: Agente | None
    pila: Any
    estado_final: str | None  # turn_outcomes.status
    verificador: list[dict[str, Any]]
    reintentos_verificador: int
    pasos: list[Paso]
    estado: list[EventoDeTarea]
    a2a: list[OperacionA2A]
    envio: list[Envio]
    costo: list[Costo]
    puede_ver_detalle: bool = False
    costos_visibles: bool = False


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("depurador: fuera del tenant_scope del negocio")


def tipo_de_paso(actor: str) -> str:
    """Cómo se agrupa una fila de `audit_log` en la línea de tiempo."""
    if actor == "core:comando":
        return "comando"
    if actor in {"core:motor", "core:motor_sombra"}:
        return "motor"
    if actor == "core:escalation":
        return "escalacion"
    if actor == "core:verificador":
        return "verificador"
    if actor == "core:guardrail":
        return "guardrail"
    if not actor.startswith(("core:", "admin:", "system:")):
        return "accion"  # `tool:<nombre>` o el actor de auditoría del agente
    return "otro"


def resultado_sin_valores(salida: Any) -> tuple[bool | None, list[str]]:
    """De la salida guardada de un paso, solo si salió bien y los códigos de error (ADR-0169 §2):
    nunca valores. `{"ok": ...}` de los comandos; `{"error": ...}` de las tools (texto o
    `{code}`); la salida de una acción ejecutada sin `error` cuenta como bien."""
    if not isinstance(salida, dict):
        return None, []
    codigos = [str(c)[:60] for c in salida.get("codigos") or [] if isinstance(c, str)]
    if "ok" in salida and isinstance(salida["ok"], bool):
        return salida["ok"], codigos
    error = salida.get("error")
    if error is not None:
        codigo = error.get("code") if isinstance(error, dict) else None
        return False, [str(codigo)[:60]] if isinstance(codigo, str) else codigos
    return True, codigos


def _nombre(actor: str) -> str:
    return actor.split(":", 1)[1] if ":" in actor else actor


async def _agentes(session: Any, tid: uuid.UUID, ids: set[uuid.UUID]) -> dict[uuid.UUID, Agente]:
    if not ids:
        return {}
    filas = await session.execute(
        text(
            "SELECT i.id, coalesce(a.name, i.role) FROM agent_instances i "
            "LEFT JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
            "WHERE i.tenant_id = :t AND i.id = ANY(:ids)"
        ),
        {"t": tid, "ids": list(ids)},
    )
    return {f[0]: Agente(f[0], f[1] or "Agente") for f in filas.all()}


async def leer(session: Any, tenant_id: uuid.UUID, turn_id: uuid.UUID) -> Turno | None:
    """El turno tal como quedó guardado (sin redactar); `None` si no es de este negocio."""
    _exigir_scope(tenant_id)
    p = {"t": tenant_id, "turno": turn_id, "max": MAX_FILAS, "prueba": TEST_CHANNEL}
    d = (
        await session.execute(
            text(
                "SELECT d.conversation_id, d.connection_id, d.created_at, d.latency_ms, d.release_id, r.number, "
                "d.release_lane, d.inbound_message_ids, d.outcome, d.blocked_by, d.route_source, d.rule_id, "
                "d.candidates, d.confidence, d.active_instance_id, d.shadow_route, "
                "o.status, o.verifier, o.verifier_retries, o.final_instance_id, o.latency_ms "
                "FROM core_decisions d "
                "LEFT JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                "LEFT JOIN connection_releases r ON r.tenant_id = d.tenant_id AND r.id = d.release_id "
                # Las conversaciones del playground no se depuran aquí (ADR-0169 §4): allí las
                # acciones corren de verdad y no son tráfico de clientes.
                "LEFT JOIN conversations c ON c.tenant_id = d.tenant_id AND c.id = d.conversation_id "
                "WHERE d.tenant_id = :t AND d.id = :turno AND (c.channel IS NULL OR c.channel <> :prueba)"
            ),
            p,
        )
    ).first()
    if d is None:
        return None

    pasos = [
        Paso(f[0], tipo_de_paso(f[1]), f[1], _nombre(f[1]), f[2], f[3], f[4], False, *resultado_sin_valores(f[4]))
        for f in (
            await session.execute(
                text(
                    "SELECT created_at, actor, action, input, output FROM audit_log "
                    "WHERE tenant_id = :t AND turn_id = :turno ORDER BY created_at, id LIMIT :max"
                ),
                p,
            )
        ).all()
    ]
    estado = [
        EventoDeTarea(f[0], f[1], f[2], f[3], f[4], f[5], dict(f[6] or {}))
        for f in (
            await session.execute(
                text(
                    "SELECT e.created_at, e.task_id, c.procedure_id, e.seq, e.type, e.actor, e.data FROM task_events e "
                    "LEFT JOIN conversation_tasks c ON c.tenant_id = e.tenant_id AND c.id = e.task_id "
                    "WHERE e.tenant_id = :t AND e.turn_id = :turno ORDER BY e.created_at, e.seq LIMIT :max"
                ),
                p,
            )
        ).all()
    ]
    operaciones = (
        await session.execute(
            text(
                "SELECT id, status, root_mode, calls, spent_usd FROM a2a_conversations "
                "WHERE tenant_id = :t AND turn_id = :turno ORDER BY started_at LIMIT :max"
            ),
            p,
        )
    ).all()
    eventos: dict[uuid.UUID, list[EventoA2A]] = {}
    if operaciones:
        for f in (
            await session.execute(
                text(
                    "SELECT a2a_conversation_id, seq, type, mode, intent, from_instance_id, to_instance_id, "
                    "payload_redacted, result_redacted, reason, cost_usd FROM a2a_events "
                    "WHERE tenant_id = :t AND a2a_conversation_id = ANY(:ops) ORDER BY a2a_conversation_id, seq LIMIT :max"
                ),
                {**p, "ops": [o[0] for o in operaciones]},
            )
        ).all():
            eventos.setdefault(f[0], []).append(
                EventoA2A(f[1], f[2], f[3], f[4], f[5], f[6], f[7], f[8], f[9], float(f[10]) if f[10] is not None else None)
            )
    a2a = [
        OperacionA2A(o[0], o[1], o[2], o[3] or 0, float(o[4]) if o[4] is not None else None, eventos.get(o[0], []))
        for o in operaciones
    ]
    envio = [
        Envio(f[0], f[1], f[2], f[3], f[4], f[5])
        for f in (
            await session.execute(
                text(
                    "SELECT id, created_at, status, content, template_name, error_reason FROM messages "
                    "WHERE tenant_id = :t AND turn_id = :turno AND direction = 'outbound' AND deleted_at IS NULL "
                    "ORDER BY created_at LIMIT :max"
                ),
                p,
            )
        ).all()
    ]
    costo = [
        Costo(f[0], f[1], f[2], f[3] or 0, f[4] or 0, float(f[5]) if f[5] is not None else None)
        for f in (
            await session.execute(
                text(
                    "SELECT kind, model, count(*), sum(input_tokens), sum(output_tokens), sum(cost_usd) FROM usage_ledger "
                    "WHERE tenant_id = :t AND turn_id = :turno GROUP BY kind, model ORDER BY kind, model"
                ),
                p,
            )
        ).all()
    ]
    agentes = await _agentes(session, tenant_id, {i for i in (d[14], d[19]) if i is not None})
    shadow = d[15] if isinstance(d[15], dict) else {}
    return Turno(
        turn_id=turn_id,
        conversation_id=d[0],
        connection_id=d[1],
        at=d[2],
        latencia_ms=d[20] if d[20] is not None else d[3],
        release_id=d[4],
        release_number=d[5],
        carril=d[6],
        mensajes_entrantes=list(d[7] or []),
        resultado=d[8],
        bloqueado_por=d[9],
        origen_ruta=d[10],
        regla=d[11],
        candidatos=list(d[12] or []),
        confianza=float(d[13]) if d[13] is not None else None,
        agente_activo=agentes.get(d[14]) if d[14] else None,
        agente_final=agentes.get(d[19]) if d[19] else None,
        pila=shadow.get("pila"),
        estado_final=d[16],
        verificador=list(d[17] or []),
        reintentos_verificador=d[18] or 0,
        pasos=pasos,
        estado=estado,
        a2a=a2a,
        envio=envio,
        costo=costo,
    )


def _redactar_evento(e: EventoDeTarea, ver_pii: bool) -> EventoDeTarea:
    """Datos de un evento de tarea: el `value` según su clase (pago siempre oculto; PII solo para
    quien ve la conversación) y el resto por clave sensible (son códigos, etapas y nombres)."""
    data = {k: (v if k == "value" else enmascarar(v, k)) for k, v in e.data.items()}
    clase = e.data.get("data_class")
    if "value" in data and (clase == "payment" or (clase != "public" and not ver_pii)):
        data["value"] = enmascarar(data["value"], data_class="payment" if clase == "payment" else "pii")
    return replace(e, data=data)


def redactar(t: Turno, *, ver_detalle: bool, ver_costos: bool, ver_pii: bool = True) -> Turno:
    """Aplica ADR-0169 §2-§3: entradas y salidas de las acciones solo con `observability` y siempre
    enmascaradas; datos de pago siempre ocultos; dinero solo con `costs`."""
    pasos = [
        replace(
            p,
            entrada=enmascarar(p.entrada) if ver_detalle and p.entrada is not None else None,
            salida=enmascarar(p.salida) if ver_detalle and p.salida is not None else None,
            detalle_oculto=not ver_detalle and (p.entrada is not None or p.salida is not None),
        )
        for p in t.pasos
    ]
    envio = [replace(e, texto=e.texto if ver_pii else (enmascarar_texto(e.texto) if e.texto else e.texto)) for e in t.envio]
    a2a = [
        replace(
            o,
            gasto_usd=o.gasto_usd if ver_costos else None,
            eventos=[replace(ev, costo_usd=ev.costo_usd if ver_costos else None) for ev in o.eventos],
        )
        for o in t.a2a
    ]
    costo = [replace(c, costo_usd=c.costo_usd if ver_costos else None) for c in t.costo]
    return replace(
        t,
        pasos=pasos,
        estado=[_redactar_evento(e, ver_pii) for e in t.estado],
        envio=envio,
        a2a=a2a,
        costo=costo,
        puede_ver_detalle=ver_detalle,
        costos_visibles=ver_costos,
    )


__all__ = ["MAX_FILAS", "OCULTO", "Turno", "leer", "redactar", "tipo_de_paso"]
