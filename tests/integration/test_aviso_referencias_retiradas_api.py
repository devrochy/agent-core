"""HU-AOS-59 · API de solo lectura del aviso de referencias retiradas, contra el stack real (ADR-0184).

Reproduce el incidente del 2026-11-03 con DOS negocios: A tiene un agente cuya versión publicada y cuya
release activa nombran `informar_plan` y `pack_commerce.contratar_plan` (retirados por SPEC-43 PR-4); B
está limpio. Gherkin:

- **El master ve lo mismo que el CLI.** `GET /admin/platform/tenants/{id}/retired-references` trae agentes,
  versiones, rutas, códigos y releases activas; solo el master (ni el dueño ni el tester).
- **El dueño ve solo lo suyo, en palabras simples.** `GET /admin/agents/retired-references` y
  `GET /admin/agents/{id}/retired-references`: sin códigos internos; el agente de otro negocio es 404.
- **Publicar con lo retirado da un error claro.** Mismo 422 `invalid_spec` de siempre, con el texto simple
  por campo, y sin publicar nada (ni un procedimiento retirado, que antes pasaba y fallaba en el build).
- **Solo lectura.** Ninguna de las rutas de aviso escribe: ni versiones, ni releases, ni auditoría.

Sin LLM ni red.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text
from test_referencias_retiradas import (  # los helpers del incidente de #676
    ACCION,
    VIEJO,
    _modules,
    _negocio,
    _negocio_del_incidente,
    _sql,
)

pytestmark = [
    pytest.mark.aislamiento,  # dos negocios: ni el informe ni el aviso cruzan de uno a otro
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"
CODIGOS = ("accion_inexistente", "procedimiento_inexistente", "referencia_retirada")


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs

    reporte = await sync_packs(_modules())
    assert not reporte.fallo, reporte
    await plugin_manager.sync_registry()
    await plugin_manager.activate_all_configured()


# ---------------------------------------------------------------- helpers
async def _dueno(app_client: Any, tid: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno59')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _tester(app_client: Any, tid: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    email = f"{uniq('tester59')}@example.test"
    async with get_session() as session:
        session.add(Admin(email=email, password_hash=hash_password(PASSWORD), role="tester", active=True))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _conteos(tid: uuid.UUID) -> tuple[Any, ...]:
    (fila,) = await _sql(
        tid,
        "select (select count(*) from agent_versions where tenant_id = :t), "
        "(select count(*) from connection_releases where tenant_id = :t), "
        "(select count(*) from agent_drafts where tenant_id = :t), "
        "(select count(*) from audit_log where tenant_id = :t)",
        t=tid,
    )
    return tuple(fila)


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


async def _borrador_con_lo_retirado(tid: uuid.UUID, aid: uuid.UUID) -> None:
    """El borrador del dueño, escrito ANTES del retiro: una acción y un procedimiento que ya no existen.
    Se escribe directo: `guardar_borrador` ya no dejaría guardar la acción."""
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        contenido = dict((await versiones.obtener_borrador(tid, aid)).content)
    contenido["tools"] = [*contenido.get("tools", []), {"tool": ACCION, "enabled": True, "scopes": []}]
    contenido["procedures"] = [*contenido.get("procedures", []), {"id": VIEJO, "version": "^1.0"}]
    await _sql(
        tid,
        "update agent_drafts set content = cast(:c as jsonb) where tenant_id = :t and agent_id = :a",
        c=json.dumps(contenido),
        t=tid,
        a=aid,
    )


def _fila(informe: dict[str, Any], scope: str, name: str) -> dict[str, Any]:
    (fila,) = [f for f in informe["rows"] if f["scope"] == scope and f["name"] == name]
    return fila


# ---------------------------------------------------------------- master
async def test_master_ve_el_mismo_informe_que_el_cli(app_client) -> None:
    from core.releases.referencias_retiradas import auditar

    a, slug_a, cid, _aid, version = await _negocio_del_incidente()
    b, slug_b, _ = await _negocio()
    master = await api_headers(app_client, a)

    r = await app_client.get(f"/api/v1/admin/platform/tenants/{a}/retired-references", headers=master)
    assert r.status_code == 200, r.text
    informe = r.json()
    cli = (await auditar(tenant_ids=[a], modules=_modules())).negocios[0]

    assert (informe["tenant_id"], informe["slug"]) == (str(a), slug_a)
    assert informe["affected"] is True and informe["error"] is None
    assert informe["agents_affected"] == cli.agentes_afectados == 1
    assert informe["releases_affected"] == cli.releases_afectadas == 1
    assert informe["affected_connections"] == [{"connection_id": str(cid), "release": cli.conexiones_afectadas[cid]}]
    # Las mismas filas que el CLI (ambito, agente, versión, ruta, código, nombre, destino y dónde corre).
    assert [
        (f["scope"], f["agent"], f["version"], f["path"], f["code"], f["name"], f["destination"], tuple(f["where"]))
        for f in informe["rows"]
    ] == [(f.ambito, f.agente, f.version, f.ruta, f.codigo, f.nombre, f.destino, f.donde) for f in cli.filas]

    en_version = _fila(informe, "version", ACCION)
    assert en_version["agent"] == "general" and en_version["version"] == version.version
    assert en_version["code"] == "accion_inexistente" and en_version["kind"] == "action"
    assert en_version["path"].startswith("tools[") and en_version["destination"] == "quitar"
    assert en_version["where"] and "release #" in en_version["where"][0]
    proc = _fila(informe, "version", VIEJO)
    assert proc["code"] == "procedimiento_inexistente" and proc["kind"] == "procedure"
    assert proc["destination"].startswith("reemplazar por pack_subscriptions.contratar_plan")
    assert _fila(informe, "release", VIEJO)["where"]  # la release activa también lo congeló
    assert all(f["message"] and not any(c in f["message"] for c in CODIGOS) for f in informe["rows"])

    # B, limpio, sale limpio y sin nada de A.
    r = await app_client.get(f"/api/v1/admin/platform/tenants/{b}/retired-references", headers=master)
    assert r.status_code == 200, r.text
    limpio = r.json()
    assert (limpio["slug"], limpio["affected"], limpio["rows"], limpio["drafts"], limpio["aside"]) == (
        slug_b, False, [], [], [],
    )

    r = await app_client.get(f"/api/v1/admin/platform/tenants/{uuid.uuid4()}/retired-references", headers=master)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


async def test_solo_el_master_ve_el_informe_de_un_negocio(app_client, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", True)  # el tester puede iniciar sesión
    a, *_ = await _negocio_del_incidente()
    b, _, _ = await _negocio()
    dueno_a = await _dueno(app_client, a)
    tester = await _tester(app_client, a)

    url_a = f"/api/v1/admin/platform/tenants/{a}/retired-references"
    for h in (dueno_a, tester):
        assert (await app_client.get(url_a, headers=h)).status_code == 403
    # Un dueño tampoco lee el informe de OTRO negocio.
    assert (await app_client.get(f"/api/v1/admin/platform/tenants/{b}/retired-references", headers=dueno_a)).status_code == 403
    assert (await app_client.get(url_a)).status_code == 401


# ---------------------------------------------------------------- dueño
async def test_el_dueno_ve_solo_lo_suyo_en_palabras_simples(app_client) -> None:
    a, _, _, aid, version = await _negocio_del_incidente()
    b, _, _ = await _negocio()
    dueno_a = await _dueno(app_client, a)
    dueno_b = await _dueno(app_client, b)
    antes = await _conteos(a)

    r = await app_client.get("/api/v1/admin/agents/retired-references", headers=dueno_a)
    assert r.status_code == 200, r.text
    aviso = r.json()
    assert aviso["affected_agents"] == 1
    (agente,) = aviso["agents"]
    assert (agente["agent_id"], agente["key"]) == (str(aid), "general")
    assert agente["name"] and agente["published_version"] == version.version
    assert agente["in_use"] is True  # la release activa de la conexión lo congeló
    assert agente["blocks_publish"] is False  # el borrador está limpio: publicar no falla
    por_nombre = {n["name"]: n for n in agente["notices"]}
    assert set(por_nombre) == {ACCION, VIEJO}
    assert {n["scope"] for n in agente["notices"]} == {"published"}
    assert (por_nombre[ACCION]["kind"], por_nombre[VIEJO]["kind"]) == ("action", "procedure")
    assert por_nombre[ACCION]["field"].startswith("tools[")
    assert por_nombre[VIEJO]["field"].startswith("procedures[")
    assert "ya no existe" in por_nombre[ACCION]["message"]
    assert "pack_subscriptions.contratar_plan" in por_nombre[VIEJO]["message"]
    # Sin códigos internos en ninguna parte del cuerpo.
    assert not any(c in r.text for c in CODIGOS)

    # El mismo aviso, para un solo agente (editor y publicar).
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=dueno_a)
    assert r.status_code == 200, r.text
    assert r.json() == agente

    # B no ve nada de A: ni en la lista ni pidiendo el agente de A por id.
    r = await app_client.get("/api/v1/admin/agents/retired-references", headers=dueno_b)
    assert r.status_code == 200 and r.json() == {"affected_agents": 0, "agents": []}
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=dueno_b)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    # Ni con la sesión de B apuntando al negocio A.
    r = await app_client.get("/api/v1/admin/agents/retired-references", headers={**dueno_b, "X-Tenant-Id": str(a)})
    assert r.status_code == 403, r.text

    assert await _conteos(a) == antes  # solo lectura: ni versiones, ni releases, ni borradores, ni auditoría


async def test_un_agente_limpio_viene_sin_avisos(app_client) -> None:
    a, _, _, aid, _ = await _negocio_del_incidente()
    dueno = await _dueno(app_client, a)
    otros = [x for x in (await app_client.get("/api/v1/admin/agents", headers=dueno)).json() if x["id"] != str(aid)]
    assert otros
    r = await app_client.get(f"/api/v1/admin/agents/{otros[0]['id']}/retired-references", headers=dueno)
    assert r.status_code == 200, r.text
    assert (r.json()["notices"], r.json()["blocks_publish"]) == ([], False)


async def test_el_borrador_con_lo_retirado_bloquea_publicar_y_lo_que_corre_hoy_se_avisa(app_client) -> None:
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    a, _, _, aid, _ = await _negocio_del_incidente()
    dueno = await _dueno(app_client, a)
    await _borrador_con_lo_retirado(a, aid)

    r = await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=dueno)
    assert r.status_code == 200, r.text
    agente = r.json()
    assert agente["blocks_publish"] is True
    assert {(n["scope"], n["name"]) for n in agente["notices"]} >= {("draft", ACCION), ("draft", VIEJO)}
    assert all(not any(c in n["message"] for c in CODIGOS) for n in agente["notices"])

    # La última versión se corrige y se publica; la release activa sigue con la vieja: se avisa «en uso».
    async with tenant_scope(a):
        ultima, primera = (await versiones.listar_versiones(a, aid))[0], (await versiones.listar_versiones(a, aid))[-1]
        limpio = dict(primera.content)
        limpio["description"] = "Versión corregida por el dueño"
        from core.kernel.procedures import catalogo_de_acciones

        nueva, creada = await versiones.publicar(
            a, aid, catalogo=catalogo_de_acciones(_modules()), contenido=limpio, label="limpia", notes="sin lo retirado"
        )
    assert creada and nueva.version > ultima.version
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=dueno)
    agente = r.json()
    assert agente["published_version"] == nueva.version and agente["in_use"] is True
    # La versión nueva está limpia y el borrador aún nombra lo retirado: lo que corre no se repite.
    assert {n["scope"] for n in agente["notices"]} == {"draft"}
    # El dueño limpia también el borrador: queda solo lo que corre hoy.
    await _sql(
        a,
        "update agent_drafts set content = cast(:c as jsonb) where tenant_id = :t and agent_id = :a",
        c=json.dumps(limpio),
        t=a,
        a=aid,
    )
    agente = (await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=dueno)).json()
    assert agente["blocks_publish"] is False and agente["in_use"] is True
    assert {n["scope"] for n in agente["notices"]} == {"in_use"}
    en_uso = agente["notices"]
    assert {n["name"] for n in en_uso} == {ACCION, VIEJO}
    assert all("todavía lo usa" in n["message"] for n in en_uso)


# ---------------------------------------------------------------- publicar
async def test_publicar_con_lo_retirado_da_422_claro_y_no_publica_nada(app_client) -> None:
    a, _, _, aid, _ = await _negocio_del_incidente()
    master = await api_headers(app_client, a)  # el master opera el negocio (X-Tenant-Id)
    await _borrador_con_lo_retirado(a, aid)
    antes = await _conteos(a)

    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=master, json={"notes": "intento"})
    assert r.status_code == 422, r.text  # el mismo 422 `invalid_spec` de siempre
    error = r.json()["error"]
    assert error["code"] == "invalid_spec"
    assert "Este agente usa 2 cosas que ya no existen" in error["message"]
    assert set(error["fields"]) == {f["field"] for f in error["details"]["retired_references"]}
    assert {f["field"].split("[")[0] for f in error["details"]["retired_references"]} == {"tools", "procedures"}
    textos = " ".join(error["fields"].values())
    assert f"«{ACCION}» ya no existe" in textos and f"«{VIEJO}» ya no existe" in textos
    assert not any(c in r.text for c in CODIGOS)
    # Es el mismo texto que el aviso del editor.
    aviso = (await app_client.get(f"/api/v1/admin/agents/{aid}/retired-references", headers=master)).json()
    assert {n["message"] for n in aviso["notices"] if n["scope"] == "draft"} == set(error["fields"].values())
    assert await _conteos(a) == antes  # no se publicó nada


async def test_el_editor_tampoco_publica_lo_retirado_aunque_sea_un_procedimiento(app_client) -> None:
    """Antes, un procedimiento retirado pasaba el linter: la versión salía publicada (200) y el build de la
    release fallaba después, por conexión. Ahora es el mismo 422, antes de publicar."""
    from core.agents import versiones
    from core.shared.tenant import tenant_scope

    a, _, cid, _, _ = await _negocio_del_incidente()
    await _runtime_v3(a)
    master = await api_headers(app_client, a)
    async with tenant_scope(a):
        agente = await versiones.crear_agente(a, key="ventas59", kind="specialist", name="Ventas 59", description="", actor=None)
        contenido = dict((await versiones.obtener_borrador(a, agente.id)).content)
    contenido["procedures"] = [{"id": VIEJO, "version": "^1.0"}]  # solo el procedimiento: el linter no lo ve
    await _sql(
        a,
        "update agent_drafts set content = cast(:c as jsonb) where tenant_id = :t and agent_id = :a",
        c=json.dumps(contenido),
        t=a,
        a=agente.id,
    )
    antes = await _conteos(a)

    r = await app_client.post(
        f"/api/v1/admin/agents/{agente.id}/publish", headers=master, json={"connection_ids": [str(cid)]}
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_spec"
    # El negocio no tiene ni la habilidad: el editor suma sus propios problemas. Lo retirado sale igual,
    # con su texto, en `fields` y en `details` (el mismo que el aviso del editor).
    assert "ya no existe" in error["message"]
    (retirada,) = error["details"]["retired_references"]
    assert retirada["field"] == "procedures[0].id"
    assert f"«{VIEJO}» ya no existe" in retirada["message"] and "pack_subscriptions.contratar_plan" in retirada["message"]
    assert retirada["message"] in error["fields"]["procedures[0].id"]
    assert not any(c in r.text for c in CODIGOS)
    assert await _conteos(a) == antes  # ni versión, ni release, ni instancia


async def test_publicar_un_agente_limpio_sigue_funcionando(app_client) -> None:
    a, _, _, aid, _ = await _negocio_del_incidente()
    master = await api_headers(app_client, a)
    await _sql(a, "update agent_drafts set content = jsonb_set(content, '{description}', '\"limpio\"') where tenant_id = :t and agent_id = :a", t=a, a=aid)
    # El borrador nace de la v1 (sin lo retirado): publicar pasa por el chequeo sin tocarlo.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=master, json={"notes": "ok"})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
