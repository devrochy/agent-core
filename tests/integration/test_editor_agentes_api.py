"""HU-AOS-24 · editor de agentes desde plantillas por `/api/v1` (adenda de ADR-0139, 2026-10-18).

Negocio v3 con `pack_commerce` y `pack_calendar`, una conexión conversacional con su release 1 y
`AOS_CONNECTION_ROUTING` encendida. Gherkin:

- «Desde plantilla»: con Comercio habilitado, la plantilla Ventas trae sus superpoderes y las
  frases sugeridas; el borrador nace derivado y sin publicar.
- «Validación»: un superpoder sin las frases mínimas no se publica; el error viene por campo
  (`procedures[i].overrides.examples`), no como un JSON del contrato.
- D-35: sin conexión no se publica; al publicar se instala el agente y se construye la release
  candidata, sin activarla; reintentar no duplica nada.

Más: plantilla sin habilidades, key ocupada, modelo de otro proveedor, catálogo de modelos con el
costo estimado desde el consumo real, colaborador sin permiso (403) y aislamiento entre negocios.
Sin LLM ni red.
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

PACKS = ["pack_commerce", "pack_calendar"]
PASSWORD = "clave-segura-123"


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

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


@pytest.fixture(autouse=True)
def _ruteo(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)


async def _negocio() -> tuple[uuid.UUID, uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = f"aos24-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS24 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    reporte = await backfill_releases(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte.errores
    await _runtime_v3(tid)
    return tid, cid


async def _dueno(app_client, tid: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno24')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


def _pocas_frases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Superpoderes cuya habilidad trae una sola frase: el dueño tiene que agregar las suyas."""
    from dataclasses import replace

    from core.agents import editor

    original = editor.superpoderes

    def _una(habilitados: Any) -> list[Any]:
        return [replace(s, phrases=s.phrases[:1]) for s in original(habilitados)]

    monkeypatch.setattr(editor, "superpoderes", _una)


