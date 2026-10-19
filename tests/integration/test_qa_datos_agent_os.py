"""HU-QA-09 · «Preparar mis datos» deja el negocio de prueba del tester listo para el Agent OS.

Con `QA_TESTERS_ENABLED` y `AOS_CONNECTION_ROUTING` (monkeypatch), sin LLM real:

- **Negocio nuevo:** chat web activo con clave y origen de la landing, `aos_runtime = v3`,
  `aos_procedures`, `aos_routing = active`, General + Ventas + Agenda + Cobranza en el chat web
  con las reglas `preset_*` del Coordinador y la release activa; la respuesta trae la clave, el
  enlace y los agentes. Volver a pulsar responde 200 sin contraseñas y no duplica nada.
- **Negocio legacy existente** (preparado con el Agent OS apagado, como los `qa-` de test):
  queda en legacy con el motivo; al volver a pulsar se completa sin duplicar.
- **Fallo a medias:** si la promoción falla después del preset, el reintento completa y las
  reglas del Coordinador siguen (no se vuelve a legacy).
- **Aislamiento:** no toca otro negocio; un slug que no es del tester no recibe banderas.
- **Ruteo activo:** «quiero agendar una demo» por el chat web lo atiende Agenda (modelo guionado).
"""

from __future__ import annotations

import os
import uuid
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

PASSWORD = "clave-del-tester-aos-123"
LANDING = "https://landing-test.example.test"
DEMO = "quiero agendar una demo"


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
    """Packs registrados en `pack_versions` y activos en el proceso, y las tools del core."""
    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.tools.core_skill import register_core_skills

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()
    await plugin_manager.ensure_process_activation()
    register_core_skills(mcp_host)


@pytest.fixture
def entorno(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)
    monkeypatch.setattr(get_settings(), "aos_connection_routing", True)
    monkeypatch.setattr(get_settings(), "landing_base_url", LANDING)
    return monkeypatch


async def _tester(app_client: Any, t1: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"tester-aos-{uuid.uuid4().hex[:6]}@example.test"
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True))
    return await api_headers(app_client, t1, email=email, password=PASSWORD)


async def _banderas(tid: uuid.UUID) -> tuple[str, bool, str]:
    ((runtime, procedimientos, ruteo),) = await _sql(
        None, "select aos_runtime, aos_procedures, aos_routing from tenants where id = :t", t=tid
    )
    return str(runtime), bool(procedimientos), str(ruteo)


async def _foto(tid: uuid.UUID) -> dict[str, Any]:
    """Conteos que no deben crecer al volver a preparar."""
    consultas = {
        "conexiones": "select count(*) from channel_connections where tenant_id = :t and deleted_at is null",
        "agentes": "select count(*) from agents where tenant_id = :t and deleted_at is null",
        "versiones": "select count(*) from agent_versions where tenant_id = :t",
        "instancias": "select count(*) from agent_instances where tenant_id = :t and deleted_at is null",
        "releases": "select count(*) from connection_releases where tenant_id = :t",
        "productos": "select count(*) from commerce_products where tenant_id = :t",
    }
    foto: dict[str, Any] = {}
    for clave, sql in consultas.items():
        try:
            ((n,),) = await _sql(tid, sql, t=tid)
        except Exception:  # tabla de un pack con otro nombre: no se cuenta
            continue
        foto[clave] = n
    foto["banderas"] = await _banderas(tid)
    foto["activas"] = sorted(
        str(r) for (r,) in await _sql(tid, "select active_release_id from channel_connections where tenant_id = :t and deleted_at is null", t=tid)
    )
    return foto


async def _reglas(tid: uuid.UUID) -> list[str]:
    ((contenido,),) = await _sql(
        tid,
        "select v.content from agent_versions v join agents a on a.tenant_id = v.tenant_id and a.id = v.agent_id "
        "where a.tenant_id = :t and a.key = 'coordinador' order by v.version desc limit 1",
        t=tid,
    )
    return sorted(r["id"] for r in ((contenido.get("routing") or {}).get("rules") or []))


def _verificar_listo(datos: dict[str, Any]) -> None:
    aos = datos["agent_os"]
    assert aos["estado"] == "listo", aos
    assert aos["motivo"] is None
    chat = aos["chat_web"]
    assert chat["activo"] and chat["clave"] and chat["enlace"] == f"{LANDING}/w/{chat['clave']}"
    keys = {a["key"] for a in aos["agentes"]}
    assert {"general", "ventas", "agenda", "cobranza"} <= keys, keys
    assert [a["key"] for a in aos["agentes"] if a["por_defecto"]] == ["general"]
    # La pestaña Datos del panel lo muestra en `contenido`, sin cambios en el panel.
    seccion = next(c for c in datos["contenido"] if c["habilidad"] == "Agent OS")
    assert any(chat["enlace"] in d for d in seccion["detalle"])


