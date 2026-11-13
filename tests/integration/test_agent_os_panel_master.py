"""HU-AOS-15 · el master enciende y apaga el Agent OS de un negocio desde el panel.

`/api/v1/admin/platform/tenants/{id}/agent-os` (GET, PUT) y `.../agent-os/simular` contra el
stack real, sin LLM ni red:

- **Permisos:** solo el master (decisión del dueño, 2026-10-20). Un dueño de negocio y el tester,
  aunque `QA_TESTERS_ENABLED` esté encendida, reciben 403 en las tres rutas, y la función rechaza
  el `admin_id` del tester; el negocio no existente, 404.
- **Valores:** un modo de enrutamiento desconocido → 422; un UPDATE directo de las banderas con el rol de la aplicación lo rechaza el trigger de 0097, y la función
  con admin no se deja llamar bajo el `tenant_scope` de un negocio.
- **Aplicar:** procedimientos y ruteo se encienden; el runtime ya no es bandera (ADR-0191) y un
  `runtime` que mande un panel viejo se ignora. Cada cambio deja su fila `agent_os.banderas` en
  `audit_log` con el actor (quién) y la fecha (cuándo); sin cambios no audita.
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


def _objetivo(procedures: bool = False, routing: str = "off", **extra: Any) -> dict[str, Any]:
    return {"procedures": procedures, "routing": routing, **extra}


async def _banderas(tid: uuid.UUID) -> tuple[bool, str]:
    ((p, ru),) = await _sql(None, "select aos_procedures, aos_routing from tenants where id = :t", t=tid)
    return bool(p), str(ru)


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

    from core.releases.packs import sync_packs
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    assert not (await sync_packs(_modules())).fallo
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = [f"aosm-a-{uuid.uuid4().hex[:6]}", f"aosm-b-{uuid.uuid4().hex[:6]}"]
    a, _ = await _negocio(slugs[0], pagos)
    b, _ = await _negocio(slugs[1], pagos)
    yield a, b
    capacidad.build_provider = original
    for tid, slug in zip((a, b), slugs, strict=True):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _tester() -> tuple[uuid.UUID, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"tester-aosm-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True)
        session.add(admin)
        await session.flush()
        return admin.id, email


async def test_quien_no_es_master_recibe_403(app_client, negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings
    from core.db.session import get_session
    from core.kernel.banderas import BanderasAOS, BanderasRechazadas, fijar_banderas

    a, _b = negocios
    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)  # el tester puede iniciar sesión
    tester_id, tester_email = await _tester()
    dueno = await api_headers(app_client, a, email=await _dueno(a), password=PASSWORD)
    tester = await api_headers(app_client, a, email=tester_email, password=PASSWORD)
    for h in (dueno, tester):
        assert (await app_client.get(_url(a), headers=h)).status_code == 403
        assert (await app_client.post(_url(a, simular=True), headers=h, json=_objetivo())).status_code == 403
        assert (await app_client.put(_url(a), headers=h, json=_objetivo())).status_code == 403
    # La base tampoco acepta al tester como admin.
    with pytest.raises(BanderasRechazadas) as rechazo:
        async with get_session() as session:
            await fijar_banderas(session, a, BanderasAOS(), admin_id=tester_id)
    assert rechazo.value.codigo == "permiso"
    assert await _banderas(a) == (False, "off")
    master = await api_headers(app_client, a)
    assert (await app_client.get(_url(uuid.uuid4()), headers=master)).status_code == 404


async def test_valores_invalidos_y_escrituras_directas_se_rechazan(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _b = negocios
    master = await api_headers(app_client, a)
    assert (await app_client.put(_url(a), headers=master, json=_objetivo(routing="on"))).status_code == 422
    # El rol de la aplicación ya no escribe las banderas directo (trigger de 0097).
    with pytest.raises(DBAPIError, match="aos_fijar_banderas"):
        await _sql(None, "update tenants set aos_procedures = true where id = :t", t=a)
    # Con admin, la función no acepta la llamada bajo el tenant_scope de un negocio.
    from core.db.session import get_session
    from core.kernel.banderas import BanderasAOS, BanderasRechazadas, fijar_banderas
    from core.shared.tenant import tenant_scope

    ((master_id,),) = await _sql(None, "select id from admins where email = 'admin@local'")
    with pytest.raises(BanderasRechazadas) as rechazo:
        async with tenant_scope(a):
            async with get_session() as session:
                await fijar_banderas(session, a, BanderasAOS(), admin_id=master_id)
    assert rechazo.value.codigo == "permiso"
    assert await _banderas(a) == (False, "off")
    assert await _auditoria(a) == []


async def test_simular_aplicar_con_auditoria_y_sin_tocar_al_otro(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b = negocios
    master = await api_headers(app_client, a)
    b_antes = (await _banderas(b), await _contar(b, "agents"), await _contar(b, "connection_releases"))

    assert await _banderas(a) == (False, "off")
    # Repetir el mismo estado: sin cambios, sin auditar. Un `runtime` de un panel viejo se ignora.
    r = await app_client.put(_url(a), headers=master, json=_objetivo(runtime="legacy"))
    assert r.json()["accion"] == "sin_cambios"

    # Procedimientos + ruteo en shadow en un solo cambio: avisos del runbook.
    objetivo = _objetivo(procedures=True, routing="shadow")
    sim = (await app_client.post(_url(a, simular=True), headers=master, json=objetivo)).json()
    assert sim["accion"] == "listo" and sim["pasos"] == ["procedimientos_on", "ruteo_shadow"], sim
    assert any("pass^4" in x for x in sim["avisos"]), sim["avisos"]
    r = await app_client.put(_url(a), headers=master, json=objetivo)
    assert r.status_code == 200 and r.json()["accion"] == "aplicado", r.text
    assert await _banderas(a) == (True, "shadow")

    # Auditoría: quién y qué, una fila por cambio real, en el negocio operado.
    filas = await _auditoria(a)
    assert [f[0] for f in filas] == ["admin:admin@local"]
    assert [(f[2]["procedures"], f[2]["routing"]) for f in filas] == [(True, "shadow")]
    estado = (await app_client.get(_url(a), headers=master)).json()
    assert estado["ultimo_cambio"]["actor"] == "admin:admin@local" and estado["ultimo_cambio"]["fecha"]
    assert (estado["procedures"], estado["routing"]) == (True, "shadow")
    assert "runtime" not in estado and "aos_connection_routing" not in estado

    # El otro negocio, intacto.
    assert (await _banderas(b), await _contar(b, "agents"), await _contar(b, "connection_releases")) == b_antes
    assert await _auditoria(b) == []
