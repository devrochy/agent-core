"""HU-AOS-64 · API de acciones y habilidades del agente (ADR-0190 §8-§9).

Sin LLM ni red. Criterios cubiertos:
- **Crear instala en el agente:** el agente de Ventas creado por el asistente tiene Comercio instalado.
- **Acciones agrupadas:** `GET /agents/{id}/actions` trae las acciones de Comercio con su grupo (`pedidos`, `leads`…).
- **Habilidades transversales:** `GET /agents/{id}/skills` lista Pagos en línea sin instalar; `PUT` con una cuenta
  Wompi del negocio la deja activa y la config vuelve sin secretos; una cuenta que no existe es 422 sin efectos;
  quitarla la desinstala.
- **Aislamiento:** el agente de otro negocio es 404.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"
BASE = "/api/v1/admin/agents"


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _runtime_v3(tid: uuid.UUID) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_runtime = 'v3' where id = :t"), {"t": tid})
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(plugin_manager.scan())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


@pytest.fixture(autouse=True)
def _ruteo(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _dueno(app_client, tid: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('hda')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
        for seccion in ("settings", "plugins"):
            session.add(AdminPermission(admin_id=admin.id, section=seccion, access="write", tenant_id=None))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _negocio_con_ventas(app_client) -> tuple[uuid.UUID, dict[str, str], str]:
    from core.alta_negocio import onboard_tenant

    slug = f"hda-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"HDA {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    await _runtime_v3(tid)
    h = await _dueno(app_client, tid)
    r = await app_client.post(f"{BASE}/from-script", headers=h, json={"template_key": "ventas", "answers": {}})
    assert r.status_code == 201, r.text
    return tid, h, r.json()["agent"]["id"]


async def test_acciones_agrupadas_y_pagos_por_agente(app_client) -> None:
    from pack_payments.cuentas import crear_cuenta

    from core.shared.tenant import tenant_scope

    tid, h, agente = await _negocio_con_ventas(app_client)
    instaladas = await _sql(
        tid, "select pack_id from pack_installations where agent_id = :a and scope = 'agent'", a=uuid.UUID(agente)
    )
    assert [f[0] for f in instaladas] == ["pack_commerce"]

    r = await app_client.get(f"{BASE}/{agente}/actions", headers=h, params={"page_size": 100})
    assert r.status_code == 200, r.text
    grupos = {a["tool"]: a["group"] for a in r.json()["items"]}
    assert grupos["crear_pedido"] == "pedidos" and grupos["crear_lead"] == "leads"
    assert all(a["pack_id"] == "pack_commerce" for a in r.json()["items"])

    r = await app_client.get(f"{BASE}/{agente}/skills", headers=h)
    assert r.status_code == 200, r.text
    pagos = next(s for s in r.json()["items"] if s["pack_id"] == "pack_payments")
    assert pagos == {"pack_id": "pack_payments", "installed": False, "status": None, "config": {}}

    r = await app_client.put(
        f"{BASE}/{agente}/skills/pack_payments", headers=h, json={"installed": True, "config": {"cuenta_id": str(uuid.uuid4())}}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["fields"] == {"cuenta_id": "Valor inválido."}

    async with tenant_scope(tid):
        cuenta = await crear_cuenta(nombre="Principal", public_key="pub_test_x", private_key="prv_test_x", events_secret="e")
    r = await app_client.put(
        f"{BASE}/{agente}/skills/pack_payments", headers=h, json={"installed": True, "config": {"cuenta_id": str(cuenta.id)}}
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"pack_id": "pack_payments", "installed": True, "status": "active", "config": {"cuenta_id": str(cuenta.id)}}
    assert "prv_test_x" not in r.text

    r = await app_client.put(f"{BASE}/{agente}/skills/pack_payments", headers=h, json={"installed": False})
    assert r.status_code == 200 and r.json()["installed"] is False


async def test_agente_de_otro_negocio_es_404(app_client) -> None:
    _tid, h, _agente = await _negocio_con_ventas(app_client)
    _otro, _h2, ajeno = await _negocio_con_ventas(app_client)
    for metodo, ruta, cuerpo in (
        ("get", f"{BASE}/{ajeno}/actions", None),
        ("get", f"{BASE}/{ajeno}/skills", None),
        ("put", f"{BASE}/{ajeno}/skills/pack_payments", {"installed": True}),
    ):
        r = await getattr(app_client, metodo)(ruta, headers=h, **({"json": cuerpo} if cuerpo else {}))
        assert r.status_code == 404, (ruta, r.text)


async def test_cuentas_wompi_por_api(app_client) -> None:
    _tid, h, agente = await _negocio_con_ventas(app_client)
    cuerpo = {"name": "Principal", "public_key": "pub_test_abcdefgh", "private_key": "prv_test_abcdefgh", "events_secret": "evsecreto"}
    r = await app_client.post("/api/v1/pack/payments/accounts", headers=h, json=cuerpo)
    assert r.status_code == 201, r.text
    cuenta = r.json()
    assert "prv_test" not in r.text and "evsecreto" not in r.text
    assert cuenta["events_url"].endswith(f"/webhooks/payments/wompi/{cuenta['events_url'].split('/')[-2]}/{cuenta['id']}")
    assert (await app_client.post("/api/v1/pack/payments/accounts", headers=h, json=cuerpo)).status_code == 409

    r = await app_client.put(
        f"{BASE}/{agente}/skills/pack_payments", headers=h, json={"installed": True, "config": {"cuenta_id": cuenta["id"]}}
    )
    assert r.status_code == 200, r.text
    (listada,) = (await app_client.get("/api/v1/pack/payments/accounts", headers=h)).json()
    assert listada["agent_ids"] == [agente]

    r = await app_client.delete(f"/api/v1/pack/payments/accounts/{cuenta['id']}", headers=h)
    assert r.status_code == 409 and r.json()["error"]["details"]["agent_ids"] == [agente]
    r = await app_client.patch(f"/api/v1/pack/payments/accounts/{cuenta['id']}", headers=h, json={"public_key": "pub_test_rotadaxx"})
    assert r.status_code == 200 and r.json()["public_key"] == "pub_test_rotadaxx"
