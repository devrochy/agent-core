"""HU-AOS-94 · API de tipos de agente, catálogo de habilidades y `/posibles` (ADR-0207 §6).

Sin LLM ni red. Criterios cubiertos:
- **Tipos:** `GET /admin/agent-types` trae el Coordinador y un superconjunto de `agent-templates`.
- **Catálogo:** `GET /admin/skill-catalog` lista habilidades por clase y no expone secretos.
- **Posibles:** `GET /admin/agents/{id}/posibles` da la especialidad, los datos del cliente y las transversales con su
  estado; el Coordinador responde 200 con `core:recepcion`; el agente de otro negocio es 404.
- **Compatibilidad:** instalar una transversal que el tipo no admite o un pack de otra especialidad es 409
  `habilidad_incompatible` y no escribe nada; quitar nunca da 409.
- **Permisos:** las lecturas piden `agents`; crear por tipo pide `settings:write`.
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
BASE = "/api/v1/admin"


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(plugin_manager.scan())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


async def _colaborador(app_client, tid: uuid.UUID, secciones: dict[str, str], rol: str = "owner") -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"{uniq('tip')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
        for seccion, acceso in secciones.items():
            session.add(AdminPermission(admin_id=admin.id, section=seccion, access=acceso, tenant_id=None))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _negocio(app_client, plantilla: str = "ventas") -> tuple[uuid.UUID, dict[str, str], str]:
    from _habilidades import onboard_tenant

    slug = f"tip-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(plan="empresarial", name=f"TIP {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    h = await _colaborador(app_client, tid, {"agents": "write", "settings": "write", "plugins": "write"})
    r = await app_client.post(f"{BASE}/agents/from-script", headers=h, json={"template_key": plantilla, "answers": {}})
    assert r.status_code == 201, r.text
    return tid, h, r.json()["agent"]["id"]


async def test_tipos_son_superconjunto_de_las_plantillas(app_client) -> None:
    _tid, h, _agente = await _negocio(app_client)
    r = await app_client.get(f"{BASE}/agent-types", headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["coordinator"]["key"] == "coordinador" and cuerpo["coordinator"]["skill"] == "core:recepcion"
    viejas = (await app_client.get(f"{BASE}/agent-templates", headers=h, params={"page_size": 100})).json()["items"]
    por_clave = {t["key"]: t for t in cuerpo["types"]}
    assert {p["key"] for p in viejas} <= set(por_clave)
    for vieja in viejas:
        assert {k: por_clave[vieja["key"]][k] for k in vieja} == vieja
    ventas = por_clave["ventas"]
    assert "pack_commerce" in ventas["mandatory_skills"]
    assert set(ventas["transversal_skills"]) == {"pack_knowledge", "pack_payments", "pack_voice"}


async def test_catalogo_de_habilidades_por_clase_y_sin_secretos(app_client) -> None:
    _tid, h, _agente = await _negocio(app_client)
    r = await app_client.get(f"{BASE}/skill-catalog", headers=h)
    assert r.status_code == 200, r.text
    por_id = {s["id"]: s for s in r.json()}
    assert por_id["core:recepcion"]["kind"] == "core"
    assert por_id["pack_commerce"]["kind"] == "especialidad"
    assert por_id["pack_payments"]["kind"] == "transversal"
    assert all("config" not in s for s in por_id.values())


async def test_posibles_del_agente_y_estados(app_client) -> None:
    _tid, h, agente = await _negocio(app_client)
    r = await app_client.get(f"{BASE}/agents/{agente}/posibles", headers=h)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["type_key"] == "ventas" and cuerpo["specialty"]["id"] == "pack_commerce"
    estados = {t["pack_id"]: t["status"] for t in cuerpo["transversal"]}
    assert {"pack_knowledge", "pack_payments"} <= set(estados) <= {"pack_knowledge", "pack_payments", "pack_voice"}
    assert set(estados.values()) == {"sin_agregar"}
    assert "config" not in r.text

    # Pagos sin cuenta no se instala (422); conocimiento sí y queda lista o por configurar, pero ya no «sin agregar».
    r = await app_client.put(f"{BASE}/agents/{agente}/skills/pack_knowledge", headers=h, json={"installed": True})
    assert r.status_code == 200, r.text
    estados = {
        t["pack_id"]: t["status"]
        for t in (await app_client.get(f"{BASE}/agents/{agente}/posibles", headers=h)).json()["transversal"]
    }
    assert estados["pack_knowledge"] in ("lista", "falta_configurar")
    assert estados["pack_payments"] == "sin_agregar"


async def test_coordinador_responde_200_con_su_habilidad(app_client) -> None:
    from core.agents.base import asegurar_coordinador
    from core.shared.tenant import tenant_scope

    tid, h, _agente = await _negocio(app_client)
    async with tenant_scope(tid):
        coordinador = await asegurar_coordinador(tid)
    r = await app_client.get(f"{BASE}/agents/{coordinador}/posibles", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "type_key": "coordinador",
        "specialty": r.json()["specialty"],
        "customer_data": [],
        "transversal": [],
    }
    assert r.json()["specialty"]["id"] == "core:recepcion"


async def test_agente_de_otro_negocio_es_404(app_client) -> None:
    _tid, h, _agente = await _negocio(app_client)
    _otro, _h2, ajeno = await _negocio(app_client)
    assert (await app_client.get(f"{BASE}/agents/{ajeno}/posibles", headers=h)).status_code == 404


async def test_409_habilidad_incompatible_sin_escribir(app_client) -> None:
    tid, h, ventas = await _negocio(app_client, "ventas")
    # Un pack de especialidad ajeno (Agenda en el agente de Ventas) no se instala a mano.
    antes = await _sql(tid, "select count(*) from pack_installations where agent_id = :a", a=uuid.UUID(ventas))
    r = await app_client.put(f"{BASE}/agents/{ventas}/skills/pack_calendar", headers=h, json={"installed": True})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "habilidad_incompatible"
    despues = await _sql(tid, "select count(*) from pack_installations where agent_id = :a", a=uuid.UUID(ventas))
    assert antes == despues
    # Quitar una habilidad que no estaba nunca da 409.
    r = await app_client.put(f"{BASE}/agents/{ventas}/skills/pack_calendar", headers=h, json={"installed": False})
    assert r.status_code != 409, r.text


async def test_permisos_lectura_agents_y_creacion_settings(app_client) -> None:
    tid, _h, _agente = await _negocio(app_client)
    solo_lectura = await _colaborador(app_client, tid, {"agents": "read"}, rol="member")
    for ruta in ("agent-types", "skill-catalog"):
        assert (await app_client.get(f"{BASE}/{ruta}", headers=solo_lectura)).status_code == 200, ruta
    r = await app_client.post(f"{BASE}/agent-types/ventas/agents", headers=solo_lectura, json={})
    assert r.status_code == 403, r.text
    sin_permiso = await _colaborador(app_client, tid, {"settings": "read"}, rol="member")
    assert (await app_client.get(f"{BASE}/agent-types", headers=sin_permiso)).status_code == 403
