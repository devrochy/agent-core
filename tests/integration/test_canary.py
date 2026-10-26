"""HU-AOS-32 parte 1 · canary sticky: iniciar, reparto, 409, detener, publicar y aislamiento.

Sin LLM ni turnos reales: las conversaciones se asignan con `asignar` (como el kernel) y la muestra
se siembra en `core_decisions`. `CANARY_DISPONIBLE` se enciende en el test (la parte 1 lo deja
apagado hasta el job de la parte 2).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno
from test_releases_por_conexion import (
    _activa,
    _asignar,
    _cambiar_nombre,
    _construir,
    _conversacion,
    _mensaje,
    _negocio,
    _promover,
    _release_uno,
    _rollback,
    _sql,
)

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


@pytest.fixture(autouse=True)
def _canary_disponible(monkeypatch: Any) -> None:
    from core.releases import canary

    monkeypatch.setattr(canary, "CANARY_DISPONIBLE", True)


async def _con_candidata() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Negocio v3 con la release 1 activa y una candidata. (tid, conexión, activa, candidata)."""
    tid, _slug, cid = await _negocio()
    await _release_uno(tid)
    await banderas_como_dueno(tid, aos_runtime="v3")
    uno = await _activa(tid, cid)
    assert uno is not None
    await _cambiar_nombre(tid, "Dos")
    dos = (await _construir(tid, cid)).release_id
    return tid, cid, uno, dos


async def _svc(tid: uuid.UUID, fn: str, *args: Any, **kw: Any) -> Any:
    from core.releases import canary
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await getattr(canary, fn)(tid, *args, **kw)


async def _codigo(coro: Any) -> str:
    from core.releases.promocion import ConflictoDeRelease

    with pytest.raises(ConflictoDeRelease) as exc:
        await coro
    return exc.value.codigo


async def test_iniciar_reparte_sticky_y_bloquea_publicar_y_volver_atras() -> None:
    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=50, actor="test")
    ((canary, percent),) = await _sql(tid, "select canary_release_id, canary_percent from channel_connections where id = :c", c=cid)
    assert (canary, percent) == (dos, 50)
    assert await _codigo(_svc(tid, "iniciar", cid, dos, percent=10, actor="test")) == "canary_en_curso"
    # Reparto: primera asignación sorteada; dentro del episodio no cambia.
    t0 = datetime.now(UTC) - timedelta(hours=1)
    destinos: list[tuple[uuid.UUID, str]] = []
    for _ in range(40):
        conv = await _conversacion(tid, cid)
        await _mensaje(tid, conv, t0)
        a = await _asignar(tid, cid, conv)
        await _mensaje(tid, conv, t0 + timedelta(minutes=2))
        b = await _asignar(tid, cid, conv)
        assert b.release_id == a.release_id and b.motivo is None  # sticky
        ((lane,),) = await _sql(tid, "select release_lane from conversations where id = :c", c=conv)
        destinos.append((a.release_id, lane))
    al_canary = [d for d in destinos if d[0] == dos]
    assert 8 <= len(al_canary) <= 32 and all(lane == "canary" for _r, lane in al_canary)
    assert all(lane == "stable" for r, lane in destinos if r == uno)
    # Con canary vivo no se publica otra ni se vuelve atrás.
    from core.releases.promocion import ConflictoDeRelease

    with pytest.raises(ConflictoDeRelease) as exc:
        await _promover(tid, cid, dos)
    assert exc.value.codigo == "canary_en_curso"
    with pytest.raises(ConflictoDeRelease) as exc:
        await _rollback(tid, cid)
    assert exc.value.codigo == "canary_en_curso"


async def test_detener_a_mano_se_puede_reintentar_y_las_conversaciones_vuelven() -> None:
    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=50, actor="test")
    t0 = datetime.now(UTC) - timedelta(hours=1)
    conv = None
    for _ in range(30):
        c = await _conversacion(tid, cid)
        await _mensaje(tid, c, t0)
        if (await _asignar(tid, cid, c)).release_id == dos:
            conv = c
            break
    assert conv is not None
    await _svc(tid, "detener", cid, actor="test")
    ((canary,),) = await _sql(tid, "select canary_release_id from channel_connections where id = :c", c=cid)
    assert canary is None
    # Su siguiente turno vuelve a la activa (retirada a mitad de episodio), en el carril estable.
    await _mensaje(tid, conv, t0 + timedelta(minutes=3))
    a = await _asignar(tid, cid, conv)
    assert (a.release_id, a.motivo) == (uno, "retirada")
    assert await _sql(tid, "select release_lane from conversations where id = :c", c=conv) == [("stable",)]
    # Detenida a mano: se puede volver a probar.
    await _svc(tid, "iniciar", cid, dos, percent=10, actor="test")