async def test_desde_plantilla_validacion_y_publicacion_d35(app_client, monkeypatch: pytest.MonkeyPatch) -> None:
    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)
    activa_antes = (await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid))[0][0]

    # Plantillas: Ventas disponible con sus superpoderes y frases; Cobranza no (falta la habilidad).
    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    plantillas = {p["key"]: p for p in r.json()["items"]}
    assert set(plantillas) == {"agenda", "cobranza", "suscripciones", "ventas"}
    ventas = plantillas["ventas"]
    assert ventas["available"] and ventas["missing_skills"] == []
    assert [s["id"] for s in ventas["superpowers"]] == ["pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"]
    assert all(len(s["phrases"]) >= 3 for s in ventas["superpowers"])
    assert not plantillas["cobranza"]["available"] and plantillas["cobranza"]["missing_skills"] == ["pack_cobranza"]

    r = await app_client.post("/api/v1/admin/agent-templates/cobranza/agents", headers=h, json={})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "template_unavailable")

    # Desde plantilla: borrador derivado, con los superpoderes de Ventas, sin publicar.
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={"name": "Mis ventas"})
    assert r.status_code == 201, r.text
    creado = r.json()
    aid = creado["agent"]["id"]
    assert (creado["agent"]["key"], creado["agent"]["name"], creado["agent"]["latest_version"]) == (
        "ventas", "Mis ventas", None,
    )
    contenido = creado["draft"]["content"]
    assert {p["id"] for p in contenido["procedures"]} == {"pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"}
    assert creado["draft"]["lock_version"] == 1

    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "key_taken")
    assert r.json()["error"]["fields"]["agent_id"] == aid

    # Destinos: la conexión, sin el agente todavía y con las habilidades.
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/publish-targets", headers=h)
    assert r.status_code == 200, r.text
    assert [(t["connection_id"], t["installed"], t["covers_skills"]) for t in r.json()["items"]] == [(str(cid), False, True)]

    # D-35: sin conexión, o con una que no es del negocio, no se publica.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": []})
    assert r.status_code == 422, r.text
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(uuid.uuid4())]})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_connections"), r.text
    assert "connection_ids[0]" in r.json()["error"]["fields"]

    # Validación: con frases insuficientes, error junto al campo de cada superpoder.
    _pocas_frases(monkeypatch)
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_spec"
    assert set(error["fields"]) == {"procedures[0].overrides.examples", "procedures[1].overrides.examples"}
    assert "frases" in error["fields"]["procedures[0].overrides.examples"]

    # Copiar frases de la habilidad no suma; frases propias sí. Y un modelo de otro proveedor no pasa.
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()
    nuevo = borrador["content"]
    for ref in nuevo["procedures"]:
        ref["overrides"] = {"examples": ["Quiero hacer un pedido", "me interesa comprar algo", "necesito cotizar"]}
    nuevo["model"]["primary"] = "claude-haiku-4-5"
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": nuevo, "lock_version": borrador["lock_version"]}
    )
    assert r.status_code == 200, r.text
    lock = r.json()["lock_version"]
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert set(r.json()["error"]["fields"]) == {"model.primary"}, r.text
    nuevo["model"]["primary"] = "deepseek-flash"
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": nuevo, "lock_version": lock})
    lock = r.json()["lock_version"]
    assert await _sql(tid, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(aid)) == []

    # Publicar: versión, instancia (no por defecto) y release candidata que NO se activa.
    r = await app_client.post(
        f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)], "lock_version": lock}
    )
    assert r.status_code == 200, r.text
    pub = r.json()
    assert pub["created"] and pub["version"]["version"] == 1
    [conexion] = pub["connections"]
    assert (conexion["instance"], conexion["release_created"], conexion["error"]) == ("created", True, None)
    instancia = await _sql(
        tid, "select is_default, enabled from agent_instances where agent_id = :a and deleted_at is null", a=uuid.UUID(aid)
    )
    assert [tuple(f) for f in instancia] == [(False, True)]
    activa = (await _sql(tid, "select active_release_id from channel_connections where id = :c", c=cid))[0][0]
    assert activa == activa_antes and str(activa) != conexion["release_id"]

    # Reintentar no duplica: misma versión, instancia existente, misma candidata.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    otra = r.json()
    assert not otra["created"] and otra["version"]["id"] == pub["version"]["id"]
    assert (otra["connections"][0]["instance"], otra["connections"][0]["release_created"]) == ("existing", False)
    assert otra["connections"][0]["release_id"] == conexion["release_id"]

    # Un colaborador con `settings` en escritura pero sin `connections` no publica (403).
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission
    from core.db.session import get_session

    email = f"{uniq('solo-settings24')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(AdminPermission(admin_id=admin.id, section="settings", access="write", tenant_id=None))
    h2 = await api_headers(app_client, tid, email=email, password=PASSWORD)
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h2, json={"connection_ids": [str(cid)]})
    assert r.status_code == 403, r.text
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/publish-targets", headers=h2)
    assert r.status_code == 403, r.text

    # Aislamiento: el dueño de otro negocio no ve ni publica este agente.
    otro, cid_otro = await _negocio()
    h_otro = await _dueno(app_client, otro)
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/publish-targets", headers=h_otro)
    assert r.status_code == 404, r.text
    r = await app_client.post(
        f"/api/v1/admin/agents/{aid}/publish", headers=h_otro, json={"connection_ids": [str(cid_otro)]}
    )
    assert r.status_code == 404, r.text
    # Ni instala un agente propio en la conexión de este negocio.
    r = await app_client.post("/api/v1/admin/agent-templates/agenda/agents", headers=h_otro, json={})
    agenda_otro = r.json()["agent"]["id"]
    r = await app_client.post(
        f"/api/v1/admin/agents/{agenda_otro}/publish", headers=h_otro, json={"connection_ids": [str(cid)]}
    )
    # (Sigue el superpoder con pocas frases: los dos errores vienen juntos, por campo.)
    assert r.status_code == 422 and "connection_ids[0]" in r.json()["error"]["fields"], r.text
    assert await _sql(otro, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(agenda_otro)) == []


