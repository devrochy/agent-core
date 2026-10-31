"""HU-QA-13 · escenarios de datos de prueba en el negocio `qa-` del tester (adenda de ADR-0135).

Con `QA_TESTERS_ENABLED` y `AOS_CONNECTION_ROUTING` (monkeypatch), sin LLM:

- **Cada escenario crea lo prometido** y se ve por la misma API que usa el panel (turno, tarjeta
  «En curso», escalaciones, calidad, versiones, canary, pedidos, suscripciones, campana, fallidos,
  cobranza).
- **Solo en el negocio `qa-` del tester**: sin datos preparados, 409; con el slug cambiado, 409;
  nada se escribe en otro negocio.
- **Sin la bandera, 404** (la lista y la creación).
- **Repetir no rompe**: crea otro caso, conserva el anterior y la lista muestra el último.
- **«Preparar mis datos» con una prueba en curso** no promueve y el Agent OS sigue listo.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-del-tester-escenarios-13"
LANDING = "https://landing-test.example.test"
BASE = "/api/v1/admin/qa/datos/escenarios"
SLUGS = (
    "conversacion-con-turnos",
    "escalacion-en-curso",
    "calidad-con-muestra",
    "version-candidata",
    "canary-en-curso",
    "clientes-y-pedidos",
    "suscripciones",
    "notificaciones",
    "mensajes-fallidos",
    "cobranza-completa",
)


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _uno(tid: uuid.UUID, sql: str, **params: Any) -> Any:
    ((valor,),) = await _sql(tid, sql, **params)
    return valor


@pytest.fixture(scope="module", autouse=True)
async def _proceso() -> None:
    """Packs registrados y activos en el proceso (procedimientos incluidos), y las tools del core."""
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(plugin_manager.scan())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


@pytest.fixture(scope="module")
def entorno() -> Any:
    from core.config import get_settings

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(get_settings(), "qa_testers_enabled", True)
        mp.setattr(get_settings(), "aos_connection_routing", True)
        mp.setattr(get_settings(), "landing_base_url", LANDING)
        yield mp


async def _tester(app_client: Any, t1: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"tester-esc-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True))
    return await api_headers(app_client, t1, email=email, password=PASSWORD)


@pytest.fixture(scope="module")
async def preparado(app_client: Any, tenant_setup: Any, entorno: Any) -> AsyncIterator[dict[str, Any]]:
    """Un tester con «Preparar mis datos» hecho (Agent OS listo) y los headers del dueño (master)
    sobre su negocio de prueba para leer lo creado por la API del panel."""
    t1, t2 = tenant_setup
    headers = await _tester(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    assert datos["agent_os"]["estado"] == "listo", datos["agent_os"]
    tid = uuid.UUID(datos["negocio"]["id"])
    yield {"headers": headers, "tid": tid, "otro": t2, "panel": await api_headers(app_client, tid)}


async def _crear(app_client: Any, p: dict[str, Any], slug: str) -> dict[str, Any]:
    r = await app_client.post(f"{BASE}/{slug}", headers=p["headers"])
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    assert cuerpo["slug"] == slug and cuerpo["negocio_id"] == str(p["tid"])
    assert cuerpo["creado"]["detalle"] and cuerpo["creado"]["ruta"].startswith("/")
    return cuerpo


def _id_de_ruta(cuerpo: dict[str, Any], prefijo: str) -> str:
    ruta = cuerpo["creado"]["ruta"]
    assert ruta.startswith(prefijo), ruta
    return ruta.removeprefix(prefijo).split("/")[0]


# ---------------------------------------------------------------- reglas
async def test_sin_la_bandera_no_existen(app_client: Any, preparado: dict[str, Any], entorno: Any) -> None:
    from core.config import get_settings

    entorno.setattr(get_settings(), "qa_testers_enabled", False)
    try:
        # Sin la bandera el rol tester ni siquiera entra; el master recibe 404.
        assert (await app_client.get(BASE, headers=preparado["headers"])).status_code == 401
        assert (await app_client.get(BASE, headers=preparado["panel"])).status_code == 404
        assert (await app_client.post(f"{BASE}/notificaciones", headers=preparado["panel"])).status_code == 404
    finally:
        entorno.setattr(get_settings(), "qa_testers_enabled", True)


async def test_lista_los_diez_escenarios(app_client: Any, preparado: dict[str, Any]) -> None:
    r = await app_client.get(BASE, headers=preparado["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["datos_listos"] is True
    assert [e["slug"] for e in cuerpo["escenarios"]] == list(SLUGS)
    titulos = {e["slug"]: e["titulo"] for e in cuerpo["escenarios"]}
    assert titulos["canary-en-curso"] == "Prueba con una parte" and titulos["conversacion-con-turnos"] == "Conversación con turnos"
    assert (await app_client.post(f"{BASE}/no-existe", headers=preparado["headers"])).status_code == 404


async def test_sin_datos_preparados_pide_prepararlos(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    t1, _t2 = tenant_setup
    headers = await _tester(app_client, t1)
    assert (await app_client.get(BASE, headers=headers)).json()["datos_listos"] is False
    r = await app_client.post(f"{BASE}/notificaciones", headers=headers)
    assert r.status_code == 409 and "Prepara tus datos" in r.text


async def test_un_colaborador_con_qa_no_crea_escenarios(app_client: Any, preparado: dict[str, Any]) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, AdminPermission, TenantUser
    from core.db.session import get_session

    email = f"colab-esc-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador", active=True)
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=preparado["tid"], admin_id=admin.id, role="colaborador"))
        session.add(AdminPermission(admin_id=admin.id, tenant_id=None, section="qa", access="write"))
    h = await api_headers(app_client, preparado["tid"], email=email, password=PASSWORD)
    assert (await app_client.post(f"{BASE}/notificaciones", headers=h)).status_code == 403


# ---------------------------------------------------------------- cada escenario
async def test_conversacion_con_turnos(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    cuerpo = await _crear(app_client, p, "conversacion-con-turnos")
    conv = _id_de_ruta(cuerpo, "/conversaciones/")
    r = await app_client.get(f"/api/v1/admin/conversations/{conv}", headers=p["panel"])
    assert r.status_code == 200, r.text
    turnos = [m["turn_id"] for m in r.json()["messages"] if m.get("turn_id")]
    assert len(turnos) == 3, r.json()["messages"]
    detalles = []
    for turno in turnos:
        d = await app_client.get(f"/api/v1/admin/turns/{turno}", headers=p["panel"])
        assert d.status_code == 200, d.text
        detalles.append(d.json())
    normal, accion, traspaso = detalles
    assert normal["status"] == "replied" and normal["decision"]["outcome"] == "routed"
    # Acción con datos personales enmascarados, comando, A2A, consumo y un rechazo del verificador.
    paso = next(s for s in accion["steps"] if s["kind"] == "action")
    assert paso["input"]["email"].startswith("a***@") and paso["input"]["telefono"] == "•••0147"
    assert any(s["kind"] == "command" for s in accion["steps"])
    assert any(v["verdict"] == "fail" for v in accion["verifier"]) and accion["verifier_retries"] == 1
    assert accion["a2a"] and accion["cost"]
    # Traspaso: atendió Ventas y terminó Agenda.
    nombres = dict(await _sql(p["tid"], "select key, name from agents where tenant_id = :t", t=p["tid"]))
    decision = traspaso["decision"]
    assert decision["active_agent"]["name"] == nombres["ventas"] and decision["final_agent"]["name"] == nombres["agenda"], decision
    modos = {e.get("mode") for a in traspaso["a2a"] for e in a.get("events", [])}
    assert "handoff" in modos, traspaso["a2a"]


async def test_escalacion_en_curso(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    cuerpo = await _crear(app_client, p, "escalacion-en-curso")
    conv = _id_de_ruta(cuerpo, "/conversaciones/")
    r = await app_client.get(f"/api/v1/admin/conversations/{conv}/in-progress", headers=p["panel"])
    assert r.status_code == 200, r.text
    cuerpo_en_curso = r.json()
    assert cuerpo_en_curso["escalation"]["status"] == "pendiente", cuerpo_en_curso
    tareas = {t["procedure_id"]: t for t in cuerpo_en_curso["tasks"]}
    plan = tareas["pack_subscriptions.contratar_plan"]
    assert plan["status"] == "active" and plan["stage"] == "elegir_plan" and plan["in_focus"], plan
    datos = {d["name"]: d for d in plan["data"]}
    assert {"nombre", "email", "monto_a_pagar"} <= set(datos), datos
    assert datos["monto_a_pagar"]["value"] == "•••"
    assert [m["name"] for m in plan["missing"]] == ["plan_code"], plan["missing"]
    pedido = tareas["pack_commerce.tomar_pedido"]
    assert pedido["status"] == "escalated" and pedido["last_action"]["ok"] is False, pedido
    versiones = dict(await _sql(p["tid"], "select procedure_id, procedure_version from conversation_tasks where conversation_id = :c", c=conv))
    assert versiones["pack_subscriptions.contratar_plan"] == "2.0.0"
    esc = await app_client.get("/api/v1/admin/escalations", headers=p["panel"])
    assert conv in esc.text, esc.text


async def test_calidad_con_muestra(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    await _crear(app_client, p, "calidad-con-muestra")
    r = await app_client.get("/api/v1/admin/reliability", headers=p["panel"])
    assert r.status_code == 200, r.text
    d = r.json()
    op = d["operational"]
    assert op["turns"] >= 50 and op["escalation_rate"] is not None and op["p95_ms"] is not None, op
    assert sum(1 for a in op["by_agent"] if a["escalation_rate"] is not None) >= 2, op["by_agent"]
    assert any(c["rate"] is not None for c in d["completion"]), d["completion"]
    assert d["verifier"] and all(v["rejections"] > 0 for v in d["verifier"]), d["verifier"]
    assert any(c["pass_k"] is not None for x in d["consistency"] for c in x["procedures"]), d["consistency"]
    v = await app_client.get("/api/v1/admin/reliability/verifier", headers=p["panel"])
    assert v.status_code == 200 and v.json()["items"], v.text


async def test_version_candidata_y_canary(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    cuerpo = await _crear(app_client, p, "version-candidata")
    cid = _id_de_ruta(cuerpo, "/conexiones/")
    r = await app_client.get(f"/api/v1/admin/connections/{cid}/releases", headers=p["panel"])
    assert r.status_code == 200, r.text
    lista = r.json()
    activa = next(x for x in lista if x["active"])
    candidata = max(lista, key=lambda x: x["number"])
    assert candidata["number"] > activa["number"] and not candidata["active"]
    diff = await app_client.get(f"/api/v1/admin/connections/{cid}/releases/{candidata['id']}/diff", headers=p["panel"])
    assert diff.status_code == 200 and "assistant_name" in diff.json()["otros"]["ajustes"], diff.text

    await _crear(app_client, p, "canary-en-curso")
    estado = await app_client.get(f"/api/v1/admin/connections/{cid}/canary", headers=p["panel"])
    assert estado.status_code == 200, estado.text
    c = estado.json()
    assert c["percent"] == 20 and c["veredicto"] == "sin_muestra", c
    assert c["estable"]["conversaciones"] >= 12 and c["canary"]["conversaciones"] >= 4, c
    assert c["senal_dura"]["activa"] is False
    # Otra prueba con la primera en curso: 409. «Preparar mis datos» publica la versión en prueba
    # (HU-QA-09) y el Agent OS sigue listo.
    r = await app_client.post(f"{BASE}/canary-en-curso", headers=p["headers"])
    assert r.status_code == 409 and "detenla" in r.text
    r = await app_client.post("/api/v1/admin/qa/datos", headers=p["headers"])
    assert r.status_code == 200 and r.json()["agent_os"]["estado"] == "listo", r.text
    assert (await app_client.get(f"/api/v1/admin/connections/{cid}/canary", headers=p["panel"])).json() is None
    activa = await _uno(p["tid"], "select active_release_id from channel_connections where id = :c", c=uuid.UUID(cid))
    assert str(activa) == c["release_id"]
    # Con la prueba publicada, se puede crear otra.
    await _crear(app_client, p, "canary-en-curso")


async def test_clientes_y_pedidos(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    cuerpo = await _crear(app_client, p, "clientes-y-pedidos")
    sufijo = cuerpo["creado"]["sufijo"]
    tid = p["tid"]
    marca = f"%:qa-{sufijo.lower()}-%"
    assert await _uno(tid, "select count(*) from customers where tenant_id = :t and external_id like :m", t=tid, m=marca) == 6
    assert await _uno(
        tid, "select count(*) from customers where tenant_id = :t and external_id like :m and channel = 'whatsapp' "
        "and contact_phone is null",
        t=tid, m=marca,
    ) == 3
    # Ninguna conversación de WhatsApp: ningún aviso al cliente sale por ahí.
    assert await _uno(tid, "select count(*) from conversations where tenant_id = :t and channel = 'whatsapp'", t=tid) == 0
    pedidos = (await app_client.get("/api/v1/pack/commerce/orders", headers=p["panel"])).json()
    estados = {(o["status"], o["payment_status"]) for o in (pedidos["items"] if isinstance(pedidos, dict) else pedidos)}
    assert {("abierto", "pendiente"), ("entregado", "pagado"), ("cancelado", "pendiente")} <= estados, estados
    promos = (await app_client.get("/api/v1/pack/commerce/promotions", headers=p["panel"])).json()
    assert promos, promos
    assert await _uno(tid, "select count(*) from customer_notes where tenant_id = :t", t=tid) >= 6


async def test_suscripciones(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    await _crear(app_client, p, "suscripciones")
    r = await app_client.get("/api/v1/pack/pack_subscriptions/suscripciones", headers=p["panel"])
    assert r.status_code == 200, r.text
    filas = r.json()["items"] if isinstance(r.json(), dict) else r.json()
    assert {"activa", "vencida"} <= {s["estado"] for s in filas}, filas
    tid = p["tid"]
    assert await _uno(tid, "select count(*) from pack_subscriptions_charges where tenant_id = :t and estado = 'pagado'", t=tid) >= 2
    assert await _uno(tid, "select count(*) from pack_subscriptions_charges where tenant_id = :t and estado = 'pendiente'", t=tid) >= 2


async def test_notificaciones(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    dueno = await _dueno(app_client, p)
    await _crear(app_client, p, "notificaciones")
    r = await app_client.get("/api/v1/me/notifications", headers=dueno)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {"conversaciones", "ventas", "cobros", "agenda", "sistema"} <= {n["category"] for n in items}, items


async def test_mensajes_fallidos(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    cuerpo = await _crear(app_client, p, "mensajes-fallidos")
    eventos = (await app_client.get("/api/v1/admin/domain-events", headers=p["panel"])).json()["entries"]
    assert any(e["topic"] == "qa.escenario_de_prueba" and e["attempts"] == 5 for e in eventos), eventos
    dlq = (await app_client.get("/api/v1/admin/dlq", headers=p["panel"])).json()["entries"]
    assert any(e["external_message_id"].startswith("web:qa-dlq-") for e in dlq), (cuerpo, dlq)
    # El cliente del mensaje existe con el agente en pausa: reintentar no abre un turno.
    sufijo = cuerpo["creado"]["sufijo"].lower()
    assert await _uno(
        p["tid"], "select bot_paused from customers where tenant_id = :t and external_id = :e", t=p["tid"], e=f"web:qa-dlq-{sufijo}"
    ) is True


async def test_cobranza_completa(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    await _crear(app_client, p, "cobranza-completa")
    alertas = (await app_client.get("/api/v1/pack/cobranza/alerts", headers=p["panel"])).json()
    assert "denegado" in str(alertas), alertas
    dispositivos = (await app_client.get("/api/v1/pack/cobranza/devices", headers=p["panel"])).json()
    assert "pendiente" in str(dispositivos), dispositivos
    tid = p["tid"]
    for tabla in ("pack_cobranza_promesas", "pack_cobranza_cobros_propietario", "pack_cobranza_liquidaciones"):
        assert await _uno(tid, f"select count(*) from {tabla} where tenant_id = :t", t=tid) >= 1, tabla
    assert await _uno(tid, "select count(*) from pack_cobranza_reglas_negociacion where tenant_id = :t", t=tid) == 1


async def _dueno(app_client: Any, p: dict[str, Any]) -> dict[str, str]:
    """Headers del dueño de prueba (sus contraseñas se piden de nuevo: no se guardan)."""
    r = await app_client.post("/api/v1/admin/qa/datos/contrasenas", headers=p["headers"])
    assert r.status_code == 200, r.text
    datos = r.json()
    correo = next(c["email"] for c in datos["cuentas"] if c["rol"] == "dueno")
    return await api_headers(app_client, p["tid"], email=correo, password=datos["contrasenas"]["dueno"])


# ---------------------------------------------------------------- repetir y aislamiento
async def test_repetir_crea_otro_caso_sin_borrar_y_no_toca_otro_negocio(app_client: Any, preparado: dict[str, Any]) -> None:
    p = preparado
    tid, otro = p["tid"], p["otro"]

    async def foto(negocio: uuid.UUID) -> tuple[int, ...]:
        return tuple(
            [
                await _uno(negocio, f"select count(*) from {t} where tenant_id = :t", t=negocio)
                for t in ("customers", "conversations", "core_decisions", "notifications", "pack_commerce_orders")
            ]
        )

    antes_otro = await foto(otro)
    primero = await _crear(app_client, p, "notificaciones")
    medio = await foto(tid)
    segundo = await _crear(app_client, p, "notificaciones")
    despues = await foto(tid)
    assert primero["creado"]["sufijo"] != segundo["creado"]["sufijo"]
    assert despues[3] == medio[3] + 5 and despues[0] == medio[0] + 1  # 5 avisos y un cliente más
    await _crear(app_client, p, "conversacion-con-turnos")
    await _crear(app_client, p, "conversacion-con-turnos")
    await _crear(app_client, p, "clientes-y-pedidos")
    assert await foto(otro) == antes_otro
    lista = (await app_client.get(BASE, headers=p["headers"])).json()["escenarios"]
    ultimo = next(e for e in lista if e["slug"] == "notificaciones")["ultimo"]
    assert ultimo["sufijo"] == segundo["creado"]["sufijo"]
    # Lo creado queda registrado en `qa_events` del tester.
    n = await _uno(None, "select count(*) from qa_events where campo = 'datos.escenario' and item_slug = 'datos-escenario-notificaciones'")
    assert n >= 2


async def test_solo_en_el_negocio_qa_del_tester(app_client: Any, preparado: dict[str, Any]) -> None:
    """Si el negocio deja de llamarse `qa-…` (o el slug no es el del tester), no se toca."""
    from core.db.session import get_session

    p = preparado
    async with get_session() as session:
        slug = (await session.execute(text("select slug from tenants where id = :t"), {"t": p["tid"]})).scalar_one()
        await session.execute(text("update tenants set slug = :s where id = :t"), {"s": f"x{slug}", "t": p["tid"]})
    try:
        antes = await _uno(p["tid"], "select count(*) from notifications where tenant_id = :t", t=p["tid"])
        r = await app_client.post(f"{BASE}/notificaciones", headers=p["headers"])
        assert r.status_code == 409, r.text
        assert await _uno(p["tid"], "select count(*) from notifications where tenant_id = :t", t=p["tid"]) == antes
    finally:
        async with get_session() as session:
            await session.execute(text("update tenants set slug = :s where id = :t"), {"s": slug, "t": p["tid"]})
