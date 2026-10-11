"""`turn_id` dentro de las tareas Celery contra el stack real (seguimiento #439 de HU-AOS-10).

Una tarea que el turno encola lleva el turno en los headers del mensaje (se publica de verdad
en el broker Redis, a una cola propia que ningún worker consume). En el worker, lo que esa tarea
escribe lleva el `turn_id` del turno: el caso real es `record_audit` en `_handle_policy_defer`
de `outbound_send`. Lo que escribe para otro negocio queda NULL, y una tarea nueva que encole la
tarea no hereda el turno.

Sin LLM: el turno se abre y se enruta a mano.
"""

from __future__ import annotations

import json
import os
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from _api_helpers import uniq
from conftest import PHONE
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # guarda de negocio del turn_id en lo que escriben las tareas
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _filas(tid: uuid.UUID, sql: str, **params: Any) -> list[dict[str, Any]]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return [dict(r._mapping) for r in (await session.execute(text(sql), params)).all()]


async def _publicar_y_leer(tarea: Any, args: list[str]) -> dict[str, Any]:
    """Publica en el broker real (cola propia, sin consumidor) y devuelve los headers del
    mensaje tal como quedaron en Redis."""
    from core.memory.working import get_redis

    cola = uniq("aos10c-cola")
    tarea.apply_async(args=args, queue=cola)
    r = get_redis()
    try:
        crudo = await r.lpop(cola)
    finally:
        await r.delete(cola)
    assert crudo, "el mensaje no llegó al broker"
    return json.loads(crudo)["headers"]


async def _conversacion(tid: uuid.UUID) -> Any:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _c, _s = await consumer._resolve_conversation(
            {
                "channel": "whatsapp",
                "customer_external_id": uniq("wa-celery"),
                "connection_external_id": PHONE,
            }
        )
    return conversacion


async def test_la_tarea_del_turno_escribe_con_su_turn_id(tenant_setup, monkeypatch) -> None:
    from core.gateway.meta_errors import GraphError
    from core.kernel.decisions import en_turno, enrutar
    from core.observability import celery_trazas
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    t1, t2 = tenant_setup
    conversacion = await _conversacion(t1)
    mensaje = uuid.uuid4()
    capturado: dict[str, Any] = {}

    async with tenant_scope(t1):

        @en_turno
        async def _turno() -> None:
            await enrutar(conversacion)
            capturado["propia"] = await _publicar_y_leer(
                outbound_send.outbound_send_task, [str(mensaje), str(t1)]
            )
            # Encolar para otro negocio dentro del turno (p. ej. cobro de plataforma).
            async with tenant_scope(t2):
                capturado["ajena"] = await _publicar_y_leer(
                    outbound_send.outbound_send_task, [str(uuid.uuid4()), str(t2)]
                )

        await _turno()
        capturado["fuera"] = await _publicar_y_leer(
            outbound_send.outbound_send_task, [str(uuid.uuid4()), str(t1)]
        )

    [d] = await _filas(
        t1, "SELECT id, trace_id FROM core_decisions WHERE conversation_id = :c", c=conversacion.id
    )
    headers = capturado["propia"]
    assert headers[celery_trazas.H_TURNO] == str(d["id"])
    assert headers[celery_trazas.H_NEGOCIO] == str(t1)
    assert headers[celery_trazas.H_TRAZA] == d["trace_id"]
    for clave in ("ajena", "fuera"):
        assert celery_trazas.H_TURNO not in capturado[clave], clave

    # ---- worker: la tarea agota las reprogramaciones por política y lo deja en auditoría.
    async def _agotado(_message_id):
        return outbound_send.MAX_POLICY_DEFERRALS + 1

    monkeypatch.setattr(outbound_send, "_bump_policy_deferrals", _agotado)
    error = GraphError("pausa anti-baneo (test)", category="POLICY")
    tarea = SimpleNamespace(name="core.outbound_send", request=SimpleNamespace(**headers))
    celery_trazas.turno_al_empezar(task_id=headers["id"], task=tarea)
    try:
        await outbound_send._handle_policy_defer(mensaje, t1, error)
        await outbound_send._handle_policy_defer(mensaje, t2, error)  # otro negocio
        # Lo que la tarea encola de nuevo no hereda el turno.
        async with tenant_scope(t1):
            encadenada = await _publicar_y_leer(
                outbound_send.outbound_send_task, [str(uuid.uuid4()), str(t1)]
            )
    finally:
        celery_trazas.turno_al_terminar(task_id=headers["id"])
    assert celery_trazas.H_TURNO not in encadenada

    sql = (
        "SELECT turn_id FROM audit_log WHERE action = 'outbound_bloqueado_por_politica' "
        "AND input->>'message_id' = :m"
    )
    assert await _filas(t1, sql, m=str(mensaje)) == [{"turn_id": d["id"]}]
    assert await _filas(t2, sql, m=str(mensaje)) == [{"turn_id": None}], (
        "la fila del otro negocio se escribe y sin turn_id"
    )
