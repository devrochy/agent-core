"""Métricas de fiabilidad por conexión y versión (HU-AOS-29, adenda de ADR-0117). Solo lectura.

Un solo servicio para el tablero de calidad y, más adelante, para el canary (HU-AOS-32), que compara
estas mismas métricas entre carriles de una conexión. El `Filtro` (negocio, ventana, conexión, versión y
carril) se aplica a todas.

Definiciones (adenda HU-AOS-29):
- **turno**: `core_decisions` con `outcome` `routed` o `escalated` (no los bloqueos por política);
- **escalamiento**: `outcome = 'escalated'` o `turn_outcomes.status = 'escalated'`, por turno y en
  conversaciones distintas;
- **sin resultado**: turnos sin `turn_outcomes` (el ledger es best-effort): no cuentan como éxito;
- **dinero**: solo `usage_ledger` sin `NO_ES_ACTIVIDAD` (turno, `agent_run`, clasificador y redactor);
- **rechazos del verificador**: `verdict = 'fail'` por tipo, menos los falsos positivos marcados;
- **completitud**: tareas cerradas en la ventana (`completed_at`), completadas sobre terminadas.

Bajo la muestra mínima, las tasas salen `None` («sin datos suficientes»). Todo filtra por `tenant_id`
además de RLS y exige el `tenant_scope` del negocio.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text

from core.shared.tenant import get_current_tenant_id

FALSO_POSITIVO = "verifier_false_positive"
MIN_TURNOS = 20
MIN_TAREAS = 10
TERMINALES = ("completed", "cancelled", "failed", "escalated")


@dataclass(frozen=True)
class Filtro:
    desde: datetime
    hasta: datetime
    connection_id: uuid.UUID | None = None
    release_id: uuid.UUID | None = None
    lane: str | None = None


@dataclass
class Operativas:
    turnos: int = 0
    conversaciones: int = 0
    escalados: int = 0
    conversaciones_escaladas: int = 0
    fallidos: int = 0
    sin_resultado: int = 0
    p95_ms: int | None = None
    costo_usd: float = 0.0
    por_agente: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tasa_escalamiento(self) -> float | None:
        return self.escalados / self.turnos if self.turnos >= MIN_TURNOS else None

    @property
    def costo_por_conversacion(self) -> float | None:
        return self.costo_usd / self.conversaciones if self.conversaciones and self.turnos >= MIN_TURNOS else None


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    if get_current_tenant_id() != tenant_id:
        raise PermissionError("métricas: fuera del tenant_scope del negocio")


def senal_falso_positivo(tipo: str) -> str:
    return f"{FALSO_POSITIVO}:{tipo[:60]}"


def _turnos(f: Filtro, alias: str = "d") -> tuple[str, dict[str, Any]]:
    """Condiciones sobre `core_decisions` (negocio, ventana, turnos que cuentan y el filtro)."""
    partes = [
        f"{alias}.tenant_id = :t", f"{alias}.created_at >= :desde", f"{alias}.created_at < :hasta",
        f"{alias}.outcome IN ('routed', 'escalated')",
    ]
    params: dict[str, Any] = {"desde": f.desde, "hasta": f.hasta}
    if f.connection_id is not None:
        partes.append(f"{alias}.connection_id = :c")
        params["c"] = f.connection_id
    if f.release_id is not None:
        partes.append(f"{alias}.release_id = :r")
        params["r"] = f.release_id
    if f.lane is not None:
        partes.append(f"{alias}.release_lane = :lane")
        params["lane"] = f.lane
    return " AND ".join(partes), params


async def _dinero_por_turno(session: Any, tenant_id: uuid.UUID, f: Filtro) -> tuple[float, dict[Any, float]]:
    """Gasto del ledger de los turnos del filtro: total y por instancia de agente."""
    from core.billing.tipos_de_uso import NO_ES_ACTIVIDAD

    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params.update({"t": tenant_id, "fuera": list(NO_ES_ACTIVIDAD)})
    filas = (
        await session.execute(
            text(
                "SELECT u.agent_instance_id, coalesce(sum(u.cost_usd), 0) FROM usage_ledger u "
                "JOIN core_decisions d ON d.tenant_id = u.tenant_id AND d.id = u.turn_id "
                f"WHERE u.tenant_id = :t AND u.kind <> ALL(:fuera) AND {donde} GROUP BY 1"
            ),
            params,
        )
    ).all()
    por = {r[0]: float(r[1] or 0) for r in filas}
    return sum(por.values()), por


async def operativas(session: Any, tenant_id: uuid.UUID, f: Filtro) -> Operativas:
    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params["t"] = tenant_id
    escalado = "(d.outcome = 'escalated' OR o.status = 'escalated')"
    fila = (
        await session.execute(
            text(
                f"SELECT count(*), count(DISTINCT d.conversation_id), count(*) FILTER (WHERE {escalado}), "
                f"  count(DISTINCT d.conversation_id) FILTER (WHERE {escalado}), "
                "  count(*) FILTER (WHERE o.status = 'failed'), count(*) FILTER (WHERE o.turn_id IS NULL), "
                "  percentile_cont(0.95) WITHIN GROUP (ORDER BY o.latency_ms) "
                "FROM core_decisions d LEFT JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                f"WHERE {donde}"
            ),
            params,
        )
    ).one()
    total_usd, por_instancia = await _dinero_por_turno(session, tenant_id, f)
    salida = Operativas(
        turnos=int(fila[0] or 0),
        conversaciones=int(fila[1] or 0),
        escalados=int(fila[2] or 0),
        conversaciones_escaladas=int(fila[3] or 0),
        fallidos=int(fila[4] or 0),
        sin_resultado=int(fila[5] or 0),
        p95_ms=int(fila[6]) if fila[6] is not None and int(fila[0] or 0) >= MIN_TURNOS else None,
        costo_usd=total_usd,
    )
    filas = (
        await session.execute(
            text(
                "SELECT coalesce(o.final_instance_id, d.active_instance_id), nullif(a.name, ''), a.key, count(*), "
                f"  count(*) FILTER (WHERE {escalado}), percentile_cont(0.95) WITHIN GROUP (ORDER BY o.latency_ms) "
                "FROM core_decisions d LEFT JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                "LEFT JOIN agent_instances i ON i.tenant_id = d.tenant_id "
                "  AND i.id = coalesce(o.final_instance_id, d.active_instance_id) "
                "LEFT JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
                f"WHERE {donde} GROUP BY 1, 2, 3 ORDER BY 4 DESC"
            ),
            params,
        )
    ).all()
    salida.por_agente = [
        {
            "instance_id": r[0],
            "nombre": r[1] or r[2] or "Sin agente",
            "turnos": int(r[3]),
            "escalados": int(r[4]),
            "tasa_escalamiento": int(r[4]) / int(r[3]) if int(r[3]) >= MIN_TURNOS else None,
            "p95_ms": int(r[5]) if r[5] is not None and int(r[3]) >= MIN_TURNOS else None,
            "costo_usd": por_instancia.get(r[0], 0.0),
        }
        for r in filas
    ]
    return salida


async def verificador(session: Any, tenant_id: uuid.UUID, f: Filtro) -> list[dict[str, Any]]:
    """Rechazos del verificador por tipo en la ventana y cuántos marcó el dueño como falsos positivos."""
    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params.update({"t": tenant_id, "fp": f"{FALSO_POSITIVO}:"})
    filas = (
        await session.execute(
            text(
                "WITH rechazos AS ("
                "  SELECT DISTINCT d.id AS turn_id, v->>'kind' AS kind "
                "  FROM core_decisions d JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                "  CROSS JOIN LATERAL jsonb_array_elements(coalesce(o.verifier, '[]'::jsonb)) v "
                f"  WHERE {donde} AND v->>'verdict' = 'fail'"
                ") SELECT r.kind, count(*), count(*) FILTER (WHERE EXISTS (SELECT 1 FROM turn_feedback fb "
                "  WHERE fb.tenant_id = :t AND fb.source = 'owner' AND fb.turn_id = r.turn_id AND fb.signal = :fp || r.kind)) "
                "FROM rechazos r GROUP BY r.kind ORDER BY 2 DESC"
            ),
            params,
        )
    ).all()
    return [{"tipo": r[0] or "desconocido", "rechazos": int(r[1]), "falsos_positivos": int(r[2])} for r in filas]


async def rechazos_del_verificador(
    session: Any, tenant_id: uuid.UUID, f: Filtro, *, tipo: str | None = None, limite: int = 50
) -> list[dict[str, Any]]:
    """Los turnos con un rechazo del verificador (para revisar y marcar falsos positivos)."""
    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params.update({"t": tenant_id, "fp": f"{FALSO_POSITIVO}:", "tipo": tipo, "n": max(1, min(limite, 200))})
    filas = (
        await session.execute(
            text(
                "SELECT DISTINCT ON (d.created_at, d.id, v->>'kind') d.id, d.conversation_id, d.connection_id, "
                "  d.created_at, v->>'kind', EXISTS (SELECT 1 FROM turn_feedback fb WHERE fb.tenant_id = :t "
                "  AND fb.source = 'owner' AND fb.turn_id = d.id AND fb.signal = :fp || (v->>'kind')) "
                "FROM core_decisions d JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                "CROSS JOIN LATERAL jsonb_array_elements(coalesce(o.verifier, '[]'::jsonb)) v "
                f"WHERE {donde} AND v->>'verdict' = 'fail' "
                "AND (cast(:tipo AS text) IS NULL OR v->>'kind' = :tipo) "
                "ORDER BY d.created_at DESC, d.id, v->>'kind' LIMIT :n"
            ),
            params,
        )
    ).all()
    return [
        {
            "turn_id": r[0],
            "conversation_id": r[1],
            "connection_id": r[2],
            "creado": r[3],
            "tipo": r[4] or "desconocido",
            "falso_positivo": bool(r[5]),
        }
        for r in filas
    ]


async def marcar_falso_positivo(
    tenant_id: uuid.UUID, turn_id: uuid.UUID, tipo: str, *, actor: str, nota: str | None = None
) -> bool:
    """El dueño marca un rechazo del verificador como falso positivo. Idempotente por `(turno, tipo)`
    (índice único parcial). False si el turno no tiene ese rechazo. No se desmarca (append-only)."""
    from core.db.session import get_session

    _exigir_scope(tenant_id)
    async with get_session() as session:
        fila = (
            await session.execute(
                text(
                    "SELECT d.conversation_id FROM core_decisions d JOIN turn_outcomes o "
                    "ON o.tenant_id = d.tenant_id AND o.turn_id = d.id "
                    "WHERE d.tenant_id = :t AND d.id = :turn AND EXISTS (SELECT 1 FROM "
                    "jsonb_array_elements(coalesce(o.verifier, '[]'::jsonb)) v "
                    "WHERE v->>'verdict' = 'fail' AND v->>'kind' = :tipo)"
                ),
                {"t": tenant_id, "turn": turn_id, "tipo": tipo},
            )
        ).first()
        if fila is None or fila[0] is None:
            return False
        await session.execute(
            text(
                "INSERT INTO turn_feedback (tenant_id, turn_id, conversation_id, source, signal, note, created_by) "
                "VALUES (:t, :turn, :conv, 'owner', :signal, :nota, :actor) ON CONFLICT DO NOTHING"
            ),
            {
                "t": tenant_id, "turn": turn_id, "conv": fila[0], "signal": senal_falso_positivo(tipo),
                "nota": (nota or None) and nota[:500], "actor": actor[:120],
            },
        )
    return True


async def completitud(session: Any, tenant_id: uuid.UUID, f: Filtro) -> list[dict[str, Any]]:
    """Por procedimiento: tareas cerradas en la ventana y cuántas se completaron. Con versión o carril,
    por el turno que las inició."""
    _exigir_scope(tenant_id)
    params: dict[str, Any] = {"t": tenant_id, "desde": f.desde, "hasta": f.hasta, "terminales": list(TERMINALES)}
    union = ""
    extra = ""
    if f.connection_id is not None:
        # Por la conversación: `started_turn_id` puede faltar (tareas abiertas fuera de un turno).
        union += "JOIN conversations cv ON cv.tenant_id = t.tenant_id AND cv.id = t.conversation_id "
        extra += " AND cv.connection_id = :c"
        params["c"] = f.connection_id
    if f.release_id is not None or f.lane is not None:
        union += "JOIN core_decisions d ON d.tenant_id = t.tenant_id AND d.id = t.started_turn_id "
        if f.release_id is not None:
            extra += " AND d.release_id = :r"
            params["r"] = f.release_id
        if f.lane is not None:
            extra += " AND d.release_lane = :lane"
            params["lane"] = f.lane
    filas = (
        await session.execute(
            text(
                "SELECT t.procedure_id, count(*), count(*) FILTER (WHERE t.status = 'completed'), "
                "  count(*) FILTER (WHERE t.status = 'escalated') "
                f"FROM conversation_tasks t {union}"
                "WHERE t.tenant_id = :t AND t.parent_task_id IS NULL AND t.status = ANY(:terminales) "
                f"AND t.completed_at >= :desde AND t.completed_at < :hasta{extra} "
                "GROUP BY 1 ORDER BY 2 DESC"
            ),
            params,
        )
    ).all()
    return [
        {
            "procedure_id": r[0],
            "terminadas": int(r[1]),
            "completadas": int(r[2]),
            "escaladas": int(r[3]),
            "tasa": int(r[2]) / int(r[1]) if int(r[1]) >= MIN_TAREAS else None,
        }
        for r in filas
    ]


async def completitud_total(session: Any, tenant_id: uuid.UUID, f: Filtro) -> tuple[int, int]:
    """`(terminadas, completadas)` de las tareas de primer nivel cerradas **que empezaron en la
    ventana** (por su turno de inicio, con su conexión, versión y carril). El canary (HU-AOS-32) lo
    usa así: una tarea empezada antes no cuenta, y así el carril estable no se infla con embudos que
    ya estaban por terminar."""
    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params.update({"t": tenant_id, "terminales": list(TERMINALES)})
    fila = (
        await session.execute(
            text(
                "SELECT count(*), count(*) FILTER (WHERE t.status = 'completed') FROM conversation_tasks t "
                "JOIN core_decisions d ON d.tenant_id = t.tenant_id AND d.id = t.started_turn_id "
                f"WHERE t.tenant_id = :t AND t.parent_task_id IS NULL AND t.status = ANY(:terminales) AND {donde}"
            ),
            params,
        )
    ).one()
    return int(fila[0] or 0), int(fila[1] or 0)


@dataclass(frozen=True)
class ConversacionReciente:
    conversation_id: uuid.UUID
    ultimo: datetime
    # Un turno `failed`, o `neutral`/`escalated` con un rechazo del verificador (no lo pudo corregir).
    llego_mal: bool
    # Un `action_failed` de una acción gobernada en alguno de sus turnos.
    accion_fallida: bool
    # Ningún turno dejó `turn_outcomes` (ledger best-effort): sale del denominador.
    sin_resultado: bool


async def conversaciones_recientes(
    session: Any, tenant_id: uuid.UUID, f: Filtro, *, limite: int = 20
) -> list[ConversacionReciente]:
    """Las últimas conversaciones del filtro (por su último turno) y lo que les llegó mal (la señal
    dura del canary, HU-AOS-32)."""
    _exigir_scope(tenant_id)
    donde, params = _turnos(f)
    params.update({"t": tenant_id, "n": max(1, min(limite, 1000))})
    filas = (
        await session.execute(
            text(
                "WITH tt AS ("
                "  SELECT d.conversation_id, d.created_at, o.turn_id IS NULL AS sin,"
                "    (o.status = 'failed' OR (o.status IN ('neutral', 'escalated') AND EXISTS ("
                "      SELECT 1 FROM jsonb_array_elements(coalesce(o.verifier, '[]'::jsonb)) v"
                "      WHERE v->>'verdict' = 'fail'))) AS mal,"
                "    EXISTS (SELECT 1 FROM task_events te WHERE te.tenant_id = d.tenant_id AND te.turn_id = d.id"
                "      AND te.type = 'action_failed') AS accion"
                "  FROM core_decisions d LEFT JOIN turn_outcomes o ON o.tenant_id = d.tenant_id AND o.turn_id = d.id"
                f"  WHERE {donde}"
                ") SELECT conversation_id, max(created_at), coalesce(bool_or(mal), false), bool_or(accion), bool_and(sin) "
                "FROM tt GROUP BY conversation_id ORDER BY 2 DESC LIMIT :n"
            ),
            params,
        )
    ).all()
    return [ConversacionReciente(r[0], r[1], bool(r[2]), bool(r[3]), bool(r[4])) for r in filas]


async def consistencia(session: Any, tenant_id: uuid.UUID, connection_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
    """Por conexión conversacional: la consistencia vigente de su versión activa (`veredicto.
    consistencia_vigente`). Sin versión activa o sin mediciones, `sin_datos`."""
    from core.releases.veredicto import UMBRAL, consistencia_vigente

    _exigir_scope(tenant_id)
    filas = (
        await session.execute(
            text(
                "SELECT cc.id, coalesce(nullif(cc.label, ''), cc.channel), cc.active_release_id, r.number "
                "FROM channel_connections cc LEFT JOIN connection_types ct ON ct.id = cc.type_id "
                "LEFT JOIN connection_releases r ON r.tenant_id = cc.tenant_id AND r.id = cc.active_release_id "
                "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND coalesce(ct.produces_conversations, true) "
                "AND cc.type_id IS DISTINCT FROM 'playground' "
                "AND (cast(:c AS uuid) IS NULL OR cc.id = cast(:c AS uuid)) ORDER BY 2"
            ),
            {"t": tenant_id, "c": connection_id},
        )
    ).all()
    salida: list[dict[str, Any]] = []
    for cid, nombre, activa, numero in filas:
        procedimientos = await consistencia_vigente(session, tenant_id, activa) if activa else []
        salida.append(
            {
                "connection_id": cid,
                "conexion": nombre,
                "release_id": activa,
                "version": numero,
                "umbral": UMBRAL,
                "procedimientos": procedimientos,
                "bajo_umbral": [p["procedure_id"] for p in procedimientos if p["pass_k"] is not None and not p["ok"]],
                "sin_datos": not any(p["pass_k"] is not None for p in procedimientos),
            }
        )
    return salida


async def costo(session: Any, tenant_id: uuid.UUID, f: Filtro, *, por: str) -> list[dict[str, Any]]:
    """Gasto del negocio en la ventana desde `usage_ledger` (sin `NO_ES_ACTIVIDAD`), agrupado por
    `connection`, `agent`, `day` o `a2a` (operación entre agentes: quién pide, a quién y modo)."""
    from core.billing.tipos_de_uso import NO_ES_ACTIVIDAD

    _exigir_scope(tenant_id)
    params: dict[str, Any] = {"t": tenant_id, "desde": f.desde, "hasta": f.hasta, "fuera": list(NO_ES_ACTIVIDAD)}
    filtro = "u.tenant_id = :t AND u.created_at >= :desde AND u.created_at < :hasta AND u.kind <> ALL(:fuera)"
    if f.connection_id is not None:
        filtro += " AND u.connection_id = :c"
        params["c"] = f.connection_id
    if f.release_id is not None:
        filtro += " AND u.release_id = :r"
        params["r"] = f.release_id
    if f.lane is not None:
        filtro += (
            " AND EXISTS (SELECT 1 FROM core_decisions d WHERE d.tenant_id = u.tenant_id AND d.id = u.turn_id "
            "AND d.release_lane = :lane)"
        )
        params["lane"] = f.lane
    nombre_agente = "coalesce(nullif(a.name, ''), a.key, 'Sin agente')"
    union_agente = (
        "LEFT JOIN agent_instances i ON i.tenant_id = u.tenant_id AND i.id = u.agent_instance_id "
        "LEFT JOIN agents a ON a.tenant_id = i.tenant_id AND a.id = i.agent_id "
    )
    if por == "agent":
        sql = (
            f"SELECT u.agent_instance_id::text, {nombre_agente}, NULL, count(*), coalesce(sum(u.cost_usd), 0) "
            f"FROM usage_ledger u {union_agente}WHERE {filtro} GROUP BY 1, 2 ORDER BY 5 DESC"
        )
    elif por == "connection":
        sql = (
            "SELECT u.connection_id::text, coalesce(nullif(c.label, ''), c.channel, 'Sin conexión'), NULL, count(*), "
            "coalesce(sum(u.cost_usd), 0) FROM usage_ledger u "
            "LEFT JOIN channel_connections c ON c.tenant_id = u.tenant_id AND c.id = u.connection_id "
            f"WHERE {filtro} GROUP BY 1, 2 ORDER BY 5 DESC"
        )
    elif por == "day":
        sql = (
            "SELECT to_char(date_trunc('day', u.created_at), 'YYYY-MM-DD'), to_char(date_trunc('day', u.created_at), "
            f"'YYYY-MM-DD'), NULL, count(*), coalesce(sum(u.cost_usd), 0) FROM usage_ledger u WHERE {filtro} "
            "GROUP BY 1, 2 ORDER BY 1"
        )
    elif por == "a2a":
        sql = (
            "SELECT concat(e.from_instance_id, '>', e.to_instance_id, ':', e.mode), "
            "  concat(coalesce(nullif(af.name, ''), af.key, 'un agente'), ' → ', coalesce(nullif(at.name, ''), at.key, 'un agente')), "
            "  e.mode, count(*), coalesce(sum(u.cost_usd), 0) FROM usage_ledger u "
            "JOIN LATERAL (SELECT from_instance_id, to_instance_id, mode FROM a2a_events ev "
            "  WHERE ev.tenant_id = u.tenant_id AND ev.call_id = u.a2a_call_id ORDER BY ev.seq LIMIT 1) e ON true "
            "LEFT JOIN agent_instances fi ON fi.tenant_id = u.tenant_id AND fi.id = e.from_instance_id "
            "LEFT JOIN agents af ON af.tenant_id = fi.tenant_id AND af.id = fi.agent_id "
            "LEFT JOIN agent_instances ti ON ti.tenant_id = u.tenant_id AND ti.id = e.to_instance_id "
            "LEFT JOIN agents at ON at.tenant_id = ti.tenant_id AND at.id = ti.agent_id "
            f"WHERE {filtro} AND u.kind = 'agent_run' AND u.a2a_call_id IS NOT NULL GROUP BY 1, 2, 3 ORDER BY 5 DESC"
        )
    else:
        raise ValueError(f"agrupación desconocida: {por}")
    filas = (await session.execute(text(sql), params)).all()
    return [
        {"clave": r[0], "nombre": r[1], "modo": r[2], "filas": int(r[3]), "costo_usd": float(r[4] or 0)} for r in filas
    ]


async def corridas(session: Any, tenant_id: uuid.UUID, *, connection_id: uuid.UUID | None = None, limite: int = 20) -> list[dict[str, Any]]:
    """Las últimas corridas de evaluación (gate, manual o nightly) con su versión. Sin su costo: lo paga
    la plataforma."""
    _exigir_scope(tenant_id)
    filas = (
        await session.execute(
            text(
                "SELECT e.id, e.origen, e.k, e.pass_1, e.pass_k, e.passed, e.total, e.started_at, r.number, r.connection_id "
                "FROM eval_runs e LEFT JOIN connection_releases r ON r.tenant_id = e.tenant_id AND r.id = e.release_id "
                "WHERE e.tenant_id = :t AND (cast(:c AS uuid) IS NULL OR r.connection_id = cast(:c AS uuid)) "
                "ORDER BY e.started_at DESC LIMIT :n"
            ),
            {"t": tenant_id, "c": connection_id, "n": max(1, min(limite, 100))},
        )
    ).all()
    return [
        {
            "id": r[0],
            "origen": r[1] or "manual",
            "k": int(r[2] or 1),
            "pass_1": float(r[3]) if r[3] is not None else None,
            "pass_k_promedio": float(r[4]) if r[4] is not None else None,
            "aprobados": int(r[5] or 0),
            "total": int(r[6] or 0),
            "creada": r[7],
            "version": r[8],
            "connection_id": r[9],
        }
        for r in filas
    ]


__all__ = [
    "FALSO_POSITIVO",
    "MIN_TAREAS",
    "MIN_TURNOS",
    "ConversacionReciente",
    "Filtro",
    "Operativas",
    "completitud",
    "completitud_total",
    "consistencia",
    "conversaciones_recientes",
    "corridas",
    "costo",
    "marcar_falso_positivo",
    "operativas",
    "rechazos_del_verificador",
    "senal_falso_positivo",
    "verificador",
]
