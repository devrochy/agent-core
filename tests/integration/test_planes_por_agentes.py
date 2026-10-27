"""HU-BILL-05 · planes por agentes: agentes publicados, colaboradores y crédito (D-35, ADR-0142).

- Free (1 agente) con el General incorporado: el primer especialista entra; el segundo da 409
  `plan_limit_agentes`, por la API de la conexión y por el editor (antes de publicar la versión).
- Premium permite 5 agentes; el sexto da 409. Un agente que ya está en una conexión no ocupa otro cupo.
- Ni el General ni el Coordinador cuentan (enmienda del ADR-0142, 2026-10-27): el General viene
  incorporado y se puede volver a poner sin cupo.
- Colaboradores al tope (Free = 2): el tercero da 409 `plan_limit_colaboradores` sin crear la cuenta; la
  invitación pendiente ocupa cupo.
- Un negocio por encima del tope (plan bajado) sigue operando: edita, publica versiones de sus agentes,
  cambia instancias; solo no crece.
- Aislamiento: el tope y el conteo de un negocio no ven al otro.
- `GET /admin/plan` trae `agentes`, `colaboradores` y el crédito mensual.

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
    pytest.mark.aislamiento,  # topes por negocio y conteos entre negocios (ADR-0142)
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


async def _negocio(plan: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Negocio v3 en `plan` con una conexión conversacional: Coordinador y General instanciados."""
    from core.alta_negocio import onboard_tenant
    from core.releases.release_uno import backfill_releases

    slug = f"bill05-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"BILL05 {slug}", slug=slug, packs=PACKS, sembrar_ejemplos=False, plan=plan)
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

    email = f"{uniq('dueno-b05')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _especialista(app_client, h: dict[str, str], key: str) -> str:
    """Especialista con una versión publicada (sin instancia: todavía no cuenta)."""
    from test_agentes_versionados import spec_en_su_enfoque

    r = await app_client.post("/api/v1/admin/agents", headers=h, json={"key": key, "kind": "specialist", "name": key})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": spec_en_su_enfoque(key), "lock_version": 0}
    )
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
    assert r.status_code == 200, r.text
    return aid


async def _agentes(tid: uuid.UUID) -> int:
    from core.billing.metrics import count_metric

    return await count_metric(tid, "agentes")


async def _plan(tid: uuid.UUID, plan_id: str) -> None:
    await _sql(tid, "update tenant_subscriptions set plan_id = :p where tenant_id = :t", p=plan_id, t=tid)


def _agents_url(cid: Any) -> str:
    return f"/api/v1/admin/connections/{cid}/agents"


def _limite(r: Any) -> tuple[int, str, dict[str, Any]]:
    error = r.json()["error"]
    return r.status_code, error["code"], error.get("details") or {}


async def _instancias_vivas(tid: uuid.UUID, aid: str) -> int:
    filas = await _sql(
        tid, "select count(*) from agent_instances where agent_id = :a and deleted_at is null", a=uuid.UUID(aid)
    )
    return int(filas[0][0])


# ---------------------------------------------------------------- agentes
async def _llenar_cupo(
    app_client, h: dict[str, str], cid: Any, key: str = "ventas", *, por_defecto: bool = False
) -> str:
    """Instala un especialista en la conexión (ocupa un cupo de `agentes`)."""
    aid = await _especialista(app_client, h, key)
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": aid, "is_default": por_defecto})
    assert r.status_code == 201, r.text
    return aid


async def test_free_el_general_no_cuenta_y_un_segundo_agente_da_409(app_client) -> None:
    tid, cid = await _negocio("free")
    h = await api_headers(app_client, tid)  # master: escribe con cualquier runtime
    # Ni el General ni el Coordinador cuentan: el negocio arranca en 0 con ambos instanciados.
    assert await _agentes(tid) == 0
    kinds = await _sql(
        tid,
        "select a.key, a.kind from agent_instances i join agents a on a.id = i.agent_id "
        "where i.tenant_id = :t and i.deleted_at is null order by a.key",
        t=tid,
    )
    assert [tuple(k) for k in kinds] == [("coordinador", "core"), ("general", "specialist")]

    ventas = await _especialista(app_client, h, "ventas")
    assert await _agentes(tid) == 0  # publicado sin instancia: todavía no es unidad facturable
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": ventas})
    assert r.status_code == 201, r.text  # el primero entra: el General no ocupó el cupo del Free
    assert await _agentes(tid) == 1

    soporte = await _especialista(app_client, h, "soporte")
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": soporte})
    assert _limite(r) == (409, "plan_limit_agentes", {"metric": "agentes", "used": 1, "limit": 1}), r.text
    assert "mejora tu plan" in r.json()["error"]["message"].lower()
    assert await _instancias_vivas(tid, soporte) == 0
    assert await _agentes(tid) == 1


