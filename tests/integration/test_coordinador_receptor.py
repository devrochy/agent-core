"""HU-AOS-88 · la conexión atendida solo por el Coordinador (ADR-0205 §1–§4).

Negocio dado de alta sin especialistas y su primera conexión conversacional. Gherkin:

- «Build de release solo con Coordinador»: la release 1 se construye y se activa con solo el Coordinador.
- «Ruteo al Coordinador»: sin especialistas, el foco del turno es el Coordinador con la fuente `coordinador_receptor`.
- «Sin alta automática»: ni el alta ni la primera conexión crean especialistas.
- «Transición al publicar un especialista»: al publicar el primero, su release se activa sola (exenta del gate) y la
  conversación que atendía el Coordinador pasa a él en su siguiente turno, aunque siga el mismo episodio.
- La exención no se reabre: tras un rollback a la release del Coordinador, publicar de nuevo no queda exento.

Sin LLM ni red.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_editor_agentes_api import (  # noqa: F401  (`_proceso`: fixture del módulo)
    _dueno,
    _proceso,
    _sql,
)
from test_releases_por_conexion import _activa, _asignar, _conversacion, _mensaje, _rollback

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]



@pytest.fixture(scope="module", autouse=True)
def _alta_con_agent_os():
    """El alta de estos tests crea el Coordinador, como en producción (ver `_alta_agent_os`)."""
    import _alta_agent_os

    _alta_agent_os.ACTIVO = True
    yield
    _alta_agent_os.ACTIVO = False


async def _negocio_sin_especialistas() -> tuple[uuid.UUID, uuid.UUID]:
    """Alta con preset (sin especialistas desde ADR-0205 §1) y su primera conexión conversacional, el chat web, con
    la release 1 que deja el outbox."""
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.events.core_handlers import on_connection_release_1
    from core.gateway import chat_web_conexion
    from core.shared.tenant import tenant_scope

    slug = f"aos88-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"AOS88 {slug}", slug=slug, preset="comercio", plan="premium", sembrar_ejemplos=False
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await chat_web_conexion.guardar_widget(session, tid, enabled=False, allowed_origins=[])
            cid = (await chat_web_conexion.conexion_del_negocio(session, tid)).id
        await on_connection_release_1(connection_id=str(cid))
    return tid, cid


async def _roles(tid: uuid.UUID, release_id: Any) -> list[str]:
    filas = await _sql(
        tid,
        "select c ->> 'role' from connection_releases r, jsonb_array_elements(r.manifest -> 'components') c "
        "where r.id = :r order by 1",
        r=release_id,
    )
    return [rol for (rol,) in filas]


async def _publicar(app_client, h: dict[str, str], cid: uuid.UUID, plantilla: str = "agenda") -> dict[str, Any]:
    r = await app_client.post(f"/api/v1/admin/agent-templates/{plantilla}/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    [conexion] = r.json()["connections"]
    assert conexion["error"] is None, conexion
    return {"agent_id": aid, **conexion}


async def _gate_de(tid: uuid.UUID, release_id: Any) -> str | None:
    filas = await _sql(
        tid,
        "select data ->> 'gate' from release_events where release_id = :r and type = 'promoted' "
        "order by created_at desc limit 1",
        r=release_id,
    )
    return filas[0][0] if filas else None


async def test_la_conexion_nace_atendida_por_el_coordinador_y_pasa_al_primer_especialista(app_client) -> None:
    tid, cid = await _negocio_sin_especialistas()
    assert await _sql(tid, "select key from agents where tenant_id = :t and kind = 'specialist'", t=tid) == []
    uno = await _activa(tid, cid)
    assert uno is not None and await _roles(tid, uno) == ["core"]

    # El turno lo atiende el Coordinador (y queda anotado con su fuente).
    conv = await _conversacion(tid, cid)
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    await _mensaje(tid, conv, t0)
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (uno, "primera")
    assert a.foco is not None and a.foco.role == "core"  # la fuente `coordinador_receptor`: test_kernel_router

    # El dueño publica su primer especialista: la release se activa sola, exenta del gate.
    h = await _dueno(app_client, tid)
    publicado = await _publicar(app_client, h, cid)
    assert publicado["release_activated"] is True
    dos = await _activa(tid, cid)
    assert str(dos) == publicado["release_id"] and await _roles(tid, dos) == ["core", "specialist"]
    assert await _gate_de(tid, dos) == "exento_primer_especialista"
    defecto = await _sql(
        tid, "select is_default from agent_instances where agent_id = :a and deleted_at is null",
        a=uuid.UUID(publicado["agent_id"]),
    )
    assert defecto == [(True,)]

    # Mismo episodio: la conversación pasa al especialista en su siguiente turno (la del Coordinador cuenta como
    # retirada).
    await _mensaje(tid, conv, t0 + timedelta(minutes=1), "outbound")
    await _mensaje(tid, conv, t0 + timedelta(minutes=2))
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (dos, "retirada")
    assert a.foco is not None and a.foco.role == "specialist"


async def test_la_exencion_no_se_reabre_tras_un_rollback(app_client) -> None:
    tid, cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    primero = await _publicar(app_client, h, cid)
    assert primero["release_activated"] is True

    # Rollback a la release del Coordinador y otra publicación: ya no es el primer especialista de la conexión.
    await _rollback(tid, cid)
    assert await _roles(tid, await _activa(tid, cid)) == ["core"]
    segundo = await _publicar(app_client, h, cid, plantilla="ventas")
    assert segundo["release_activated"] is False
    assert await _roles(tid, await _activa(tid, cid)) == ["core"]


async def test_la_exencion_es_del_negocio_que_publica(app_client) -> None:
    """Aislamiento: publicar el primer especialista en un negocio no toca la release de otro."""
    tid_a, cid_a = await _negocio_sin_especialistas()
    tid_b, cid_b = await _negocio_sin_especialistas()
    antes_b = await _activa(tid_b, cid_b)

    h_a = await _dueno(app_client, tid_a)
    assert (await _publicar(app_client, h_a, cid_a))["release_activated"] is True
    assert await _activa(tid_b, cid_b) == antes_b
    assert await _roles(tid_b, antes_b) == ["core"]
