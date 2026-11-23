"""HU-AOS-24 · editor de agentes desde plantillas por `/api/v1` (adenda de ADR-0139, 2026-10-18).

Negocio v3 con `pack_commerce` y `pack_calendar`, una conexión conversacional con su release 1 y
`AOS_CONNECTION_ROUTING` encendida. Gherkin:

- «Desde plantilla»: con Comercio habilitado, la plantilla Ventas trae sus superpoderes y las
  frases sugeridas; el borrador nace derivado y sin publicar.
- «Validación»: un superpoder sin las frases mínimas no se publica; el error viene por campo
  (`procedures[i].overrides.examples`), no como un JSON del contrato.
- D-35: sin conexión no se publica; al publicar se instala el agente y se construye la release
  candidata, sin activarla; reintentar no duplica nada.

Más: plantilla sin habilidades, key ocupada, modelo que no sirve (HU-AOS-79), catálogo de modelos con el
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
    pass



async def _negocio() -> tuple[uuid.UUID, uuid.UUID]:
    from _habilidades import onboard_tenant
    from release_de_prueba import release_inicial

    slug = f"aos24-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS24 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    reporte = await release_inicial(tid, _modules())
    assert not reporte.fallo, reporte.errores
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

    # Plantillas: Ventas disponible con sus superpoderes y frases; Cobranza también, y al crearla habilitará
    # su habilidad (`will_enable`): estar habilitada ya no es precondición (ver test_plantilla_habilita_habilidades).
    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    plantillas = {p["key"]: p for p in r.json()["items"]}
    assert set(plantillas) == {"agenda", "cobranza", "suscripciones", "ventas"}
    ventas = plantillas["ventas"]
    assert ventas["available"] and ventas["missing_skills"] == [] and ventas["will_enable"] == []
    assert [s["id"] for s in ventas["superpowers"]] == ["pack_commerce.tomar_pedido", "pack_commerce.pago_en_linea", "pack_commerce.comprobar_transferencia"]
    assert all(len(s["phrases"]) >= 3 for s in ventas["superpowers"])
    cobranza = plantillas["cobranza"]
    assert cobranza["available"] and cobranza["missing_skills"] == [] and cobranza["will_enable"] == ["pack_cobranza"]

    # Desde plantilla: borrador derivado, con los superpoderes de Ventas, sin publicar.
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={"name": "Mis ventas"})
    assert r.status_code == 201, r.text
    creado = r.json()
    aid = creado["agent"]["id"]
    assert (creado["agent"]["key"], creado["agent"]["name"], creado["agent"]["latest_version"]) == (
        "ventas", "Mis ventas", None,
    )
    contenido = creado["draft"]["content"]
    assert {p["id"] for p in contenido["procedures"]} == {"pack_commerce.tomar_pedido", "pack_commerce.pago_en_linea", "pack_commerce.comprobar_transferencia"}
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
    # Con el mínimo subido, falla cada superpoder de Ventas (pedidos y formas de pago, SPEC-48).
    assert set(error["fields"]) == {f"procedures[{i}].overrides.examples" for i in range(3)}
    assert "frases" in error["fields"]["procedures[0].overrides.examples"]

    # Copiar frases de la habilidad no suma; frases propias sí. (Un modelo que no sirve ya no
    # bloquea: test_modelo_que_no_sirve_usa_el_del_negocio, HU-AOS-79.)
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()
    nuevo = borrador["content"]
    for ref in nuevo["procedures"]:
        ref["overrides"] = {"examples": ["Quiero hacer un pedido", "me interesa comprar algo", "necesito cotizar"]}
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": nuevo, "lock_version": borrador["lock_version"]}
    )
    assert r.status_code == 200, r.text
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
    # (Sigue el superpoder con pocas frases: ambos errores vienen juntos, por campo.)
    assert r.status_code == 422 and "connection_ids[0]" in r.json()["error"]["fields"], r.text
    assert await _sql(otro, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(agenda_otro)) == []


async def test_catalogo_de_modelos_con_costo_estimado_del_consumo_real(app_client) -> None:
    """El catálogo de modelos es del master (ADR-0210 §5): el gestor lo recibe vacío."""
    tid, _cid = await _negocio()
    h = await api_headers(app_client, tid)  # master

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
    # El gestor no ve modelos ni proveedor.
    assert lector["items"] == [] and lector["provider"] == "" and lector["business_model"] == ""
    dueno = (await app_client.get("/api/v1/admin/agent-models", headers=await _dueno(app_client, tid))).json()
    assert dueno["items"] == [] and dueno["sample_turns"] == 2  # con la sección Costos ve el consumo

    # El otro negocio no ve este consumo.
    otro, _ = await _negocio()
    r = await app_client.get("/api/v1/admin/agent-models", headers=await _dueno(app_client, otro))
    assert r.json()["sample_turns"] == 0


async def _poner_modelo(app_client, h: dict[str, str], aid: str, modelo: str) -> None:
    """Fija el modelo del borrador. `h` es de un master: el negocio no ve ni cambia el modelo (ADR-0210 §1)."""
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()
    contenido = borrador["content"]
    contenido["model"]["primary"] = modelo
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": contenido, "lock_version": borrador["lock_version"]}
    )
    assert r.status_code == 200, r.text


async def _llave_propia(tid: uuid.UUID, modelo: str | None = None) -> None:
    """BYOK del negocio: proveedor y llave en `app_settings` (y, si se pide, el modelo que fija)."""
    from core.shared.crypto import get_secret_box

    await _sql(
        tid,
        "update app_settings set llm_provider = 'deepseek', llm_api_key_encrypted = :k, llm_model = :m where tenant_id = :t",
        k=get_secret_box().encrypt("sk-test-no-real"), m=modelo, t=tid,
    )


async def test_modelo_que_no_sirve_usa_el_del_negocio(app_client) -> None:
    """HU-AOS-79 con llave propia: el dueño ya no elige modelo. Uno de otro proveedor o retirado no bloquea la
    publicación: la versión usa el del negocio y lo anota sin nombrarlo (ADR-0210 §5); uno válido se respeta.
    Con la llave de la plataforma manda el nivel de IA (ADR-0210 §3)."""
    from core.agents.editor import NOTA_MODELO_DEL_NEGOCIO

    tid, cid = await _negocio()
    h, master = await _dueno(app_client, tid), await api_headers(app_client, tid)
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]

    # Llave de la plataforma: un modelo de otro proveedor no se anota; la versión lleva el del nivel.
    await _poner_modelo(app_client, master, aid, "claude-haiku-4-5")
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    assert NOTA_MODELO_DEL_NEGOCIO not in r.json()["warnings"]
    [(contenido,)] = await _sql(
        tid, "select content from agent_versions where id = :v", v=uuid.UUID(r.json()["version"]["id"])
    )
    assert (contenido["model"]["primary"], contenido["model"]["router"]) == ("deepseek-flash", "deepseek-flash")

    await _llave_propia(tid)
    del_negocio = (await app_client.get("/api/v1/admin/agent-models", headers=master)).json()["business_model"]
    assert del_negocio  # sin modelo fijado: el del despliegue, que el proveedor del negocio sirve

    # Modelo de otro proveedor: el catálogo (del master) dice que atenderá con el del negocio.
    catalogo = (await app_client.get(f"/api/v1/admin/agent-models?agent_id={aid}", headers=master)).json()
    assert (catalogo["effective_model"], catalogo["effective_source"]) == (del_negocio, "negocio")

    # Publicar no falla: la versión lleva el del negocio, con la nota sin el nombre; el borrador no cambia.
    await _poner_modelo(app_client, master, aid, "claude-haiku-4-5")
    r = await app_client.post(
        f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)], "notes": "Primera"}
    )
    assert r.status_code == 200, r.text
    pub = r.json()
    assert NOTA_MODELO_DEL_NEGOCIO in pub["warnings"]
    assert del_negocio not in r.text
    [(contenido, notas)] = await _sql(
        tid, "select content, notes from agent_versions where id = :v", v=uuid.UUID(pub["version"]["id"])
    )
    assert contenido["model"]["primary"] == del_negocio and notas == f"Primera\n{NOTA_MODELO_DEL_NEGOCIO}"
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=master)).json()
    assert borrador["content"]["model"]["primary"] == "claude-haiku-4-5"
    # El dueño no ve el modelo en su borrador (HU-BILL-24).
    assert "model" not in (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()["content"]

    # Retirado: igual.
    await _poner_modelo(app_client, master, aid, "deepseek-chat")
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200 and NOTA_MODELO_DEL_NEGOCIO in r.json()["warnings"], r.text

    # Un modelo válido distinto (lo fija un master en el JSON) se respeta con llave propia, sin aviso.
    catalogo = (await app_client.get("/api/v1/admin/agent-models", headers=master)).json()
    propio = next(m for m in catalogo["items"] if m["selectable"] and m["model"] != del_negocio)["model"]
    await _poner_modelo(app_client, master, aid, propio)
    catalogo = (await app_client.get(f"/api/v1/admin/agent-models?agent_id={aid}", headers=master)).json()
    assert (catalogo["effective_model"], catalogo["effective_source"]) == (propio, "agente")
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    assert NOTA_MODELO_DEL_NEGOCIO not in r.json()["warnings"]
    [(contenido,)] = await _sql(
        tid, "select content from agent_versions where id = :v", v=uuid.UUID(r.json()["version"]["id"])
    )
    assert contenido["model"]["primary"] == propio

    # Aislamiento: otro negocio no consulta el modelo de este agente.
    otro, _ = await _negocio()
    r = await app_client.get(f"/api/v1/admin/agent-models?agent_id={aid}", headers=await _dueno(app_client, otro))
    assert r.status_code == 404, r.text


async def test_cada_negocio_usa_su_modelo_por_defecto(app_client) -> None:
    """HU-AOS-79 · aislamiento, con llave propia: dos negocios con modelos por defecto distintos. Cada agente con un
    modelo que no sirve publica con el de SU negocio y el catálogo (del master) de cada uno dice el suyo."""
    from core.agents.editor import AVISO_LLAVE_PROPIA, NOTA_MODELO_DEL_NEGOCIO

    a, cid_a = await _negocio()
    b, cid_b = await _negocio()
    await _llave_propia(a, "deepseek-v4-pro")
    await _llave_propia(b)
    ha, hb = await _dueno(app_client, a), await _dueno(app_client, b)
    del_b = (await app_client.get("/api/v1/admin/agent-models", headers=await api_headers(app_client, b))).json()["business_model"]
    del_a = (await app_client.get("/api/v1/admin/agent-models", headers=await api_headers(app_client, a))).json()["business_model"]
    assert del_a == "deepseek-v4-pro" and del_b != "deepseek-v4-pro"

    masters = {id(ha): await api_headers(app_client, a), id(hb): await api_headers(app_client, b)}
    for h, cid, esperado in ((ha, cid_a, "deepseek-v4-pro"), (hb, cid_b, del_b)):
        master = masters[id(h)]
        r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
        assert r.status_code == 201, r.text
        aid = r.json()["agent"]["id"]
        await _poner_modelo(app_client, master, aid, "claude-haiku-4-5")
        r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
        assert r.status_code == 200, r.text
        assert NOTA_MODELO_DEL_NEGOCIO in r.json()["warnings"]
        assert (AVISO_LLAVE_PROPIA in r.json()["warnings"]) is (h is ha)
        assert esperado not in str(r.json()["warnings"])
        tid = a if h is ha else b
        [(contenido,)] = await _sql(
            tid, "select content from agent_versions where id = :v", v=uuid.UUID(r.json()["version"]["id"])
        )
        assert contenido["model"]["primary"] == esperado


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
    assert {s["id"] for s in r.json()["items"]} == {"pack_commerce.tomar_pedido", "pack_commerce.pago_en_linea", "pack_commerce.comprobar_transferencia"}

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
    ultimo = f"procedures[{len(nuevo['procedures']) - 1}].id"  # el ajeno va al final
    assert ultimo in campos and "enfoque" in campos[ultimo]
    assert await _sql(tid, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(aid)) == []

    # HU-AOS-51: la API genérica de versiones tampoco se salta el enfoque.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
    assert r.status_code == 422, r.text
    assert "enfoque" in r.json()["error"]["fields"][ultimo]
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
    (la pregunta `que_vendes` salió con SPEC-43 y se ignora), con 422 por pregunta, 409 `key_taken` y los requisitos pendientes."""
    tid, _ = await _negocio()
    h = await _dueno(app_client, tid)

    r = await app_client.get("/api/v1/admin/agent-templates", headers=h)
    assert r.status_code == 200, r.text
    ventas = {p["key"]: p for p in r.json()["items"]}["ventas"]
    assert "que_vendes" not in {q["id"] for q in ventas["questions"]}

    # Respuesta fuera de las opciones: 422 por pregunta y no se crea nada.
    malo = {"template_key": "ventas", "answers": {"largo_respuesta": "enorme"}}
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=malo)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_answers"), r.text
    assert set(r.json()["error"]["fields"]) == {"largo_respuesta"}
    # (que no se creó nada lo prueba el 201 siguiente: con un agente ya creado daría 409 `key_taken`)

    # Un cliente viejo (panel o APK anteriores) todavía envía `que_vendes`: se ignora, no da 422.
    ok = {"template_key": "ventas", "answers": {"que_vendes": "planes", "largo_respuesta": "cortas"}}
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=ok)
    assert r.status_code == 201, r.text
    creado = r.json()
    assert (creado["agent"]["key"], creado["agent"]["latest_version"]) == ("ventas", None)
    contenido = creado["draft"]["content"]
    # Aunque pida «planes», Ventas ya no vende planes: quedan sus pedidos y formas de pago (SPEC-48).
    assert sorted(p["id"] for p in contenido["procedures"]) == [
        "pack_commerce.comprobar_transferencia",
        "pack_commerce.pago_en_linea",
        "pack_commerce.tomar_pedido",
    ]
    assert contenido["persona"]["maxLength"] == 300
    assert isinstance(creado["requirements"], list)
    assert all({"skill", "kind", "title", "section"} <= set(x) for x in creado["requirements"])

    # El mismo guion otra vez: 409 con el agente existente.
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json=ok)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "key_taken")
    assert r.json()["error"]["fields"]["agent_id"] == creado["agent"]["id"]

    # Sin responder nada: productos (el defecto) y el largo de la configuración del negocio.
    r = await app_client.post("/api/v1/admin/agents/from-script", headers=h, json={"template_key": "agenda"})
    assert r.status_code == 201, r.text
    assert [p["id"] for p in r.json()["draft"]["content"]["procedures"]] == ["pack_calendar.agendar_cita"]


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