async def test_catalogo_de_modelos_con_costo_estimado_del_consumo_real(app_client) -> None:
    tid, _cid = await _negocio()
    h = await _dueno(app_client, tid)

    r = await app_client.get("/api/v1/admin/agent-models", headers=h)
    assert r.status_code == 200, r.text
    catalogo = r.json()
    assert catalogo["provider"] == "deepseek" and catalogo["byok_model"] is None and catalogo["sample_turns"] == 0
    modelos = {m["model"]: m for m in catalogo["items"]}
    assert modelos["deepseek-flash"]["selectable"] and modelos["deepseek-flash"]["input_usd_per_mtok"] == 0.14
    assert not modelos["claude-haiku-4-5"]["selectable"]
    assert not modelos["deepseek-chat"]["selectable"] and modelos["deepseek-chat"]["retired"]
    assert all(m["estimated_usd_per_1000_turns"] is None for m in catalogo["items"])  # sin consumo, sin estimado

    for entrada, salida in ((1000, 200), (3000, 600)):
        await _sql(
            tid,
            "insert into usage_ledger (id, tenant_id, model, input_tokens, output_tokens, cost_usd) "
            "values (:id, :t, 'deepseek-flash', :i, :o, 0)",
            id=uuid.uuid4(), t=tid, i=entrada, o=salida,
        )
    catalogo = (await app_client.get("/api/v1/admin/agent-models", headers=h)).json()
    assert (catalogo["sample_turns"], catalogo["avg_input_tokens"], catalogo["avg_output_tokens"]) == (2, 2000, 400)
    flash = next(m for m in catalogo["items"] if m["model"] == "deepseek-flash")
    # (2000 × 0,14 + 400 × 0,28) / 1e6 × 1000 turnos
    assert flash["estimated_usd_per_1000_turns"] == pytest.approx(0.392)

    # Sin la sección Costos, solo los precios: ni promedios ni estimados.
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission
    from core.db.session import get_session

    email = f"{uniq('sin-costos24')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(AdminPermission(admin_id=admin.id, section="settings", access="read", tenant_id=None))
    lector = (await app_client.get(
        "/api/v1/admin/agent-models", headers=await api_headers(app_client, tid, email=email, password=PASSWORD)
    )).json()
    assert (lector["sample_turns"], lector["avg_input_tokens"]) == (0, None)
    assert all(m["estimated_usd_per_1000_turns"] is None for m in lector["items"])
    assert next(m for m in lector["items"] if m["model"] == "deepseek-flash")["input_usd_per_mtok"] == 0.14

    # El otro negocio no ve este consumo.
    otro, _ = await _negocio()
    r = await app_client.get("/api/v1/admin/agent-models", headers=await _dueno(app_client, otro))
    assert r.json()["sample_turns"] == 0


async def test_superpoderes_por_enfoque(app_client) -> None:
    """Un agente de Ventas solo ve y solo publica lo de Ventas, aunque el negocio tenga Agenda."""
    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)

    r = await app_client.get("/api/v1/admin/agent-superpowers", headers=h)
    assert r.status_code == 200, r.text
    todos = {s["id"] for s in r.json()["items"]}
    assert "pack_calendar.agendar_cita" in todos and "pack_commerce.tomar_pedido" in todos

    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]

    r = await app_client.get(f"/api/v1/admin/agent-superpowers?agent_id={aid}", headers=h)
    assert r.status_code == 200, r.text
    assert {s["id"] for s in r.json()["items"]} == {"pack_commerce.tomar_pedido", "pack_commerce.contratar_plan"}

    # Marcar un superpoder de otro enfoque: el borrador lo guarda, publicar lo rechaza junto al campo.
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()
    nuevo = borrador["content"]
    ajeno = {**nuevo["procedures"][0], "id": "pack_calendar.agendar_cita"}
    nuevo["procedures"].append(ajeno)
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": nuevo, "lock_version": borrador["lock_version"]}
    )
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 422, r.text
    campos = r.json()["error"]["fields"]
    assert "procedures[2].id" in campos and "enfoque" in campos["procedures[2].id"]
    assert await _sql(tid, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(aid)) == []

    # HU-AOS-51: la API genérica de versiones tampoco se salta el enfoque.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
    assert r.status_code == 422, r.text
    assert "enfoque" in r.json()["error"]["fields"]["procedures[2].id"]
    assert await _sql(tid, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(aid)) == []

    # Aislamiento: el agente de un negocio no se consulta desde otro.
    otro, _ = await _negocio()
    h_otro = await _dueno(app_client, otro)
    r = await app_client.get(f"/api/v1/admin/agent-superpowers?agent_id={aid}", headers=h_otro)
    assert r.status_code == 404, r.text


