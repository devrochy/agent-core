"""HU-AOS-16 (parte 1) · instalaciones de habilidades por conexión y su efecto en el build.

Sin LLM ni red. Criterios cubiertos:
- **API** `/api/v1/admin/skills/installations`: instalar solo en una conexión (201; 200 si ya
  estaba), listar, quitar (borrado lógico); 409 si el pack no está habilitado en el negocio, si
  la conexión no es conversacional o es el `core`; 404 para la conexión o la instalación de otro
  negocio; el dueño en `legacy` no escribe (403). No existe el scope `tenant` por la API.
- **Build:** una conexión con instalaciones propias congela SOLO esas (reemplazo entero); el
  scope de negocio sigue siendo la habilitación de siempre (no se escriben filas `tenant`);
  fail-closed con `instalacion_sin_habilitar` y con `procedimiento_sin_resolver` si un agente
  declara un procedimiento cuya habilidad no está en el scope de la conexión.
- **Detalle de versión:** `procedures` resueltos contra lo que publican los packs habilitados.
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
PLAN = "pack_commerce.contratar_plan"
CITA = "pack_calendar.agendar_cita"
BASE = "/api/v1/admin/skills/installations"


def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()


async def _conexion(tid: uuid.UUID, tipo: str = "instagram_meta") -> uuid.UUID:
    cid = uuid.uuid4()
    canal, proveedor = ("instagram", "meta") if tipo == "instagram_meta" else ("calendar", "google")
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, :ch, :p, :ext, :tipo)",
        id=cid, t=tid, ch=canal, p=proveedor, ext=uniq("ext"), tipo=tipo,
    )
    return cid


async def _negocio(packs: list[str]) -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio con `packs` habilitados, una conexión conversacional y su release 1."""
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = uniq("aos16api")
    tid, _ = await onboard_tenant(name=f"AOS16 {slug}", slug=slug, packs=packs, sembrar_ejemplos=False, plan="premium")
    cid = await _conexion(tid)
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    return tid, cid


async def _construir(tid: uuid.UUID, cid: uuid.UUID) -> Any:
    from core.releases.build import construir
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await construir(tid, cid, actor="test", modules=_modules())


async def _skills(tid: uuid.UUID, rid: uuid.UUID) -> list[str]:
    ((manifest,),) = await _sql(tid, "select manifest from connection_releases where id = :r", r=rid)
    return sorted(s["pack_id"] for s in manifest["skills"])