async def test_flujo_de_atencion_por_agente(app_client) -> None:
    """HU-AOS-74: el flujo vive en el agente, valida acciones y se congela al publicar."""
    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={"name": "Ventas flujo"})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]

    # Sin flujo propio, la lectura devuelve la semilla del pack (no se guarda todavía).
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/flow", headers=h)
    assert r.status_code == 200, r.text
    leido = r.json()
    assert leido["seeded"] is True and leido["source"] == "semilla"
    assert leido["flow"]["sections"]
    assert leido["warnings"] == []

    opciones = (await app_client.get(f"/api/v1/admin/agents/{aid}/flow/options", headers=h)).json()
    accion = next(a["tool"] for a in opciones["actions"] if a["enabled"] or a["by_superpower"])
    nuevo = {"sections": [{"id": "propia", "label": "Propia", "purpose": "", "tools": [accion]}]}
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/flow",
        headers=h,
        json={"flow": nuevo, "lock_version": leido["lock_version"]},
    )
    assert r.status_code == 200, r.text
    guardado = r.json()
    assert guardado["seeded"] is False and guardado["flow"] == nuevo

    # Una acción que el agente no puede ejecutar no se guarda.
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/flow",
        headers=h,
        json={
            "flow": {"sections": [{"id": "mala", "label": "Mala", "purpose": "", "tools": ["no_existe"]}]},
            "lock_version": guardado["lock_version"],
        },
    )
    assert r.status_code == 422, r.text
    assert "sections[0].tools[0]" in r.json()["error"]["fields"]

    # Publicar congela el flujo y su hash en la versión.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    publicada = r.json()
    assert publicada["version"]["flow_hash"]
    detalle = (await app_client.get(
        f"/api/v1/admin/agents/{aid}/versions/{publicada['version']['id']}", headers=h
    )).json()
    assert detalle["flow"] == nuevo

    # Aislamiento: otro negocio no ve el flujo.
    otro, _ = await _negocio()
    h_otro = await _dueno(app_client, otro)
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/flow", headers=h_otro)
    assert r.status_code == 404, r.text