async def test_requisitos_de_configuracion_por_enfoque(app_client) -> None:
    """Ventas avisa lo que le falta al negocio (cobro en línea y catálogo vacío), por plantilla y por agente."""
    tid, _ = await _negocio()
    h = await _dueno(app_client, tid)

    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    ventas = {p["key"]: p for p in r.json()["items"]}["ventas"]
    pedidos = {(q["skill"], q["kind"]): q for q in ventas["requirements"]}
    assert ("pack_payments", "habilidad_sin_configurar") in pedidos
    assert ("pack_commerce", "sin_contenido") in pedidos
    cobro = pedidos[("pack_payments", "habilidad_sin_configurar")]
    assert cobro["title"] and cobro["section"] == "plugins"
    assert pedidos[("pack_commerce", "sin_contenido")]["content"] == "catalogo"

    # Por agente: lo mismo, y sin secretos ni datos de clientes en la respuesta.
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/requirements", headers=h)
    assert r.status_code == 200, r.text
    assert {(q["skill"], q["kind"]) for q in r.json()["items"]} == set(pedidos)

    # Aislamiento: el agente de un negocio no se consulta desde otro.
    otro, _ = await _negocio()
    h_otro = await _dueno(app_client, otro)
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/requirements", headers=h_otro)
    assert r.status_code == 404, r.text


async def test_plantillas_traen_seccion_y_solo_sus_herramientas(app_client) -> None:
    """HU-AOS-51: cada plantilla dice a qué sección del menú pertenece y qué tools de pack tendrá su agente;
    las de Ventas no incluyen `sub_*` ni las de Agenda."""
    tid, _ = await _negocio()
    h = await _dueno(app_client, tid)
    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    por_key = {p["key"]: p for p in r.json()["items"]}
    assert por_key["ventas"]["section"] == "Ventas" and por_key["agenda"]["section"] == "Agenda"
    ventas, agenda = set(por_key["ventas"]["tools"]), set(por_key["agenda"]["tools"])
    assert {"crear_pedido", "generar_cotizacion"} <= ventas
    assert not any(n.startswith("sub_") for n in ventas | agenda)
    assert not (ventas & {"check_availability", "create_calendar_event"})
    assert "check_availability" in agenda and "crear_pedido" not in agenda


async def test_crear_desde_el_guion_de_la_plantilla(app_client) -> None:
    """HU-AOS-48: las plantillas sirven su guion; `from-script` crea el borrador según las respuestas
    (planes → solo `contratar_plan`), con 422 por pregunta, 409 `key_taken` y los requisitos pendientes."""
    tid, _ = await _negocio()
    h = await _dueno(app_client, tid)

    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    ventas = {p["key"]: p for p in r.json()["items"]}["ventas"]
    que_vendes = next(q for q in ventas["questions"] if q["id"] == "que_vendes")
    assert (que_vendes["type"], que_vendes["default"]) == ("option", "productos")
    assert [o["id"] for o in que_vendes["options"]] == ["productos", "planes", "ambos"]

    # Respuesta fuera de las opciones: 422 por pregunta y no se crea nada.
    malo = {"template_key": "ventas", "answers": {"que_vendes": "servicios"}}
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=malo)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_answers"), r.text
    assert set(r.json()["error"]["fields"]) == {"que_vendes"}
    # (que no se creó nada lo prueba el 201 siguiente: con un agente ya creado daría 409 `key_taken`)

    # Planes: borrador sin publicar, solo con `contratar_plan`, con el largo elegido y los requisitos.
    ok = {"template_key": "ventas", "answers": {"que_vendes": "planes", "largo_respuesta": "cortas"}}
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=ok)
    assert r.status_code == 201, r.text
    creado = r.json()
    assert (creado["agent"]["key"], creado["agent"]["latest_version"]) == ("ventas", None)
    contenido = creado["draft"]["content"]
    assert [p["id"] for p in contenido["procedures"]] == ["pack_commerce.contratar_plan"]
    assert contenido["persona"]["maxLength"] == 300
    assert isinstance(creado["requirements"], list)
    assert all({"skill", "kind", "title", "section"} <= set(x) for x in creado["requirements"])

    # El mismo guion otra vez: 409 con el agente existente.
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=ok)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "key_taken")
    assert r.json()["error"]["fields"]["agent_id"] == creado["agent"]["id"]

    # Sin responder nada: productos (el defecto) y el largo del General.
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "agenda"})
    assert r.status_code == 201, r.text
    assert [p["id"] for p in r.json()["draft"]["content"]["procedures"]] == ["pack_calendar.agendar_cita"]


