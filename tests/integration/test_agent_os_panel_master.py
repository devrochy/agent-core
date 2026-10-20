"""HU-AOS-15 · el master enciende y apaga el Agent OS de un negocio desde el panel.

`/api/v1/admin/platform/tenants/{id}/agent-os` (GET, PUT) y `.../agent-os/simular` contra el
stack real, sin LLM ni red:

- **Permisos:** un dueño de negocio (no master) recibe 403 en las tres rutas; el negocio no
  existente, 404.
- **Orden de banderas:** procedimientos o enrutamiento sin v3 → 422 `agent_os_orden`; un UPDATE
  directo de las banderas con el rol de la aplicación lo rechaza el trigger de 0097, y la función
  con admin no se deja llamar bajo el `tenant_scope` de un negocio.
- **Simular no escribe:** con backfills pendientes responde `por_preparar` con los pasos, y el
  negocio sigue igual (ni agentes, ni releases, ni banderas).
- **Aplicar:** legacy → v3 corre los backfills del negocio y `migrar-a-v3`; después procedimientos
  y ruteo; y la vuelta a legacy apaga todo. Cada cambio deja su fila `agent_os.banderas` en
  `audit_log` con el actor (quién) y la fecha (cuándo); sin cambios no audita.
- **Bloqueos:** sin `AOS_CONNECTION_ROUTING` → 409 `agent_os_bloqueado` con los bloqueos y nada
  escrito.
- **Aislamiento:** el otro negocio no cambia (banderas, agentes, auditoría).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy.exc import DBAPIError
from test_agentes_versionados import PASSWORD, _dueno
from test_loop_release_v3 import _modules, _negocio, _sql, journeys

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _url(tid: uuid.UUID, simular: bool = False) -> str:
    return f"/api/v1/admin/platform/tenants/{tid}/agent-os" + ("/simular" if simular else "")


def _objetivo(runtime: str = "legacy", procedures: bool = False, routing: str = "off", **extra: Any) -> dict[str, Any]:
    return {"runtime": runtime, "procedures": procedures, "routing": routing, **extra}


async def _banderas(tid: uuid.UUID) -> tuple[str, bool, str]:
    ((r, p, ru),) = await _sql(None, "select aos_runtime, aos_procedures, aos_routing from tenants where id = :t", t=tid)
    return str(r), bool(p), str(ru)


async def _contar(tid: uuid.UUID, tabla: str) -> int:
    ((n,),) = await _sql(tid, f"select count(*) from {tabla} where tenant_id = :t", t=tid)
    return int(n)


async def _auditoria(tid: uuid.UUID) -> list[tuple[str, Any, Any]]:
    return [
        (str(a), i, o)
        for a, i, o in await _sql(
            tid,
            "select actor, input, output from audit_log where tenant_id = :t and action = 'agent_os.banderas' "
            "order by created_at",
            t=tid,
        )
    ]


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A (lo opera el master) y B (testigo), legacy, con una conexión y sin backfills hechos."""
    import pack_payments.payments as capacidad

    from core.config import get_settings
    from core.releases.packs import sync_packs
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    assert not (await sync_packs(_modules())).fallo
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = [f"aosm-a-{uuid.uuid4().hex[:6]}", f"aosm-b-{uuid.uuid4().hex[:6]}"]
    a, _ = await _negocio(slugs[0], pagos)
    b, _ = await _negocio(slugs[1], pagos)
    settings = get_settings()
    antes = settings.aos_connection_routing
    settings.aos_connection_routing = True
    yield a, b
    settings.aos_connection_routing = antes
    capacidad.build_provider = original
    for tid, slug in zip((a, b), slugs, strict=True):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def test_quien_no_es_master_recibe_403(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _b = negocios
    dueno = await api_headers(app_client, a, email=await _dueno(a), password=PASSWORD)
    assert (await app_client.get(_url(a), headers=dueno)).status_code == 403
    assert (await app_client.post(_url(a, simular=True), headers=dueno, json=_objetivo("v3"))).status_code == 403
    assert (await app_client.put(_url(a), headers=dueno, json=_objetivo("v3"))).status_code == 403
    assert await _banderas(a) == ("legacy", False, "off")
    master = await api_headers(app_client, a)
    assert (await app_client.get(_url(uuid.uuid4()), headers=master)).status_code == 404


async def test_combinaciones_fuera_de_orden_se_rechazan(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _b = negocios
    master = await api_headers(app_client, a)
    for objetivo in (_objetivo(procedures=True), _objetivo(routing="shadow"), _objetivo(routing="active")):
        r = await app_client.put(_url(a), headers=master, json=objetivo)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "agent_os_orden"), r.text
        r = await app_client.post(_url(a, simular=True), headers=master, json=objetivo)
        assert r.status_code == 422, r.text
    assert (await app_client.put(_url(a), headers=master, json=_objetivo(routing="on"))).status_code == 422
    # El rol de la aplicación ya no escribe las banderas directo (trigger de 0097).
    with pytest.raises(DBAPIError, match="aos_fijar_banderas"):
        await _sql(None, "update tenants set aos_runtime = 'v3' where id = :t", t=a)
    # Con admin, la función no acepta la llamada bajo el tenant_scope de un negocio.
    from core.db.session import get_session
    from core.kernel.banderas import BanderasAOS, BanderasRechazadas, fijar_banderas
    from core.shared.tenant import tenant_scope

    ((master_id,),) = await _sql(None, "select id from admins where email = 'admin@local'")
    with pytest.raises(BanderasRechazadas) as rechazo:
        async with tenant_scope(a):
            async with get_session() as session:
                await fijar_banderas(session, a, BanderasAOS(runtime="v3"), admin_id=master_id)
    assert rechazo.value.codigo == "permiso"
    assert await _banderas(a) == ("legacy", False, "off")
    assert await _auditoria(a) == []


async def test_sin_la_global_el_cambio_se_bloquea_sin_escribir(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    a, _b = negocios
    master = await api_headers(app_client, a)
    settings = get_settings()
    settings.aos_connection_routing = False
    try:
        r = await app_client.put(_url(a), headers=master, json=_objetivo("v3"))
        estado = (await app_client.get(_url(a), headers=master)).json()
    finally:
        settings.aos_connection_routing = True
    assert (r.status_code, r.json()["error"]["code"]) == (409, "agent_os_bloqueado"), r.text
    assert any("AOS_CONNECTION_ROUTING" in x for x in r.json()["error"]["details"]["bloqueos"])
    assert estado["aos_connection_routing"] is False
    assert await _banderas(a) == ("legacy", False, "off")
    assert await _contar(a, "agents") == 0


async def test_simular_aplicar_y_volver_con_auditoria_y_sin_tocar_al_otro(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b = negocios
    master = await api_headers(app_client, a)
    b_antes = (await _banderas(b), await _contar(b, "agents"), await _contar(b, "connection_releases"))

    # Simular: faltan los backfills; se reportan como pasos y nada se escribe.
    r = await app_client.post(_url(a, simular=True), headers=master, json=_objetivo("v3"))
    assert r.status_code == 200, r.text
    sim = r.json()
    assert sim["accion"] == "por_preparar" and not sim["bloqueos"], sim
    assert sim["pasos"][0] == "runtime_v3" and any("backfill-agentes" in p for p in sim["pasos"]), sim["pasos"]
    assert await _banderas(a) == ("legacy", False, "off")
    assert await _contar(a, "agents") == 0 and await _contar(a, "connection_releases") == 0

    # Aplicar: backfills del negocio + migrar-a-v3 + la bandera, auditado.
    r = await app_client.put(_url(a), headers=master, json=_objetivo("v3"))
    assert r.status_code == 200, r.text
    assert r.json()["accion"] == "aplicado" and r.json()["despues"] == _objetivo("v3"), r.json()
    assert await _banderas(a) == ("v3", False, "off")
    assert await _contar(a, "agents") > 0 and await _contar(a, "connection_releases") > 0
    # Repetir el mismo estado: sin cambios, sin auditar.
    r = await app_client.put(_url(a), headers=master, json=_objetivo("v3"))
    assert r.json()["accion"] == "sin_cambios"

    # Procedimientos + ruteo en shadow en un solo cambio: avisos del runbook.
    objetivo = _objetivo("v3", procedures=True, routing="shadow")
    sim = (await app_client.post(_url(a, simular=True), headers=master, json=objetivo)).json()
    assert sim["accion"] == "listo" and sim["pasos"] == ["procedimientos_on", "ruteo_shadow"], sim
    assert any("pass^4" in x for x in sim["avisos"]), sim["avisos"]
    r = await app_client.put(_url(a), headers=master, json=objetivo)
    assert r.status_code == 200 and r.json()["accion"] == "aplicado", r.text
    assert await _banderas(a) == ("v3", True, "shadow")

    # Volver a legacy apaga todo en un solo cambio.
    r = await app_client.put(_url(a), headers=master, json=_objetivo())
    assert r.status_code == 200 and r.json()["accion"] == "aplicado", r.text
    assert r.json()["pasos"][:3] == ["ruteo_off", "procedimientos_off", "runtime_legacy"]
    assert await _banderas(a) == ("legacy", False, "off")

    # Auditoría: quién y qué, una fila por cambio real, en el negocio operado.
    filas = await _auditoria(a)
    assert [f[0] for f in filas] == ["admin:admin@local"] * 3
    assert [(f[2]["runtime"], f[2]["procedures"], f[2]["routing"]) for f in filas] == [
        ("v3", False, "off"), ("v3", True, "shadow"), ("legacy", False, "off"),
    ]
    estado = (await app_client.get(_url(a), headers=master)).json()
    assert estado["ultimo_cambio"]["actor"] == "admin:admin@local" and estado["ultimo_cambio"]["fecha"]
    assert (estado["runtime"], estado["procedures"], estado["routing"]) == ("legacy", False, "off")

    # El otro negocio, intacto.
    assert (await _banderas(b), await _contar(b, "agents"), await _contar(b, "connection_releases")) == b_antes
    assert await _auditoria(b) == []