HORARIO_VENTAS = {"tz": "America/Bogota", "ranges": [{"day": 0, "from": "08:00", "to": "10:00"}]}


async def test_horario_por_agente(app_client) -> None:
    """HU-AOS-82: un agente fija su horario sin tocar a los demás; publicar un cambio solo de horario
    es una versión nueva; la release lo congela; el ingreso lo usa y, sin horario, cae al del negocio."""
    from types import SimpleNamespace

    from core.kernel.ingress import horario_del_agente
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)
    ventas = (await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={"name": "Ventas h"})).json()
    agenda = (await app_client.post("/api/v1/admin/agent-templates/agenda/agents", headers=h, json={"name": "Agenda h"})).json()
    aid, otro = ventas["agent"]["id"], agenda["agent"]["id"]

    r = await app_client.get(f"/api/v1/admin/agents/{aid}/schedule", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "negocio" and r.json()["schedule"] is None

    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/schedule", headers=h,
        json={"schedule": {"tz": "Marte/Base", "ranges": []}, "lock_version": r.json()["lock_version"]},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_schedule"

    lock = (await app_client.get(f"/api/v1/admin/agents/{aid}/schedule", headers=h)).json()["lock_version"]
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/schedule", headers=h, json={"schedule": HORARIO_VENTAS, "lock_version": lock})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "agente" and r.json()["schedule"] == HORARIO_VENTAS
    # Aislamiento: el otro agente sigue con el del negocio.
    assert (await app_client.get(f"/api/v1/admin/agents/{otro}/schedule", headers=h)).json()["source"] == "negocio"

    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    v1 = r.json()["version"]["id"]
    # Un cambio SOLO de horario es una versión nueva (antes chocaba con la unicidad por contenido).
    lock = (await app_client.get(f"/api/v1/admin/agents/{aid}/schedule", headers=h)).json()["lock_version"]
    otro_horario = {**HORARIO_VENTAS, "ranges": [{"day": 1, "from": "09:00", "to": "11:00"}]}
    await app_client.put(f"/api/v1/admin/agents/{aid}/schedule", headers=h, json={"schedule": otro_horario, "lock_version": lock})
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    assert r.json()["version"]["id"] != v1
    [(congelado,)] = await _sql(tid, "select schedule from agent_versions where id = :v", v=uuid.UUID(r.json()["version"]["id"]))
    assert congelado == otro_horario

    # El ingreso: con el dueño vigente en Ventas vale su horario; sin conversación conocida, None (negocio).
    [(release_id,)] = await _sql(
        tid, "select id from connection_releases where connection_id = :c order by number desc limit 1", c=cid
    )
    [(instancia,)] = await _sql(
        tid, "select id from agent_instances where agent_id = :a and connection_id = :c", a=uuid.UUID(aid), c=cid
    )
    cliente = uuid.uuid4()
    conv = uuid.uuid4()
    await _sql(tid, "insert into customers (id, tenant_id, channel, external_id) values (:i, :t, 'instagram', :e)", i=cliente, t=tid, e=f"ig:{conv.hex[:8]}")
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, connection_id, release_id, active_agent_instance_id, channel) "
        "values (:i, :t, :c, :cc, :r, :a, 'instagram')",
        i=conv, t=tid, c=cliente, cc=cid, r=release_id, a=instancia,
    )
    async with tenant_scope(tid):
        assert await horario_del_agente(SimpleNamespace(id=conv, tenant_id=tid)) == otro_horario
        assert await horario_del_agente(SimpleNamespace(id=uuid.uuid4(), tenant_id=tid)) is None

    # Otro negocio no ve ni cambia el horario.
    otro_negocio, _ = await _negocio()
    h_otro = await _dueno(app_client, otro_negocio)
    assert (await app_client.get(f"/api/v1/admin/agents/{aid}/schedule", headers=h_otro)).status_code == 404
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/schedule", headers=h_otro, json={"schedule": None, "lock_version": 0})
    assert r.status_code == 404


