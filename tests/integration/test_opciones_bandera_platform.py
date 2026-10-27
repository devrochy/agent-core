"""SPEC-40 (PR 10a) · la ruta de plataforma que enciende las opciones directas de UN negocio, contra Postgres real.

`GET|PUT /api/v1/admin/platform/tenants/{id}/opciones-directas` (solo el master):

- 403 al dueño, a un colaborador y al tester (aunque el rol tester esté habilitado en el entorno); 401 sin sesión.
- 404 a un negocio que no existe o está borrado; el cuerpo solo acepta `habilitada: true|false`.
- Idempotente, audita `opciones_directas_bandera` con el actor y no pisa el resto de `tenants.settings`.
- Por negocio: encender A no enciende B.
- Con la bandera encendida la API de plantillas deja de dar 404 `opciones_directas_no_habilitadas`; apagada, vuelve.
- El catálogo de acciones ofrece las tools marcadas aptas solo con binding explícito (PUT /settings/skills) y pack activo.
- Los caminos de leer-modificar-escribir de `tenants.settings` no reponen un `true` que el master apagó.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
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

RAIZ = Path(__file__).resolve().parents[2]
if "journeys" not in sys.modules:  # el runner de journeys sabe dejar los packs activos y las tools del core registradas
    _SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
    assert _SPEC and _SPEC.loader
    _modulo = importlib.util.module_from_spec(_SPEC)
    sys.modules["journeys"] = _modulo
    _SPEC.loader.exec_module(_modulo)
journeys = sys.modules["journeys"]

HORAS = '{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": "09:00-18:00"}'
PASSWORD = "Opciones-Bandera-2026!"
PLANTILLAS = "/api/v1/plantillas-opciones"
NO_HABILITADO = "opciones_directas_no_habilitadas"


def url(tid: uuid.UUID | str) -> str:
    return f"/api/v1/admin/platform/tenants/{tid}/opciones-directas"


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _settings(tid: uuid.UUID) -> dict[str, Any]:
    ((valor,),) = await _sql(tid, "select settings from tenants where id = :t", t=tid)
    return dict(valor or {})


async def _auditoria(tid: uuid.UUID) -> list[tuple[str, Any]]:
    return [
        (actor, entrada)
        for actor, entrada in await _sql(
            tid, "select actor, input from audit_log where tenant_id = :t and action = 'opciones_directas_bandera' order by created_at", t=tid
        )
    ]


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A (con el pack de agenda) y B (sin él), ambos con la bandera en su valor de fábrica."""
    from core.alta_negocio import onboard_tenant
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    await journeys._preparar_proceso()
    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo, packs in (("bnda", ["pack_calendar"]), ("bndb", [])):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Bandera {slug}", slug=slug, packs=packs, sembrar_ejemplos=False)
        creados.append((tid, slug))
    async with tenant_scope(creados[0][0]):  # el pack de agenda no activa sus tools sin su configuración
        await plugin_manager.save_config(
            "pack_calendar",
            {"source": "interno", "business_hours": HORAS, "timezone": "America/Bogota", "slot_minutes": 60, "buffer_minutes": 0, "event_duration_minutes": 60},
        )
    await plugin_manager.activate_all_configured()
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _usuario(tid: uuid.UUID, *, rol: str) -> str:
    """Un dueño (`owner`) o un colaborador del negocio; o un tester de plataforma (`tester`)."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    email = f"{uniq(rol)}@example.test"
    async with tenant_scope(tid):
        async with get_session() as session:
            if rol == "tester":
                session.add(Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True))
            else:
                admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
                session.add(admin)
                await session.flush()
                session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
    return email


async def test_solo_el_master_cambia_o_lee_la_bandera(app_client, negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    a, _ = negocios
    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)  # el tester puede iniciar sesión y actúa como master en test
    antes = await _settings(a)
    for rol in ("owner", "colaborador", "tester"):
        h = await api_headers(app_client, a, email=await _usuario(a, rol=rol), password=PASSWORD)
        assert (await app_client.get(url(a), headers=h)).status_code == 403, rol
        assert (await app_client.put(url(a), headers=h, json={"habilitada": True})).status_code == 403, rol
    assert (await app_client.get(url(a))).status_code == 401
    assert (await app_client.put(url(a), json={"habilitada": True})).status_code == 401
    assert await _settings(a) == antes and await _auditoria(a) == []


async def test_negocio_inexistente_o_borrado_es_404_sin_filtrar_nada(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    from core.db.session import get_session

    a, _ = negocios
    master = await api_headers(app_client, a)
    for tid in (uuid.uuid4(), uuid.UUID(int=0)):
        for r in (await app_client.get(url(tid), headers=master), await app_client.put(url(tid), headers=master, json={"habilitada": True})):
            assert r.status_code == 404 and r.json()["error"]["code"] == "not_found", r.text
            assert str(tid) not in r.text
    # Borrado (borrado lógico): también 404, y la bandera no se escribe.
    async with get_session() as session:
        await session.execute(text("update tenants set deleted_at = now() where id = :t"), {"t": a})
    try:
        for r in (await app_client.get(url(a), headers=master), await app_client.put(url(a), headers=master, json={"habilitada": True})):
            assert r.status_code == 404, r.text
        ((valor,),) = await _sql(None, "select settings->'opciones_directas' from tenants where id = :t", t=a)
        assert valor is None
    finally:
        async with get_session() as session:
            await session.execute(text("update tenants set deleted_at = null where id = :t"), {"t": a})


async def test_el_cuerpo_solo_acepta_habilitada_booleana(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _ = negocios
    master = await api_headers(app_client, a)
    for cuerpo in ({}, {"habilitada": "true"}, {"habilitada": 1}, {"habilitada": None}, {"habilitada": True, "extra": 1}, {"habilitada": True, "agent_os": {}}):
        assert (await app_client.put(url(a), headers=master, json=cuerpo)).status_code == 422, cuerpo
    assert await _auditoria(a) == []
    assert (await _settings(a)).get("opciones_directas") is None


async def test_apagada_de_fabrica_se_enciende_es_idempotente_audita_y_no_pisa_settings(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, _ = negocios
    master = await api_headers(app_client, a)
    await _sql(a, "update tenants set settings = coalesce(settings,'{}'::jsonb) || '{\"alta\": {\"origen\": \"x\"}}' where id = :t", t=a)
    resto = {k: v for k, v in (await _settings(a)).items() if k != "opciones_directas"}

    assert (await app_client.get(url(a), headers=master)).json() == {"habilitada": False}
    for _ in range(2):  # encender dos veces deja el mismo estado
        r = await app_client.put(url(a), headers=master, json={"habilitada": True})
        assert r.status_code == 200 and r.json() == {"habilitada": True}, r.text
    assert (await app_client.get(url(a), headers=master)).json() == {"habilitada": True}
    assert (await _settings(a))["opciones_directas"] == {"habilitada": True}
    assert {k: v for k, v in (await _settings(a)).items() if k != "opciones_directas"} == resto

    r = await app_client.put(url(a), headers=master, json={"habilitada": False})
    assert r.json() == {"habilitada": False}
    assert (await app_client.put(url(a), headers=master, json={"habilitada": False})).json() == {"habilitada": False}
    assert (await _settings(a))["opciones_directas"] == {"habilitada": False}

    auditoria = await _auditoria(a)
    assert [entrada for _, entrada in auditoria] == [{"habilitada": v} for v in (True, True, False, False)]
    assert {actor for actor, _ in auditoria} == {"admin:admin@local"}


async def test_encender_un_negocio_no_enciende_otro_y_la_api_de_plantillas_sigue_a_la_bandera(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b = negocios
    ha = await api_headers(app_client, a)
    hb = await api_headers(app_client, b)
    for h in (ha, hb):
        r = await app_client.get(PLANTILLAS, headers=h)
        assert r.status_code == 404 and r.json()["error"]["code"] == NO_HABILITADO, r.text

    assert (await app_client.put(url(a), headers=ha, json={"habilitada": True})).status_code == 200
    assert (await app_client.get(PLANTILLAS, headers=ha)).status_code == 200  # A: la API responde
    r = await app_client.get(PLANTILLAS, headers=hb)  # B: sigue apagada
    assert r.status_code == 404 and r.json()["error"]["code"] == NO_HABILITADO
    assert (await app_client.get(url(b), headers=hb)).json() == {"habilitada": False}
    assert await _auditoria(b) == []

    assert (await app_client.put(url(a), headers=ha, json={"habilitada": False})).status_code == 200
    r = await app_client.get(PLANTILLAS, headers=ha)  # apagada: vuelve el 404 (el rollback inmediato)
    assert r.status_code == 404 and r.json()["error"]["code"] == NO_HABILITADO


async def test_el_catalogo_ofrece_las_tools_marcadas_solo_con_binding_y_pack(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    a, b = negocios
    ha = await api_headers(app_client, a)
    hb = await api_headers(app_client, b)
    for tid, h in ((a, ha), (b, hb)):
        assert (await app_client.put(url(tid), headers=h, json={"habilitada": True})).status_code == 200
    catalogo = f"{PLANTILLAS}/catalogo-acciones"

    # Sin binding explícito no se ofrece nada (aunque la bandera esté encendida y los packs activos).
    assert (await app_client.get(catalogo, headers=ha)).json() == {"acciones": []}

    # El binding sale de Configuración → Habilidades (PUT /admin/settings/skills).
    def binding(nombre: str, plugin: str, habilitada: bool = True) -> dict[str, Any]:
        return {"name": nombre, "plugin_id": plugin, "enabled": habilitada}

    cuerpo = {
        "tools": [
            binding("list_client_appointments", "pack_calendar"),
            binding("find_next_available", "pack_calendar"),  # segura, pero la gobierna el procedimiento `agendar_cita`
            binding("sub_consultar", "core"),  # nadie la marcó apta
            binding("get_customer_profile", "core"),  # ídem
        ]
    }
    assert (await app_client.put("/api/v1/admin/settings/skills", headers=ha, json=cuerpo)).status_code == 200
    por_nombre = {x["tool"]: x for x in (await app_client.get(catalogo, headers=ha)).json()["acciones"]}
    assert set(por_nombre) == {"list_client_appointments"}
    citas = por_nombre["list_client_appointments"]
    assert citas["sensible"] is False and citas["confirmacion_obligatoria"] is False and citas["descripcion"]
    email = citas["parameters"]["properties"]["attendee_email"]
    assert email["type"] == "string" and email["description"] and not citas["parameters"].get("required")

    # Deshabilitar el binding la saca del catálogo.
    cuerpo["tools"][0] = binding("list_client_appointments", "pack_calendar", habilitada=False)
    await app_client.put("/api/v1/admin/settings/skills", headers=ha, json=cuerpo)
    assert (await app_client.get(catalogo, headers=ha)).json() == {"acciones": []}

    # B no tiene el pack de agenda: la tool no es editable (el binding se ignora) y no se ofrece.
    await app_client.put("/api/v1/admin/settings/skills", headers=hb, json={"tools": [binding("list_client_appointments", "pack_calendar")]})
    assert (await app_client.get(catalogo, headers=hb)).json() == {"acciones": []}

    # Apagada la bandera, ni el catálogo responde.
    await app_client.put(url(a), headers=ha, json={"habilitada": False})
    assert (await app_client.get(catalogo, headers=ha)).status_code == 404
    await app_client.put(url(b), headers=hb, json={"habilitada": False})


async def test_el_resultado_de_list_client_appointments_trae_las_claves_escalares_que_cita_la_respuesta(negocios) -> None:  # type: ignore[no-untyped-def]
    """Una respuesta fija solo cita `{result.k}` escalares de primer nivel: `proximas` lo es (la lista, no)."""
    from pack_calendar.skills import CalendarSkill

    from core.kernel.opciones.argumentos import renderizar_respuesta
    from core.mcp.types import ToolContext
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    contexto = ToolContext(conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key=str(uuid.uuid4()), channel="test")
    async with tenant_scope(a):
        resultado = await CalendarSkill().list_appointments({}, contexto)
    assert resultado["proximas"] == 0 and resultado["total"] == 0 and resultado["appointments"] == []
    frase = renderizar_respuesta("Tienes {result.proximas} citas.", {}, resultado, ["proximas"])
    assert frase == "Tienes 0 citas."
    assert renderizar_respuesta("{result.appointments}", {}, resultado, ["appointments"]) is None, "una lista no se cita"


# ---------------------------------------------------------------- el riesgo de `tenants.settings`
async def test_los_caminos_de_leer_modificar_escribir_no_reponen_una_bandera_apagada(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    """Caso real: el master apaga y, a continuación, un camino que reescribe el dict completo (aquí la elección del
    proveedor de suscripciones) NO la repone; y lo inverso (encendida) tampoco se pierde."""
    from core.shared.tenant import tenant_scope
    from core.subscriptions import guardar_proveedor

    a, _ = negocios
    master = await api_headers(app_client, a)
    for valor in (False, True):
        assert (await app_client.put(url(a), headers=master, json={"habilitada": valor})).status_code == 200
        async with tenant_scope(a):
            await guardar_proveedor(a, "wompi")
            await guardar_proveedor(a, None)
        assert (await _settings(a))["opciones_directas"] == {"habilitada": valor}
    await app_client.put(url(a), headers=master, json={"habilitada": False})


async def test_un_camino_que_ya_leyo_no_pisa_el_apagado_que_llega_a_la_vez(negocios) -> None:  # type: ignore[no-untyped-def]
    """El apagado del master espera el candado de la fila que tiene el camino de leer-modificar-escribir y se aplica
    DESPUÉS de su commit: el resultado es apagada y la clave que el camino escribió sigue ahí."""
    from core.db.models import Tenant
    from core.db.session import get_session
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    async with tenant_scope(a):
        await bandera.fijar(a, True, actor="test")
        async with get_session() as session:
            fila = await session.get(Tenant, a, with_for_update=True)  # el mismo patrón de presets y suscripciones
            settings = dict(fila.settings or {})  # lo leído dice «encendida»
            assert settings["opciones_directas"] == {"habilitada": True}
            apagar = asyncio.create_task(_apagar(a))
            await asyncio.sleep(0.5)
            assert not apagar.done(), "el apagado debe esperar el candado de la fila"
            settings["prueba_rmw"] = {"x": 1}
            fila.settings = settings
        await apagar
    final = await _settings(a)
    assert final["opciones_directas"] == {"habilitada": False}, "el RMW no repuso el true"
    assert final["prueba_rmw"] == {"x": 1}, "el apagado no pisó lo que escribió el otro camino"


async def _apagar(tid: uuid.UUID) -> None:
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await bandera.fijar(tid, False, actor="test")