async def test_el_general_se_puede_volver_a_poner_con_el_cupo_lleno(app_client) -> None:
    tid, cid = await _negocio("free")
    h = await api_headers(app_client, tid)
    await _llenar_cupo(app_client, h, cid, por_defecto=True)  # Free al tope: 1 de 1; deja de ser el General
    (general,) = (await _sql(tid, "select id from agents where tenant_id = :t and key = 'general'", t=tid))[0]
    (instancia,) = (
        await _sql(tid, "select id from agent_instances where agent_id = :a and deleted_at is null", a=general)
    )[0]
    r = await app_client.delete(f"{_agents_url(cid)}/{instancia}", headers=h)
    assert r.status_code == 204, r.text
    # El General no ocupa cupo: volver a ponerlo no se mide contra el tope.
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": str(general)})
    assert r.status_code == 201, r.text
    assert await _agentes(tid) == 1


async def test_free_el_editor_no_publica_la_version_si_no_hay_cupo(app_client) -> None:
    tid, cid = await _negocio("free")
    hm = await api_headers(app_client, tid)
    await _llenar_cupo(app_client, hm, cid, "soporte")  # Free al tope: 1 de 1
    h = await _dueno(app_client, tid)
    r = await app_client.post("/api/v1/admin/agent-templates/ventas/agents", headers=h, json={})
    assert r.status_code == 201, r.text
    aid = r.json()["agent"]["id"]
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/publish", headers=h, json={"connection_ids": [str(cid)]})
    assert _limite(r) == (409, "plan_limit_agentes", {"metric": "agentes", "used": 1, "limit": 1}), r.text
    # Antes de escribir: ni versión nueva ni instancia.
    assert await _sql(tid, "select 1 from agent_versions where agent_id = :a", a=uuid.UUID(aid)) == []
    assert await _instancias_vivas(tid, aid) == 0


async def test_premium_permite_cinco_agentes_y_el_sexto_da_409(app_client) -> None:
    tid, cid = await _negocio("premium")
    h = await api_headers(app_client, tid)
    agentes = [await _especialista(app_client, h, f"esp{i}") for i in range(6)]
    for aid in agentes[:5]:  # el General no cuenta: 5 especialistas llenan el cupo
        r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": aid})
        assert r.status_code == 201, r.text
    assert await _agentes(tid) == 5
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": agentes[5]})
    assert _limite(r) == (409, "plan_limit_agentes", {"metric": "agentes", "used": 5, "limit": 5}), r.text

    # Un agente que ya está en una conexión no ocupa otro cupo al entrar en una segunda.
    from core.agents import instancias
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    cid2 = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid2, t=tid, ext=f"ig-{uuid.uuid4().hex[:10]}",
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await instancias.asegurar_instancias_base(session, tid, cid2)
        nueva = await instancias.agregar_instancia(tid, cid2, uuid.UUID(agentes[0]))
    assert nueva is not None
    assert await _agentes(tid) == 5


async def test_por_encima_del_tope_sigue_operando_y_solo_no_crece(app_client) -> None:
    tid, cid = await _negocio("premium")
    h = await api_headers(app_client, tid)
    ventas = await _especialista(app_client, h, "ventas")
    agenda = await _especialista(app_client, h, "agenda")
    soporte = await _especialista(app_client, h, "soporte")
    nuevo = await _especialista(app_client, h, "facturas")
    ids = {}
    for aid in (ventas, agenda, soporte):
        r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": aid})
        assert r.status_code == 201, r.text
        ids[aid] = r.json()["id"]

    await _plan(tid, "free")  # baja de plan: 3 agentes con tope 1
    assert await _agentes(tid) == 3  # el General no suma

    # Operar no se rompe: cambiar una instancia, publicar otra versión de un agente que ya está.
    r = await app_client.patch(f"{_agents_url(cid)}/{ids[ventas]}", headers=h, json={"priority": 10})
    assert r.status_code == 200, r.text
    r = await app_client.post(f"/api/v1/admin/agents/{ventas}/versions", headers=h, json={"notes": "v2"})
    assert r.status_code == 200, r.text
    # El plan muestra el exceso sin romper la lectura.
    r = await app_client.get("/api/v1/admin/plan", headers=h)
    assert r.status_code == 200, r.text
    uso = {u["key"]: u for u in r.json()["usage"]}
    assert (uso["agentes"]["used"], uso["agentes"]["limit"], uso["agentes"]["exceeded"]) == (3, 1, True)
    # Crecer sí se bloquea.
    r = await app_client.post(_agents_url(cid), headers=h, json={"agent_id": nuevo})
    assert _limite(r) == (409, "plan_limit_agentes", {"metric": "agentes", "used": 3, "limit": 1}), r.text
    # Quitar uno no rompe nada (el negocio decide cómo bajar).
    r = await app_client.delete(f"{_agents_url(cid)}/{ids[agenda]}", headers=h)
    assert r.status_code == 204, r.text
    assert await _agentes(tid) == 2