async def test_publicar_sin_muestra_espera_y_a_los_siete_dias_publica() -> None:
    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=50, actor="test")
    assert await _codigo(_svc(tid, "publicar", cid, actor="test")) == "canary_sin_muestra"
    estado = await _svc(tid, "estado", cid)
    assert estado.veredicto.estado == "sin_muestra" and not estado.puede_publicar
    # A los 7 días sin muestra se puede publicar a mano.
    luego = datetime.now(UTC) + timedelta(days=8)
    cambio = await _svc(tid, "publicar", cid, actor="test", ahora=luego)
    assert cambio.to_release_id == dos and await _activa(tid, cid) == dos
    ((canary, percent),) = await _sql(tid, "select canary_release_id, canary_percent from channel_connections where id = :c", c=cid)
    assert (canary, percent) == (None, 0)
    ((datos,),) = await _sql(
        tid, "select data from release_events where release_id = :r and type = 'promoted' order by created_at desc limit 1", r=dos
    )
    assert datos["desde_canary"] is True and datos["veredicto"] == "sin_muestra"
    del uno


async def _sembrar_muestra(tid: uuid.UUID, cid: uuid.UUID, rid: uuid.UUID, lane: str, n: int, escaladas: int, desde: datetime) -> None:
    for i in range(n):
        conv = await _conversacion(tid, cid)
        await _sql(
            tid,
            "insert into core_decisions (id, tenant_id, connection_id, conversation_id, release_id, release_lane, outcome, latency_ms, created_at) "
            "values (:id, :t, :c, :conv, :r, :l, :o, 5, :w)",
            id=uuid.uuid4(), t=tid, c=cid, conv=conv, r=rid, l=lane, o="escalated" if i < escaladas else "routed",
            w=desde + timedelta(minutes=i),
        )


async def test_peor_se_guarda_una_vez_y_no_se_publica() -> None:
    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=50, actor="test")
    ((inicio,),) = await _sql(tid, "select created_at from release_events where release_id = :r and type = 'canary_started'", r=dos)
    await _sembrar_muestra(tid, cid, uno, "stable", 210, 10, inicio)
    await _sembrar_muestra(tid, cid, dos, "canary", 210, 40, inicio)
    luego = datetime.now(UTC) + timedelta(days=4)
    assert await _codigo(_svc(tid, "publicar", cid, actor="test", ahora=luego)) == "canary_peor"
    guardados = await _sql(tid, "select data->>'veredicto' from release_events where release_id = :r and type = 'canary_evaluated'", r=dos)
    assert guardados == [("peor",)]
    # Decidido una vez: aunque la muestra mejore, sigue `peor` y no se guarda otro.
    await _sembrar_muestra(tid, cid, dos, "canary", 400, 0, inicio + timedelta(hours=1))
    assert await _codigo(_svc(tid, "publicar", cid, actor="test", ahora=luego)) == "canary_peor"
    assert len(await _sql(tid, "select 1 from release_events where release_id = :r and type = 'canary_evaluated'", r=dos)) == 1


async def test_exencion_detiene_el_canary_y_requiere_v3() -> None:
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    tid, cid, uno, dos = await _con_candidata()
    await _svc(tid, "iniciar", cid, dos, percent=20, actor="test")
    async with tenant_scope(tid):
        await promover(tid, cid, dos, actor="test", exencion="migrar_a_v3")
    eventos = await _sql(
        tid, "select type, data->>'reason' from release_events where release_id = :r and type in ('canary_stopped','promoted') order by created_at", r=dos
    )
    assert eventos == [("canary_stopped", "reemplazado"), ("promoted", "manual")]
    # Sin v3 no se inicia (tampoco para el master).
    tid2, _s, cid2 = await _negocio()
    await _release_uno(tid2)
    await _cambiar_nombre(tid2, "Otra")
    otra = (await _construir(tid2, cid2)).release_id
    assert await _codigo(_svc(tid2, "iniciar", cid2, otra, percent=10, actor="test")) == "requiere_v3"
    del uno


async def test_api_canary_y_aislamiento(app_client) -> None:
    tid, cid, _uno, dos = await _con_candidata()
    h = await api_headers(app_client, tid)
    base = f"/api/v1/admin/connections/{cid}"
    assert (await app_client.get(f"{base}/canary", headers=h)).json() is None
    r = await app_client.post(f"{base}/releases/{dos}/canary", headers=h, json={"percent": 10})
    assert r.status_code == 204, r.text
    assert (await app_client.post(f"{base}/releases/{dos}/canary", headers=h, json={"percent": 80})).status_code == 422
    estado = (await app_client.get(f"{base}/canary", headers=h)).json()
    assert estado["percent"] == 10 and estado["veredicto"] == "sin_muestra" and estado["umbral_conversaciones"] == 200
    assert estado["release_number"] == 2 and estado["base_release_number"] == 1 and estado["costos_visibles"] is True
    assert (await app_client.patch(f"{base}/canary", headers=h, json={"percent": 25})).status_code == 204
    assert (await app_client.get(f"{base}/canary", headers=h)).json()["percent"] == 25  # el vigente, no el inicial
    r = await app_client.post(f"{base}/canary/promote", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "canary_sin_muestra"
    # Otro negocio no ve ni toca el canary de esta conexión.
    otro, _s, _c = await _negocio()
    ho = await api_headers(app_client, otro)
    assert (await app_client.get(f"{base}/canary", headers=ho)).status_code == 404
    assert (await app_client.post(f"{base}/canary/stop", headers=ho)).status_code in (403, 404)
    assert (await app_client.post(f"{base}/canary/stop", headers=h)).status_code == 204
