"""Elegir el tipo de negocio habilita sus habilidades (onboarding sin «habilidad habilitada» previa).

Un negocio nuevo en Free no tiene ningún pack (`spec.packs == []`). Antes las 4 plantillas salían
`available=false` y `from-script` daba 409 `template_unavailable`: el dueño no podía elegir el tipo de
negocio. Ahora la plantilla trae `will_enable`, crear el agente habilita las habilidades que pide (en una
sola versión de spec), sin validar `min_plan`, y quien no tiene `plugins:write` recibe 403 sin efectos.
Sin LLM ni red.
"""

from __future__ import annotations

import asyncio
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
TODAS = {"agenda": "pack_calendar", "cobranza": "pack_cobranza", "suscripciones": "pack_subscriptions", "ventas": "pack_commerce"}


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


async def _negocio_sin_packs() -> uuid.UUID:
    """El negocio del self-service en Free: sin packs, sin preset."""
    from core.alta_negocio import onboard_tenant

    slug = f"habil-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"HABIL {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    await _runtime_v3(tid)
    return tid


async def _usuario(app_client, tid: uuid.UUID, *, secciones: dict[str, str], dueno: bool = False) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('habil')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner" if dueno else "member"))
        for seccion, acceso in secciones.items():
            session.add(AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=None))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _packs(tid: uuid.UUID) -> set[str]:
    from core.agents.editor import habilitados_del_negocio
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await habilitados_del_negocio(tid)


async def _version_del_spec(tid: uuid.UUID) -> str:
    filas = await _sql(tid, "select version from agent_specs where tenant_id = :t and is_active", t=tid)
    return filas[0][0]


async def test_free_sin_packs_ve_los_cuatro_tipos_y_crear_habilita_la_habilidad(app_client) -> None:
    tid = await _negocio_sin_packs()
    h = await _usuario(app_client, tid, secciones={"settings": "write", "plugins": "write"}, dueno=True)
    assert await _packs(tid) == set()
    version_antes = await _version_del_spec(tid)

    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    plantillas = {p["key"]: p for p in r.json()["items"]}
    assert set(plantillas) == set(TODAS)
    for key, pack in TODAS.items():
        assert plantillas[key]["available"], key
        assert plantillas[key]["missing_skills"] == []
        assert plantillas[key]["will_enable"] == [pack]
        assert plantillas[key]["superpowers"], key
    # Mirar las plantillas es solo lectura: no habilita nada ni crea versión de spec.
    assert await _packs(tid) == set() and await _version_del_spec(tid) == version_antes

    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", "answers": {}})
    assert r.status_code == 201, r.text
    assert r.json()["enabled_skills"] == ["pack_commerce"]
    assert await _packs(tid) == {"pack_commerce"}
    auditoria = await _sql(
        tid, "select action from audit_log where tenant_id = :t and action = 'skills_enabled_by_template'", t=tid
    )
    assert len(auditoria) == 1

    # Reintentar no duplica nada: key_taken, y el spec no cambia de versión.
    version = await _version_del_spec(tid)
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", "answers": {}})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "key_taken")
    assert await _version_del_spec(tid) == version

    # Ya habilitada, otra plantilla que pide lo mismo no vuelve a habilitar.
    plantillas = {p["key"]: p for p in (await app_client.get("/api/v1/admin/agent-templates", headers=h)).json()["items"]}
    assert plantillas["ventas"]["will_enable"] == []


async def test_sin_plugins_write_no_habilita_ni_crea(app_client) -> None:
    tid = await _negocio_sin_packs()
    h = await _usuario(app_client, tid, secciones={"settings": "write"})
    version = await _version_del_spec(tid)

    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "agenda", "answers": {}})
    assert (r.status_code, r.json()["error"]["code"]) == (403, "skills_forbidden")
    # Las habilidades van en `details`, no en `fields` (el panel pintaría `fields` como error de un paso).
    assert r.json()["error"]["details"] == {"skills": ["pack_calendar"]} and not r.json()["error"].get("fields")
    assert await _packs(tid) == set() and await _version_del_spec(tid) == version
    assert not await _sql(tid, "select 1 from agents where tenant_id = :t and key = 'agenda'", t=tid)


async def test_habilitar_en_un_negocio_no_toca_a_otro(app_client) -> None:
    a, b = await _negocio_sin_packs(), await _negocio_sin_packs()
    h = await _usuario(app_client, a, secciones={"settings": "write", "plugins": "write"}, dueno=True)
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "cobranza", "answers": {}})
    assert r.status_code == 201, r.text
    assert await _packs(a) == {"pack_cobranza"}
    assert await _packs(b) == set()


async def test_dos_plantillas_a_la_vez_dejan_las_dos_habilidades(app_client) -> None:
    tid = await _negocio_sin_packs()
    h = await _usuario(app_client, tid, secciones={"settings": "write", "plugins": "write"}, dueno=True)
    cuerpo = {"answers": {}}
    r1, r2 = await asyncio.gather(
        app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "ventas", **cuerpo}),
        app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "agenda", **cuerpo}),
    )
    # Si una pierde la carrera dos veces seguidas responde 409 `concurrent_edit` (reintentable): nunca queda
    # un agente sin su habilidad.
    for r in (r1, r2):
        assert r.status_code in (201, 409), r.text
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "concurrent_edit"
    creadas = {
        key
        for key, r in (("ventas", r1), ("agenda", r2))
        if r.status_code == 201
    }
    assert {TODAS[k] for k in creadas} <= await _packs(tid)
    assert creadas, "al menos una de las dos tiene que haber entrado"


async def test_el_dueno_del_negocio_puede_habilitar_sin_permisos_explicitos(app_client) -> None:
    """El owner no tiene filas en `admin_permissions`: su acceso a `plugins` sale del rol. Si no fuera
    `write`, el dueño recibiría 403 en su propio onboarding."""
    tid = await _negocio_sin_packs()
    h = await _usuario(app_client, tid, secciones={}, dueno=True)
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "suscripciones", "answers": {}})
    assert r.status_code == 201, r.text
    assert r.json()["enabled_skills"] == ["pack_subscriptions"]