async def _verificar_negocio(tid: uuid.UUID, clave: str) -> None:
    assert await _banderas(tid) == ("v3", True, "active")
    ((cid, activa, origenes),) = await _sql(
        tid,
        "select id, active, settings->'allowed_origins' from channel_connections "
        "where tenant_id = :t and type_id = 'webchat' and deleted_at is null and external_id = :k",
        t=tid, k=clave,
    )
    assert activa and LANDING in origenes
    assert {"preset_agenda_palabras", "preset_ventas_palabras", "preset_cobranza_palabras"} <= set(await _reglas(tid))
    # Todas las conexiones conversacionales con release activa al día (sin candidatas pendientes).
    filas = await _sql(
        tid,
        "select cc.active_release_id, (select max(r.number) from connection_releases r where r.tenant_id = cc.tenant_id "
        "and r.connection_id = cc.id), (select r.number from connection_releases r where r.id = cc.active_release_id) "
        "from channel_connections cc join connection_types ct on ct.id = cc.type_id "
        "where cc.tenant_id = :t and cc.deleted_at is null and ct.produces_conversations",
        t=tid,
    )
    assert filas and all(activa is not None and ultima == numero for activa, ultima, numero in filas), filas
    del cid


# ---------------------------------------------------------------- negocio nuevo
async def test_negocio_nuevo_queda_listo_para_el_agent_os(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    t1, _t2 = tenant_setup
    headers = await _tester(app_client, t1)

    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    assert datos["estado"] == "listo" and set(datos["contrasenas"]) == {"dueno", "gestor", "colaborador"}
    _verificar_listo(datos)
    tid = uuid.UUID(datos["negocio"]["id"])
    clave = datos["agent_os"]["chat_web"]["clave"]
    await _verificar_negocio(tid, clave)

    # La lectura trae lo mismo (sin contraseñas).
    r = await app_client.get("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200
    _verificar_listo(r.json()["datos"])

    # Volver a pulsar: 200, sin contraseñas, misma clave y nada duplicado.
    antes = await _foto(tid)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["contrasenas"] is None
    _verificar_listo(r.json())
    assert r.json()["agent_os"]["chat_web"]["clave"] == clave
    assert await _foto(tid) == antes


# ---------------------------------------------------------------- negocio legacy existente
async def test_re_preparar_un_negocio_legacy_existente(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    from core.config import get_settings

    t1, _t2 = tenant_setup
    headers = await _tester(app_client, t1)

    # Como los `qa-` de test: preparados antes, con el Agent OS apagado.
    entorno.setattr(get_settings(), "aos_connection_routing", False)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    tid = uuid.UUID(datos["negocio"]["id"])
    assert datos["estado"] == "listo" and datos["error"] is None
    assert datos["agent_os"]["estado"] == "error" and "AOS_CONNECTION_ROUTING" in datos["agent_os"]["motivo"]
    assert await _banderas(tid) == ("legacy", False, "off")
    assert (await _sql(tid, "select count(*) from agents where tenant_id = :t", t=tid))[0][0] == 0

    # Se enciende la variable y el tester vuelve a pulsar: se completa.
    entorno.setattr(get_settings(), "aos_connection_routing", True)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    _verificar_listo(r.json())
    await _verificar_negocio(tid, r.json()["agent_os"]["chat_web"]["clave"])

    antes = await _foto(tid)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    assert await _foto(tid) == antes


async def test_un_fallo_a_medias_se_reintenta_sin_perder_las_reglas(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    from core.qa import datos_aos

    t1, _t2 = tenant_setup
    headers = await _tester(app_client, t1)

    async def _falla(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("promoción caída")

    original = datos_aos._activar_releases
    entorno.setattr(datos_aos, "_activar_releases", _falla)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    tid = uuid.UUID(datos["negocio"]["id"])
    assert datos["agent_os"]["estado"] == "error" and "promoción caída" in datos["agent_os"]["motivo"]
    # Ya en v3 (funcional con el General); no se apagó a legacy ni se encendió el ruteo.
    assert await _banderas(tid) == ("v3", False, "off")
    assert "preset_agenda_palabras" in await _reglas(tid)

    entorno.setattr(datos_aos, "_activar_releases", original)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 200, r.text
    _verificar_listo(r.json())
    await _verificar_negocio(tid, r.json()["agent_os"]["chat_web"]["clave"])


# ---------------------------------------------------------------- aislamiento
async def test_no_toca_otro_negocio(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    from core.qa import datos_aos

    t1, t2 = tenant_setup
    fotos = {t: await _foto(t) for t in (t1, t2)}
    chats = {
        t: await _sql(t, "select web_chat_config from app_settings where tenant_id = :t and deleted_at is null", t=t)
        for t in (t1, t2)
    }

    headers = await _tester(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    _verificar_listo(r.json())

    for t in (t1, t2):
        assert await _foto(t) == fotos[t]
        assert await _sql(t, "select web_chat_config from app_settings where tenant_id = :t and deleted_at is null", t=t) == chats[t]

    # Un id o un slug que no son del negocio de prueba: ni chat web, ni backfills, ni banderas.
    ((slug_t1,),) = await _sql(None, "select slug from tenants where id = :t", t=t1)
    for tid, slug in ((t1, slug_t1), (t1, f"qa-{uuid.uuid4().hex[:12]}"), (t2, r.json()["negocio"]["slug"])):
        motivo = await datos_aos.preparar(tid, slug)
        assert motivo is not None and "no es el negocio de prueba" in motivo
    for t in (t1, t2):
        assert await _foto(t) == fotos[t]


async def test_sin_la_variable_de_testers_no_hace_nada(tenant_setup: Any, entorno: Any) -> None:
    from core.config import get_settings
    from core.qa import datos_aos

    t1, _t2 = tenant_setup
    antes = await _foto(t1)
    entorno.setattr(get_settings(), "qa_testers_enabled", False)
    motivo = await datos_aos.preparar(t1, "qa-000000000000")
    assert motivo is not None and "QA_TESTERS_ENABLED" in motivo
    assert await _foto(t1) == antes


# ---------------------------------------------------------------- ruteo activo
class _Guion:
    """Modelo guionado: responde un texto y guarda las tools que vio."""

    pricing_per_mtok = (0.0, 0.0)
    model = "deepseek-flash"
    provider_name = "deepseek"

    def __init__(self) -> None:
        self.tools: list[list[str]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMUsageInfo

        nombres = [str(t.get("name") if isinstance(t, dict) else getattr(t, "name", "")) for t in kwargs.get("tools") or []]
        self.tools.append(sorted(nombres))
        return LLMResponse(
            content="¿Para qué día quieres la demo?", stop_reason="end_turn", model=self.model,
            usage=LLMUsageInfo(input_tokens=5, output_tokens=5),
        )


async def test_ruteo_activo_agendar_una_demo_va_a_agenda(app_client: Any, tenant_setup: Any, entorno: Any) -> None:
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    headers = await _tester(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/datos", headers=headers)
    assert r.status_code == 201, r.text
    datos = r.json()
    tid = uuid.UUID(datos["negocio"]["id"])
    clave = datos["agent_os"]["chat_web"]["clave"]

    ((cid,),) = await _sql(
        tid, "select id from channel_connections where tenant_id = :t and type_id = 'webchat' and external_id = :k",
        t=tid, k=clave,
    )
    ((agenda,),) = await _sql(
        tid,
        "select i.id from agent_instances i join agents a on a.tenant_id = i.tenant_id and a.id = i.agent_id "
        "where i.tenant_id = :t and i.connection_id = :c and a.key = 'agenda' and i.deleted_at is null",
        t=tid, c=cid,
    )
    cliente, conv = uuid.uuid4(), uuid.uuid4()
    await _sql(
        tid, "insert into customers (id, tenant_id, channel, external_id) values (:id, :t, 'web', :e)",
        id=cliente, t=tid, e=f"visitante-{uuid.uuid4().hex[:10]}",
    )
    await _sql(
        tid,
        "insert into conversations (id, tenant_id, customer_id, channel, connection_id) values (:id, :t, :cli, 'web', :c)",
        id=conv, t=tid, cli=cliente, c=cid,
    )
    await _sql(
        tid,
        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content) "
        "values (:id, :t, :c, 'inbound', 'customer', :m)",
        id=uuid.uuid4(), t=tid, c=conv, m=DEMO,
    )
    guion = _Guion()
    entorno.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(tid):
        await process_conversation(conv, DEMO)

    ((fuente, instancia, regla),) = await _sql(
        tid,
        "select route_source, active_instance_id, rule_id from core_decisions where conversation_id = :c "
        "order by created_at desc limit 1",
        c=conv,
    )
    assert fuente in ("trigger", "rule"), fuente
    assert instancia == agenda
    if fuente == "rule":
        assert regla == "preset_agenda_palabras"
    ((dueno,),) = await _sql(tid, "select active_agent_instance_id from conversations where id = :c", c=conv)
    assert dueno == agenda