# ---------------------------------------------------------------- colaboradores
async def test_colaboradores_al_tope_dan_409_y_no_se_crea_nada(app_client) -> None:
    from core.billing.metrics import count_metric

    tid, _cid = await _negocio("free")  # tope 2
    h = await _dueno(app_client, tid)
    assert await count_metric(tid, "colaboradores") == 0  # el propietario no cuenta

    for i in range(2):
        r = await app_client.post(
            "/api/v1/admin/collaborators", headers=h,
            json={"email": f"{uniq(f'col{i}')}@example.test", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
    assert await count_metric(tid, "colaboradores") == 2

    tercero = f"{uniq('col3')}@example.test"
    r = await app_client.post(
        "/api/v1/admin/collaborators", headers=h, json={"email": tercero, "password": PASSWORD}
    )
    assert _limite(r) == (409, "plan_limit_colaboradores", {"metric": "colaboradores", "used": 2, "limit": 2}), r.text
    assert await _sql(None, "select 1 from admins where email = :e", e=tercero) == []

    # Invitar una cuenta existente tampoco (no se envía nada).
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    existente = f"{uniq('existe')}@example.test"
    async with get_session() as session:
        session.add(Admin(email=existente, password_hash=hash_password(PASSWORD), role="colaborador"))
    r = await app_client.post(
        "/api/v1/admin/collaborators", headers=h, json={"email": existente, "password": PASSWORD}
    )
    assert _limite(r)[:2] == (409, "plan_limit_colaboradores"), r.text
    assert await _sql(None, "select 1 from tenant_invitations where tenant_id = :t", t=tid) == []


async def test_la_invitacion_pendiente_ocupa_cupo(app_client) -> None:
    from core.admin.auth import hash_password
    from core.billing.metrics import count_metric
    from core.db.models import Admin
    from core.db.session import get_session

    tid, _cid = await _negocio("free")
    h = await _dueno(app_client, tid)
    correos = [f"{uniq(f'inv{i}')}@example.test" for i in range(3)]
    async with get_session() as session:
        for correo in correos:
            session.add(Admin(email=correo, password_hash=hash_password(PASSWORD), role="colaborador"))
    for correo in correos[:2]:
        r = await app_client.post("/api/v1/admin/collaborators", headers=h, json={"email": correo, "password": PASSWORD})
        assert (r.status_code, r.json()["resultado"]) == (200, "invitado"), r.text
    assert await count_metric(tid, "colaboradores") == 2
    # Refrescar una invitación pendiente no crece.
    r = await app_client.post("/api/v1/admin/collaborators", headers=h, json={"email": correos[0], "password": PASSWORD})
    assert r.status_code == 200, r.text
    r = await app_client.post("/api/v1/admin/collaborators", headers=h, json={"email": correos[2], "password": PASSWORD})
    assert _limite(r)[:2] == (409, "plan_limit_colaboradores"), r.text
    r = await app_client.post("/api/v1/admin/invitations", headers=h, json={"email": correos[2]})
    assert _limite(r)[:2] == (409, "plan_limit_colaboradores"), r.text
    # Invitarlo como propietario no salta el tope: un propietario solo invita colaboradores.
    r = await app_client.post("/api/v1/admin/invitations", headers=h, json={"email": correos[2], "role": "owner"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------- aislamiento y plan
async def test_aislamiento_entre_negocios_y_plan_en_la_api(app_client) -> None:
    from core.billing.metrics import count_metric

    lleno, cid_lleno = await _negocio("free")
    otro, cid_otro = await _negocio("free")
    h_lleno = await api_headers(app_client, lleno)
    h_otro = await api_headers(app_client, otro)

    # El negocio lleno llega al tope de colaboradores; el otro no se entera.
    h_dueno = await _dueno(app_client, lleno)
    for i in range(2):
        r = await app_client.post(
            "/api/v1/admin/collaborators", headers=h_dueno,
            json={"email": f"{uniq(f'aisl{i}')}@example.test", "password": PASSWORD},
        )
        assert r.status_code == 200, r.text
    assert await count_metric(lleno, "colaboradores") == 2
    assert await count_metric(otro, "colaboradores") == 0
    h_dueno_otro = await _dueno(app_client, otro)
    r = await app_client.post(
        "/api/v1/admin/collaborators", headers=h_dueno_otro,
        json={"email": f"{uniq('aisl-otro')}@example.test", "password": PASSWORD},
    )
    assert r.status_code == 200, r.text

    # Agentes: el lleno no puede crecer; los agentes del otro no le suman ni le restan.
    assert await _agentes(lleno) == 0 and await _agentes(otro) == 0
    await _llenar_cupo(app_client, h_lleno, cid_lleno, "ventas")
    await _llenar_cupo(app_client, h_otro, cid_otro, "ventas")
    assert await _agentes(lleno) == 1 and await _agentes(otro) == 1
    del_otro = await _especialista(app_client, h_otro, "soporte")
    r = await app_client.post(_agents_url(cid_otro), headers=h_otro, json={"agent_id": del_otro})
    assert _limite(r)[:2] == (409, "plan_limit_agentes")  # su propio tope, no el del vecino
    await _plan(otro, "premium")
    r = await app_client.post(_agents_url(cid_otro), headers=h_otro, json={"agent_id": del_otro})
    assert r.status_code == 201, r.text
    assert await _agentes(otro) == 2 and await _agentes(lleno) == 1
    # El agente de otro negocio no se instala en la conexión de este (404, no 409).
    r = await app_client.post(_agents_url(cid_lleno), headers=h_lleno, json={"agent_id": del_otro})
    assert r.status_code == 404, r.text

    # GET /admin/plan: agentes, colaboradores y crédito del plan actual, solo del negocio del header.
    r = await app_client.get("/api/v1/admin/plan", headers=h_lleno)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    uso = {u["key"]: u for u in cuerpo["usage"]}
    assert (uso["agentes"]["used"], uso["agentes"]["limit"], uso["agentes"]["percent"]) == (1, 1, 100.0)
    assert (uso["colaboradores"]["used"], uso["colaboradores"]["limit"]) == (2, 2)
    assert "plugins" not in uso
    # Crédito medido (ADR-0144): el negocio no ha gastado nada en el mes.
    assert (cuerpo["credit"]["limit"], cuerpo["credit"]["used"], cuerpo["credit"]["percent"]) == (2500, 0.0, 0.0)
    catalogo = {p["id"]: p for p in cuerpo["plans"]}
    assert catalogo["premium"]["monthly_credits"] == 75000
    assert catalogo["empresarial"]["limits"]["agentes"] is None


async def test_desactivar_libera_cupo_y_reactivar_al_tope_da_409(app_client) -> None:
    """Decisión del dueño (2026-10-19): una cuenta desactivada no cuenta. Reactivarla vuelve a
    medir el tope: por encima, 409 y la cuenta sigue desactivada (conservador)."""
    from core.billing.metrics import count_metric

    tid, _cid = await _negocio("free")  # tope 2
    h = await _dueno(app_client, tid)
    correos = [f"{uniq(f'act{i}')}@example.test" for i in range(3)]
    for correo in correos[:2]:
        r = await app_client.post("/api/v1/admin/collaborators", headers=h, json={"email": correo, "password": PASSWORD})
        assert r.status_code == 200, r.text
    assert await count_metric(tid, "colaboradores") == 2
    (primero,) = await _sql(None, "select id from admins where email = :e", e=correos[0])
    primero_id = primero[0]

    # Desactivar libera cupo: entra un tercero.
    r = await app_client.post(f"/api/v1/admin/collaborators/{primero_id}/toggle", headers=h)
    assert r.status_code == 204, r.text
    assert await count_metric(tid, "colaboradores") == 1
    r = await app_client.post("/api/v1/admin/collaborators", headers=h, json={"email": correos[2], "password": PASSWORD})
    assert r.status_code == 200, r.text

    # Reactivar con el negocio al tope: 409 y la cuenta sigue desactivada.
    r = await app_client.post(f"/api/v1/admin/collaborators/{primero_id}/toggle", headers=h)
    assert _limite(r) == (409, "plan_limit_colaboradores", {"metric": "colaboradores", "used": 2, "limit": 2}), r.text
    assert (await _sql(None, "select active from admins where id = :a", a=primero_id))[0][0] is False

    # Con cupo (plan mayor), reactivar sí entra.
    await _plan(tid, "basic")
    r = await app_client.post(f"/api/v1/admin/collaborators/{primero_id}/toggle", headers=h)
    assert r.status_code == 204, r.text
    assert await count_metric(tid, "colaboradores") == 3