async def test_el_negocio_no_ve_ni_cambia_el_modelo_del_agente(app_client) -> None:
    """HU-BILL-24 · ADR-0210 §1: el detalle, el borrador y la versión del agente no le nombran el modelo al
    dueño; guardar su borrador sin `model` (o con otro) conserva el del borrador. El master lo ve."""
    tid, cid = await _negocio()
    h, master = await _dueno(app_client, tid), await api_headers(app_client, tid)
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]
    await _poner_modelo(app_client, master, aid, "deepseek-v4-pro")

    detalle = (await app_client.get(f"/api/v1/admin/agents/{aid}", headers=h)).json()
    borrador = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)).json()
    for contenido in (detalle["draft"]["content"], borrador["content"]):
        assert "model" not in contenido
    assert "deepseek" not in str(detalle) + str(borrador)

    # El dueño guarda su borrador (sin `model`, y después intentando poner otro): el modelo no cambia.
    contenido = {**borrador["content"], "description": "Atiende ventas"}
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": contenido, "lock_version": borrador["lock_version"]}
    )
    assert r.status_code == 200, r.text
    assert "model" not in r.json()["content"]
    otro = {**r.json()["content"], "model": {"primary": "claude-haiku-4-5"}}
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": otro, "lock_version": r.json()["lock_version"]}
    )
    assert r.status_code == 200, r.text
    [(guardado,)] = await _sql(tid, "select content from agent_drafts where agent_id = :a", a=uuid.UUID(aid))
    assert guardado["model"]["primary"] == "deepseek-v4-pro" and guardado["description"] == "Atiende ventas"
    del_master = (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=master)).json()
    assert del_master["content"]["model"]["primary"] == "deepseek-v4-pro"

    # La versión publicada tampoco le nombra el modelo al dueño; al master sí.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    vid = r.json()["version"]["id"]
    assert "model" not in (await app_client.get(f"/api/v1/admin/agents/{aid}/versions/{vid}", headers=h)).json()["content"]
    assert "model" in (await app_client.get(f"/api/v1/admin/agents/{aid}/versions/{vid}", headers=master)).json()["content"]