async def _dueno(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return email


async def _agenda(tid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Especialista Agenda publicado con `agendar_cita ^1.0` y un `contratar_plan ^9.0` que no
    resuelve. Devuelve (agente, versión)."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    catalogo = catalogo_de_acciones(_modules())
    async with tenant_scope(tid):
        agente = await versiones.crear_agente(tid, key="agenda", kind="specialist", name="Agenda")
        borrador = await versiones.obtener_borrador(tid, agente.id)
        contenido = dict(borrador.content)
        contenido["procedures"] = [{"id": CITA, "version": "^1.0"}, {"id": PLAN, "version": "^9.0"}]
        contenido["tools"] = [{"tool": "check_availability", "enabled": True}]
        await versiones.guardar_borrador(tid, agente.id, contenido, borrador.lock_version, catalogo=catalogo)
        version, _ = await versiones.publicar(tid, agente.id, catalogo=catalogo)
        return agente.id, version.id


# ---------------------------------------------------------------- API
async def test_api_instalaciones_por_conexion(app_client) -> None:
    tid, cid = await _negocio(["pack_commerce", "pack_calendar"])
    otro, ajena = await _negocio(["pack_commerce"])
    integracion = await _conexion(tid, "google_calendar")
    master = await api_headers(app_client, tid)
    master_otro = await api_headers(app_client, otro)

    assert (await app_client.get(BASE, headers=master)).json() == []  # la `core` de secretos no se lista

    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_calendar", "connection_id": str(cid)})
    assert r.status_code == 201, r.text
    instalada = r.json()
    assert (instalada["scope"], instalada["connection_id"], instalada["status"]) == ("connection", str(cid), "active")
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_calendar", "connection_id": str(cid)})
    assert r.status_code == 200 and r.json()["id"] == instalada["id"]  # idempotente

    # Precondiciones y reglas.
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_cobranza", "connection_id": str(cid)})
    assert r.status_code == 409 and "pack_not_enabled" in r.text
    r = await app_client.post(BASE, headers=master, json={"pack_id": "core", "connection_id": str(cid)})
    assert r.status_code in (404, 409)
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_calendar", "connection_id": str(integracion)})
    assert r.status_code == 409 and "connection_not_conversational" in r.text
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_calendar", "scope": "tenant", "connection_id": str(cid)})
    assert r.status_code == 422  # el scope de negocio no se escribe por aquí
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_inexistente", "connection_id": str(cid)})
    assert r.status_code == 409 and "pack_not_enabled" in r.text  # igual que uno no habilitado

    # Aislamiento: la conexión y la instalación de otro negocio no existen.
    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_commerce", "connection_id": str(ajena)})
    assert r.status_code == 404
    assert (await app_client.get(BASE, headers=master_otro)).json() == []
    assert (await app_client.get(f"{BASE}?connection_id={cid}", headers=master_otro)).json() == []
    assert (await app_client.delete(f"{BASE}/{instalada['id']}", headers=master_otro)).status_code == 404

    # El dueño no escribe mientras el negocio esté en legacy; sí lee.
    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    assert [i["id"] for i in (await app_client.get(BASE, headers=dueno)).json()] == [instalada["id"]]
    assert (await app_client.delete(f"{BASE}/{instalada['id']}", headers=dueno)).status_code == 403
    await _sql(None, "update tenants set aos_runtime = 'v3' where id = :t", t=tid)
    assert (await app_client.delete(f"{BASE}/{instalada['id']}", headers=dueno)).status_code == 204
    assert (await app_client.delete(f"{BASE}/{instalada['id']}", headers=dueno)).status_code == 404
    assert (await app_client.get(BASE, headers=dueno)).json() == []
    # Borrado lógico: la fila sigue con `deleted_at`, y se puede reinstalar.
    ((borrada,),) = await _sql(tid, "select deleted_at is not null from pack_installations where id = :i", i=uuid.UUID(instalada["id"]))
    assert borrada
    r = await app_client.post(BASE, headers=dueno, json={"pack_id": "pack_calendar", "connection_id": str(cid)})
    assert r.status_code == 201 and r.json()["id"] != instalada["id"]
    # Nunca se escriben filas de scope negocio (B1).
    assert await _sql(tid, "select id from pack_installations where scope = 'tenant' and pack_id <> 'core'") == []


# ---------------------------------------------------------------- build
async def test_el_build_congela_solo_las_de_la_conexion_y_falla_cerrado(app_client) -> None:
    from core.agents import instancias
    from core.releases.build import ErrorDelBuild
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio(["pack_commerce", "pack_calendar"])
    await _sql(None, "update tenants set aos_runtime = 'v3' where id = :t", t=tid)
    master = await api_headers(app_client, tid)
    ((uno,),) = await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid)
    assert await _skills(tid, uno) == ["pack_calendar", "pack_commerce"]  # sin instalaciones: las del negocio

    r = await app_client.post(BASE, headers=master, json={"pack_id": "pack_calendar", "connection_id": str(cid)})
    assert r.status_code == 201
    # El General declara `contratar_plan`, que no está en el scope de la conexión: fail-closed.
    with pytest.raises(ErrorDelBuild) as error:
        await _construir(tid, cid)
    assert error.value.codigo == "procedimiento_sin_resolver"
    assert any(PLAN in d for d in error.value.detalle)

    # Con un especialista de Agenda por defecto y sin el General, la release congela solo calendar.
    agente, version = await _agenda(tid)
    async with tenant_scope(tid):
        await instancias.agregar_instancia(tid, cid, agente, is_default=True)
    with pytest.raises(ErrorDelBuild):  # `^9.0` de contratar_plan no lo provee nadie
        await _construir(tid, cid)
    r = await app_client.get(f"/api/v1/admin/agents/{agente}/versions/{version}", headers=master)
    assert r.status_code == 200, r.text
    resueltos = {p["id"]: p for p in r.json()["procedures"]}
    assert resueltos[CITA]["resolved_version"] == "1.0.0" and resueltos[CITA]["problem"] is None
    assert resueltos[PLAN]["resolved_version"] is None and resueltos[PLAN]["problem"]

    # Versión 2 de Agenda solo con `agendar_cita`.
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones

    catalogo = catalogo_de_acciones(_modules())
    async with tenant_scope(tid):
        borrador = await versiones.obtener_borrador(tid, agente)
        contenido = dict(borrador.content)
        contenido["procedures"] = [{"id": CITA, "version": "^1.0"}]
        await versiones.guardar_borrador(tid, agente, contenido, borrador.lock_version, catalogo=catalogo)
        await versiones.publicar(tid, agente, catalogo=catalogo)
    ((general,),) = await _sql(
        tid,
        "select ai.id from agent_instances ai join agents a on a.id = ai.agent_id "
        "where ai.connection_id = :c and a.key = 'general' and ai.deleted_at is null",
        c=cid,
    )
    async with tenant_scope(tid):
        await instancias.quitar_instancia(tid, cid, general)
    build = await _construir(tid, cid)
    assert await _skills(tid, build.release_id) == ["pack_calendar"]  # reemplazo entero, sin merge
    ((manifest,),) = await _sql(tid, "select manifest from connection_releases where id = :r", r=build.release_id)
    (agenda,) = [c for c in manifest["components"] if c["agent_key"] == "agenda"]
    assert agenda["is_default"] and agenda["procedures"] == [{"id": CITA, "version": "1.0.0"}]

    # Una instalación de conexión de un pack que el negocio no habilita (escrita por fuera de la
    # API) hace fallar el build: la activación del código sigue colgando de `spec.packs`.
    await _sql(
        tid,
        "insert into pack_installations (id, tenant_id, pack_id, scope, connection_id, status) "
        "values (:id, :t, 'pack_cobranza', 'connection', :c, 'active')",
        id=uuid.uuid4(), t=tid, c=cid,
    )
    with pytest.raises(ErrorDelBuild) as error:
        await _construir(tid, cid)
    assert error.value.codigo == "instalacion_sin_habilitar"
