"""HU-AOS-97 · guardrails y tools declarativas por agente en el editor (D-78 decisión 4, ADR-0206 §1-2).

Negocio con `pack_commerce` y `pack_calendar`, una conexión y su release 1. Gherkin:

- «Guardrails del agente»: la regla que el dueño agrega a Ventas viaja en la versión publicada y en la release, el
  turno la aplica con su acción y Agenda (otro agente del negocio) no la tiene.
- «Tool declarativa con secreto»: el secreto se guarda cifrado en la instalación `core`, atado a los dominios de la
  tool; la tool queda con `{"secret": ...}` y al ejecutarla el valor viaja solo a su dominio.
- «Sin secretos literales»: un literal en un header sensible o un dominio interno dan 422 por campo y no se guarda
  nada (ni el secreto que venía en la misma llamada).
- «Secreto en URL o body»: se guarda con el aviso `omitted` y el turno no la ofrece.
- «Coordinador inmutable»: 409 `coordinador_inmutable`.
- «Aislamiento»: otro negocio no ve el agente ni los secretos, ni puede resolverlos.

Sin LLM ni red (la tool se ejecuta contra un HTTP espía).
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
DOMINIO = "api.transportadora.co"
VALOR = "Bearer valor-real-del-negocio-97"
REGLA = {"name": "sin_descuentos", "kind": "forbidden_phrase", "phrases": ["descuento"], "action": "escalate"}


def _tool(**cambios: Any) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": "consultar_envio",
        "description": "Estado de una guía de la transportadora",
        "url": f"https://{DOMINIO}/v1/guias/{{guia}}",
        "allowedDomains": [DOMINIO],
        "headers": {"Authorization": {"secret": "token_envios"}, "X-Cliente": "demo"},
        "parameters": {"type": "object", "properties": {"guia": {"type": "string"}}},
    }
    tool.update(cambios)
    return tool


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


async def _negocio() -> tuple[uuid.UUID, uuid.UUID]:
    from _habilidades import onboard_tenant
    from release_de_prueba import release_inicial

    slug = f"aos97-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS97 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan="premium")
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

    email = f"{uniq('dueno97')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _agente(app_client, h: dict[str, str], plantilla: str) -> str:
    r = await app_client.post(f"/api/v1/admin/agent-templates/{plantilla}/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    return r.json()["agent"]["id"]


async def _publicar(app_client, h: dict[str, str], aid: str, cid: uuid.UUID) -> str:
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert r.status_code == 200, r.text
    [conexion] = r.json()["connections"]
    assert conexion["error"] is None, conexion
    return conexion["release_id"]


async def _turno(tid: uuid.UUID, release_id: str, aid: str) -> Any:
    """El agente del turno de `aid` armado desde la release congelada, como lo arma el loop."""
    from core.agents.turno import agente_del_turno
    from core.db.session import get_session
    from core.releases.cargar import cargar_release
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            release = await cargar_release(session, tid, uuid.UUID(release_id))
    assert release is not None
    foco = next(c for c in release.componentes if str(c.agent_id) == aid)
    kernel = next(c for c in release.componentes if c.role == "core")
    return agente_del_turno(foco, kernel, skills=release.skills, tenant_plugins=release.tenant_plugins)


async def _lock(app_client, h: dict[str, str], aid: str) -> int:
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/declarative-tools", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["lock_version"]


class _HttpEspia:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, *, json_body: Any = None, headers: Any = None) -> dict[str, Any]:
        self.llamadas.append({"method": method, "url": url, "headers": dict(headers or {})})
        return {"ok": True}


async def test_reglas_y_tools_del_agente_de_punta_a_punta(app_client) -> None:
    from types import SimpleNamespace

    from core.agents.secretos import resolver_secretos, secretos_configurados
    from core.mcp.declarative import tools_for_spec
    from core.orchestrator.guardrails import run_guardrails
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio()
    h = await _dueno(app_client, tid)
    ventas = await _agente(app_client, h, "ventas")
    agenda = await _agente(app_client, h, "agenda")

    # ---- Guardrails del agente.
    r = await app_client.get(f"/api/v1/admin/agents/{ventas}/guardrails", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["guardrails"] == []
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/guardrails",
        headers=h,
        json={"guardrails": [REGLA], "lock_version": r.json()["lock_version"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["guardrails"] == [{**REGLA, "tool": None, "value": None}]
    # Una regla mal formada o una tool obligatoria que no existe: error por campo.
    r2 = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/guardrails",
        headers=h,
        json={
            "guardrails": [{"name": "x", "kind": "required_tool", "phrases": ["envío"], "tool": "no_existe"}],
            "lock_version": r.json()["lock_version"],
        },
    )
    assert r2.status_code == 422, r2.text
    assert "guardrails[0].tool" in r2.json()["error"]["fields"]

    # ---- Sin secretos literales: nada se guarda, tampoco el secreto que venía en la misma llamada.
    lock = await _lock(app_client, h, ventas)
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/declarative-tools",
        headers=h,
        json={
            "tools": [_tool(), _tool(name="literal", headers={"Authorization": "Bearer literal"})],
            "secrets": {"token_envios": VALOR},
            "lock_version": lock,
        },
    )
    assert r.status_code == 422, r.text
    assert "declarativeTools[1].headers" in r.json()["error"]["fields"]
    assert VALOR not in r.text and "Bearer literal" not in r.text
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/declarative-tools",
        headers=h,
        json={"tools": [_tool(allowedDomains=["localhost"])], "secrets": {"token_envios": VALOR}, "lock_version": lock},
    )
    assert r.status_code == 422, r.text
    assert "declarativeTools[0].allowedDomains[0]" in r.json()["error"]["fields"]
    async with tenant_scope(tid):
        assert await secretos_configurados(tid) == {}
    assert await _lock(app_client, h, ventas) == lock

    # ---- Tool declarativa con secreto (+ una con el secreto en la URL: se guarda con aviso).
    por_url = _tool(name="por_url", url=f"https://{DOMINIO}/v1?token={{secret:token_envios}}", headers={})
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/declarative-tools",
        headers=h,
        json={"tools": [_tool(), por_url], "secrets": {"token_envios": VALOR}, "lock_version": lock},
    )
    assert r.status_code == 200, r.text
    salida = r.json()
    assert VALOR not in r.text
    assert salida["tools"][0]["headers"] == {"Authorization": {"secret": "token_envios"}, "X-Cliente": "demo"}
    assert salida["secrets"] == [{"name": "token_envios", "domains": [DOMINIO]}]
    assert salida["omitted"] == ["por_url"]
    fila = await _sql(
        tid,
        "select config_encrypted from pack_installations where tenant_id = :t and pack_id = 'core' "
        "and scope = 'tenant' and deleted_at is null",
        t=tid,
    )
    assert fila and fila[0][0].startswith("gcm:v1:") and VALOR not in fila[0][0]

    # Coordinador inmutable (ADR-0205 §5).
    [(coordinador,)] = await _sql(tid, "select id from agents where tenant_id = :t and kind = 'core'", t=tid)
    for ruta, cuerpo in (
        ("guardrails", {"guardrails": [REGLA], "lock_version": 1}),
        ("declarative-tools", {"tools": [_tool()], "lock_version": 1}),
    ):
        r = await app_client.put(f"/api/v1/admin/agents/{coordinador}/{ruta}", headers=h, json=cuerpo)
        assert (r.status_code, r.json()["error"]["code"]) == (409, "coordinador_inmutable"), r.text

    # ---- Publicar: la versión y la release llevan la regla y las tools; el turno las aplica.
    release = await _publicar(app_client, h, ventas, cid)
    turno = await _turno(tid, release, ventas)
    assert turno.guardrails == [REGLA]
    veredicto = await run_guardrails("Te doy un descuento del 20 %", [], custom_rules=turno.guardrails)
    assert not veredicto.passed and veredicto.reason == "regla:sin_descuentos"
    assert (await run_guardrails("El pedido sale hoy", [], custom_rules=turno.guardrails)).passed
    # La del secreto en la URL no se ofrece en el turno.
    tools = {t.name: t for t in tools_for_spec(turno)}
    assert set(tools) == {"consultar_envio"}
    espia = _HttpEspia()
    async with tenant_scope(tid):
        assert await tools["consultar_envio"].handler({"guia": "123"}, SimpleNamespace(http=espia)) == {"ok": True}
        assert espia.llamadas[-1]["headers"] == {"Authorization": VALOR, "X-Cliente": "demo"}
        # El secreto solo viaja a sus dominios.
        assert await resolver_secretos(tid, ["token_envios"], "evil.example.com") == {}

    # Los demás agentes del negocio no tienen la regla ni las tools.
    release_agenda = await _publicar(app_client, h, agenda, cid)
    turno_agenda = await _turno(tid, release_agenda, agenda)
    assert turno_agenda.guardrails == [] and turno_agenda.declarativas == []

    # Otro agente puede reutilizar el secreto sin escribirlo; apuntarlo a otro dominio sin el valor, no.
    lock_agenda = await _lock(app_client, h, agenda)
    r = await app_client.put(
        f"/api/v1/admin/agents/{agenda}/declarative-tools",
        headers=h,
        json={
            "tools": [_tool(name="exfiltrar", url="https://evil.example.com/x", allowedDomains=["evil.example.com"])],
            "lock_version": lock_agenda,
        },
    )
    assert r.status_code == 422, r.text
    assert "declarativeTools[0].allowedDomains" in r.json()["error"]["fields"]
    r = await app_client.put(
        f"/api/v1/admin/agents/{agenda}/declarative-tools",
        headers=h,
        json={"tools": [_tool(name="envio_agenda")], "lock_version": lock_agenda},
    )
    assert r.status_code == 200, r.text
    async with tenant_scope(tid):
        assert await secretos_configurados(tid) == {"token_envios": [DOMINIO]}

    # ---- Aislamiento: otro negocio no ve el agente, ni los secretos, ni puede resolverlos.
    otro, _ = await _negocio()
    h_otro = await _dueno(app_client, otro)
    for ruta in ("guardrails", "declarative-tools"):
        r = await app_client.get(f"/api/v1/admin/agents/{ventas}/{ruta}", headers=h_otro)
        assert r.status_code == 404, r.text
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/guardrails", headers=h_otro, json={"guardrails": [], "lock_version": 1}
    )
    assert r.status_code == 404, r.text
    # La ruta que escribe secretos antes del borrador da 404 sin escribir nada en ningún negocio.
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas}/declarative-tools",
        headers=h_otro,
        json={"tools": [_tool()], "secrets": {"token_envios": "otro-valor"}, "lock_version": 1},
    )
    assert r.status_code == 404, r.text
    async with tenant_scope(otro):
        assert await secretos_configurados(otro) == {}
    ventas_otro = await _agente(app_client, h_otro, "ventas")
    r = await app_client.get(f"/api/v1/admin/agents/{ventas_otro}/declarative-tools", headers=h_otro)
    assert r.status_code == 200 and r.json()["secrets"] == [], r.text
    # Referenciar el nombre del secreto de A no lo resuelve en B.
    r = await app_client.put(
        f"/api/v1/admin/agents/{ventas_otro}/declarative-tools",
        headers=h_otro,
        json={"tools": [_tool()], "lock_version": r.json()["lock_version"]},
    )
    assert r.status_code == 422, r.text
    assert "declarativeTools[0].headers.Authorization" in r.json()["error"]["fields"]
    async with tenant_scope(otro):
        assert await resolver_secretos(otro, ["token_envios"], DOMINIO) == {}