async def _general_id(tid: uuid.UUID) -> uuid.UUID:
    from core.agents.backfill_agentes import backfill_agentes

    reporte = await backfill_agentes(dry_run=False, tenant_id=tid, modules=_modules())
    assert not reporte.fallo, reporte
    filas = await _sql(tid, "select id from agents where key = 'general' and deleted_at is null")
    return filas[0][0]


async def _publicar_desde_el_guion(app_client, h, cid, plantilla: str, respuestas: dict[str, str]) -> dict[str, Any]:
    r = await app_client.post(
        "/api/v1/admin/agents/from-script", headers=h, json={"template_key": plantilla, "answers": respuestas}
    )
    assert r.status_code == 201, r.text
    creado = r.json()
    r = await app_client.post(
        f"/api/v1/admin/agents/{creado['agent']['id']}/publish",
        headers=h,
        json={"connection_ids": [str(cid)], "lock_version": creado["draft"]["lock_version"]},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_publicar_un_especialista_enlaza_al_general_y_con_la_bandera_lo_recorta(app_client) -> None:
    """HU-AOS-52: al publicar Ventas se escribe el enlace A2A General → Ventas (handoff). El General solo se
    recorta con `settings.agent_os.recortar_general`; Agenda después sigue derivando sus tools."""
    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)
    general = await _general_id(tid)

    def _tools_del_general() -> Any:
        return _sql(
            tid,
            "select content from agent_versions where agent_id = :g order by version desc limit 1",
            g=general,
        )

    version_antes = (await _sql(tid, "select max(version) from agent_versions where agent_id = :g", g=general))[0][0]
    pub = await _publicar_desde_el_guion(app_client, h, cid, "ventas", {"que_vendes": "ambos"})
    assert pub["general_updated"] is None  # bandera apagada: solo el enlace
    enlaces = await _sql(
        tid,
        "select l.modes, ag.key from agent_links l join agents ag on ag.id = l.to_agent_id and ag.tenant_id = l.tenant_id "
        "where l.from_agent_id = :g",
        g=general,
    )
    assert [(list(m), k) for m, k in enlaces] == [(["handoff"], "ventas")]
    assert (await _sql(tid, "select max(version) from agent_versions where agent_id = :g", g=general))[0][0] == version_antes

    # Republicar no duplica el enlace.
    [(ventas_id,)] = await _sql(tid, "select id from agents where key = 'ventas' and deleted_at is null")
    r = await app_client.post(f"/api/v1/admin/agents/{ventas_id}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    assert len(await _sql(tid, "select 1 from agent_links where from_agent_id = :g", g=general)) == 1

    # Con la bandera, el siguiente especialista (Agenda) recorta al General de lo suyo.
    await _sql(
        tid,
        "update tenants set settings = jsonb_set(coalesce(settings, '{}'::jsonb), '{agent_os}', "
        "coalesce(settings->'agent_os', '{}'::jsonb) || '{\"recortar_general\": true}'::jsonb) where id = :t",
        t=tid,
    )
    pub = await _publicar_desde_el_guion(app_client, h, cid, "agenda", {})
    assert pub["general_updated"] == version_antes + 1
    [(contenido,)] = await _tools_del_general()
    assert not any(p["id"].startswith("pack_calendar.") for p in contenido["procedures"])
    assert any(p["id"].startswith("pack_commerce.") for p in contenido["procedures"])  # Comercio sigue en el General
    assert not any(b["tool"] in ("check_availability", "create_calendar_event") for b in contenido["tools"])
    assert any(b["tool"] == "escalate_to_human" for b in contenido["tools"])

    # Otra plantilla sigue derivando con la configuración que el General tuvo: lo recortado se recupera de
    # sus versiones anteriores para el siguiente especialista.
    from core.agents import editor
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        base = await editor._base_del_general(tid)
    assert any(b["tool"] == "check_availability" for b in base["tools"])
