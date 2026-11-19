"""P0 de correctitud del ciclo de pruebas 2026-10-13, contra la API real:

1. negativos → 422 con el sobre `{"error": {code, message, fields}}` (config de pack y
   cuerpo Pydantic);
2. habilitar al tope del plan → 409 `plan_limit_plugins` y `GET` sigue `enabled_for_tenant=false`;
3. «Sincronizar» plantillas sin candado horario (204) y 429 al repetir dentro del minuto;
4. `primary` sobre una cuenta de Meta → 409 explicado; números dados de baja fuera del listado;
   alta BYOK reentrante con `reused`.
"""

from __future__ import annotations

import os

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _tenant(prefix: str, packs: list[str] | None = None):
    from core.alta_negocio import onboard_tenant

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"P0 {slug}", slug=slug, business_description="P0 de pruebas.",
        persona={"tone": "cercano"}, packs=packs, admin_email=ADMIN_EMAIL,
    )
    return tid


async def _plan(tid, plan_id: str) -> None:
    from sqlalchemy import select

    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(TenantSubscription).where(
                        TenantSubscription.tenant_id == tid, TenantSubscription.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
            sub.plan_id = plan_id
            sub.status = "active"


# ------------------------------------------------------------------ 1. negativos


async def test_config_de_pack_con_negativo_responde_422_por_campo(app_client) -> None:
    tid = await _tenant("p0-neg", packs=["pack_cobranza"])
    headers = await api_headers(app_client, tid)

    r = await app_client.put(
        "/api/v1/admin/plugins/pack_cobranza/config", headers=headers,
        json={"values": {"dias_tolerancia_cobro": -4}},
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_config"
    assert error["fields"] == {"dias_tolerancia_cobro": "Debe ser mayor o igual que 0."}

    # Un valor válido sí se guarda.
    r_ok = await app_client.put(
        "/api/v1/admin/plugins/pack_cobranza/config", headers=headers,
        json={"values": {"dias_tolerancia_cobro": 3}},
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.json()["values"]["dias_tolerancia_cobro"] == 3


async def test_cuerpo_pydantic_con_negativo_responde_422_con_el_sobre(app_client) -> None:
    tid = await _tenant("p0-reglas", packs=["pack_cobranza"])
    headers = await api_headers(app_client, tid)

    r = await app_client.put(
        "/api/v1/pack/cobranza/rules", headers=headers,
        json={"max_dias_prorroga": -4, "max_reprogramaciones": 2, "min_abono_pct": 10, "umbral_saldo_autonomo": 0},
    )
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Revisa los campos marcados."
    assert error["fields"] == {"max_dias_prorroga": "Debe ser mayor o igual que 0."}
    assert "detail" not in r.json()

    r_mem = await app_client.put(
        "/api/v1/admin/settings/memory", headers=headers,
        json={"short_term_turns": -1, "rag_top_k": 0, "rag_min_score": 0.6},
    )
    assert r_mem.status_code == 422, r_mem.text
    assert "short_term_turns" in r_mem.json()["error"]["fields"]


async def test_config_guardada_fuera_de_rango_no_tumba_el_get(app_client) -> None:
    """Una config guardada antes de endurecer el schema se reporta, no revienta con 500."""
    from core.db.models import PluginConfig
    from core.db.session import get_session
    from core.shared.crypto import get_secret_box
    from core.shared.tenant import tenant_scope

    tid = await _tenant("p0-vieja", packs=["pack_cobranza"])
    headers = await api_headers(app_client, tid)
    from sqlalchemy import select

    cifrada = get_secret_box().encrypt_json({"dias_tolerancia_cobro": -4})
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (
                await session.execute(
                    select(PluginConfig).where(
                        PluginConfig.plugin_id == "pack_cobranza", PluginConfig.tenant_id == tid
                    )
                )
            ).scalar_one_or_none()
            if fila is None:
                session.add(PluginConfig(plugin_id="pack_cobranza", tenant_id=tid, config_encrypted=cifrada))
            else:
                fila.config_encrypted = cifrada
    r = await app_client.get("/api/v1/admin/plugins/pack_cobranza/config", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["values"] == {}


# ------------------------------------------------------------------ 2. tope del plan


async def test_habilitar_al_tope_del_plan_responde_409_y_queda_deshabilitada(app_client, monkeypatch) -> None:
    from dataclasses import replace

    from core.billing import plans

    # Desde ADR-0142 ningún plan del catálogo limita las habilidades; el contrato del 409 se
    # sigue probando con un `free` de prueba que trae `plugins: 1`.
    free = plans.PLANS_BY_ID["free"]
    monkeypatch.setitem(plans.PLANS_BY_ID, "free", replace(free, limits={**free.limits, "plugins": 1}))
    tid = await _tenant("p0-tope", packs=["pack_commerce"])
    await _plan(tid, "free")  # `plugins: 1`, y commerce ya ocupa el cupo
    headers = await api_headers(app_client, tid)

    r = await app_client.post(
        "/api/v1/admin/plugins/pack_calendar/action", headers=headers, json={"action": "activate"}
    )
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_plugins"
    assert error["details"] == {"metric": "plugins", "used": 1, "limit": 1, "enabled": False}

    r_list = await app_client.get("/api/v1/admin/plugins", headers=headers)
    estado = {p["id"]: p["enabled_for_tenant"] for p in r_list.json()}
    assert estado["pack_calendar"] is False and estado["pack_commerce"] is True

    # Guardar la config de otra habilidad al tope tampoco la habilita ni guarda nada.
    r_cfg = await app_client.put(
        "/api/v1/admin/plugins/pack_cobranza/config", headers=headers,
        json={"values": {"dias_tolerancia_cobro": 1}},
    )
    assert r_cfg.status_code == 409, r_cfg.text
    r_get = await app_client.get("/api/v1/admin/plugins/pack_cobranza/config", headers=headers)
    assert r_get.json()["values"].get("dias_tolerancia_cobro", 0) == 0


# ------------------------------------------------------------------ 3. plantillas


async def _cuenta_meta(tid, *, origin: str = "byok", app_id: str | None = None):
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import TOKEN_KIND
    from core.shared.tenant import tenant_scope

    waba_id = uniq("waba")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", origin=origin, meta_app_id=app_id or "app-embedded-signup",
                status="connected", business_id="biz-p0",
            )
            session.add(connection)
            await session.flush()
            waba = WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id)
            session.add(waba)
            await session.flush()
            connection_id = connection.id
        await meta_token_store.put_token(connection_id, TOKEN_KIND, "biz-p0", "tok-p0", tenant_id=tid)
    return connection_id, waba_id


async def test_sincronizar_plantillas_a_mano_no_espera_la_hora(app_client, monkeypatch) -> None:
    import core.gateway.meta_template_sync as sync_mod
    import core.gateway.meta_templates as templates_mod
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    tid = await _tenant("p0-tpl")
    connection_id, waba_id = await _cuenta_meta(tid, origin="embedded_signup")
    headers = await api_headers(app_client, tid)
    nombres = ["bienvenida"]
    llamadas: list[str] = []

    async def _fake_graph_fetch(path, *, method="GET", body=None, query=None, **kwargs):
        if path == f"/{waba_id}/message_templates" and query and query.get("summary"):
            return {"summary": {"message_template_count": 1, "message_template_limit": 250}}
        if path == f"/{waba_id}/message_templates" and method == "GET":
            llamadas.append("list")
            return {
                "data": [
                    {"id": f"tpl-{n}", "name": n, "language": "es", "status": "APPROVED", "category": "UTILITY",
                     "components": [{"type": "BODY", "text": "hola"}]}
                    for n in nombres
                ],
                "paging": {"cursors": {}},
            }
        raise AssertionError(f"no esperado: {method} {path}")

    monkeypatch.setattr(templates_mod, "graph_fetch", _fake_graph_fetch)
    monkeypatch.setattr(sync_mod, "graph_fetch", _fake_graph_fetch)
    base = f"/api/v1/admin/meta/connections/{connection_id}/templates"

    r1 = await app_client.get(base, headers=headers)  # toma el candado horario
    assert r1.status_code == 200 and [t["name"] for t in r1.json()["templates"]] == ["bienvenida"]

    # Se creó otra plantilla en Meta: el botón debe traerla aunque la hora ya tenga candado.
    nombres.append("promo")
    r_sync = await app_client.post(f"{base}/sync", headers=headers)
    assert r_sync.status_code == 204, r_sync.text
    r2 = await app_client.get(base, headers=headers)
    assert sorted(t["name"] for t in r2.json()["templates"]) == ["bienvenida", "promo"]

    r_again = await app_client.post(f"{base}/sync", headers=headers)
    assert r_again.status_code == 429, r_again.text
    assert r_again.json()["error"]["code"] == "sync_rate_limited"


async def test_eliminar_plantilla_de_ejemplo_responde_409_claro(app_client, monkeypatch) -> None:
    from sqlalchemy import select

    import core.gateway.meta_templates as templates_mod
    from core.config import get_settings
    from core.db.models import MessageTemplate, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    tid = await _tenant("p0-hello")
    connection_id, waba_id = await _cuenta_meta(tid, origin="embedded_signup")
    headers = await api_headers(app_client, tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            row = MessageTemplate(
                tenant_id=tid, waba_account_id=waba.id, meta_template_id=uniq("tpl"), name="hello_world",
                language="en_US", status="APPROVED", category="UTILITY",
            )
            session.add(row)
            await session.flush()
            template_id = row.id

    async def _fake_graph_fetch(path, *, method="GET", **kwargs):
        return {
            "error": {"message": "(#100) Invalid parameter", "code": 100, "type": "OAuthException"}
        }

    async def _delete(*args, **kwargs):
        from core.gateway.meta_errors import GraphError

        raise GraphError("Error de Meta: Invalid parameter", category="PERMANENT", code=100, http_status=400)

    monkeypatch.setattr(templates_mod, "graph_fetch", _delete)
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{connection_id}/templates/{template_id}/delete", headers=headers
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "template_not_deletable"
    assert "plantilla de ejemplo «hello_world»" in r.json()["error"]["message"]
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.get(MessageTemplate, template_id)).status == "APPROVED"


async def test_otro_negocio_no_alcanza_plantillas_ni_conexiones_ajenas(app_client, monkeypatch) -> None:
    """Endpoints nuevos o tocados con ids de otro tenant: 404, sin llamar a Meta ni tocar la fila."""
    from sqlalchemy import select

    import core.gateway.meta_template_sync as sync_mod
    import core.gateway.meta_templates as templates_mod
    from core.config import get_settings
    from core.db.models import MessageTemplate, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "app-embedded-signup")
    monkeypatch.setattr(settings, "meta_app_secret", "s3cr3t-de-la-app")
    tid_a = await _tenant("p0-xa")
    conn_a, waba_a = await _cuenta_meta(tid_a, origin="embedded_signup")
    tid_b = await _tenant("p0-xb")
    conn_b, _ = await _cuenta_meta(tid_b, origin="embedded_signup")
    headers_b = await api_headers(app_client, tid_b)
    async with tenant_scope(tid_a):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_a))).scalar_one()
            row = MessageTemplate(
                tenant_id=tid_a, waba_account_id=waba.id, meta_template_id=uniq("tpl"), name="pedido_a",
                language="es", status="APPROVED", category="UTILITY",
            )
            session.add(row)
            await session.flush()
            template_a = row.id

    llamadas: list[str] = []

    async def _graph(path, **kwargs):
        llamadas.append(path)
        raise AssertionError(f"no debe llamar a Meta: {path}")

    monkeypatch.setattr(templates_mod, "graph_fetch", _graph)
    monkeypatch.setattr(sync_mod, "graph_fetch", _graph)

    # Plantilla de A por la conexión de B.
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{conn_b}/templates/{template_a}/delete", headers=headers_b
    )
    assert r.status_code == 404, r.text
    # Conexión de A con la sesión de B: sync, delete y primary.
    r = await app_client.post(f"/api/v1/admin/meta/connections/{conn_a}/templates/sync", headers=headers_b)
    assert r.status_code in (400, 404), r.text
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{conn_a}/templates/{template_a}/delete", headers=headers_b
    )
    assert r.status_code in (400, 404), r.text
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{conn_a}/action", headers=headers_b, json={"action": "primary"}
    )
    assert r.status_code == 404, r.text
    assert llamadas == []
    async with tenant_scope(tid_a):
        async with get_session() as session:
            assert (await session.get(MessageTemplate, template_a)).status == "APPROVED"


# ------------------------------------------------------------------ 4. Meta


async def test_principal_en_cuenta_de_meta_explica_la_regla(app_client) -> None:
    tid = await _tenant("p0-primary")
    connection_id, _ = await _cuenta_meta(tid)
    headers = await api_headers(app_client, tid)
    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{connection_id}/action", headers=headers, json={"action": "primary"}
    )
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "primary_not_applicable"
    assert "conexiones manuales" in r.json()["error"]["message"]

    # Una cuenta de otro negocio (o inexistente) no revela nada: 404.
    otro = await _tenant("p0-primary-otro")
    r_otro = await app_client.post(
        f"/api/v1/admin/meta/connections/{connection_id}/action",
        headers=await api_headers(app_client, otro), json={"action": "primary"},
    )
    assert r_otro.status_code == 404


async def test_numeros_dados_de_baja_no_se_listan(app_client, monkeypatch) -> None:
    import core.gateway.meta_phone_numbers as phones_mod
    from core.db.models import PhoneNumber, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant("p0-numeros")
    connection_id, _waba_id = await _cuenta_meta(tid)
    headers = await api_headers(app_client, tid)
    vivo, baja = uniq("pn-vivo"), uniq("pn-baja")
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(PhoneNumber(tenant_id=tid, connection_id=connection_id, phone_number_id=vivo, registered=True))
            session.add(
                PhoneNumber(tenant_id=tid, connection_id=connection_id, phone_number_id=baja, deleted_at=utcnow())
            )

    async def _sin_red(*args, **kwargs):
        return [{"id": vivo, "display_phone_number": "+1 555"}]

    monkeypatch.setattr(phones_mod, "list_phone_numbers", _sin_red)
    r = await app_client.get(f"/api/v1/admin/meta/connections/{connection_id}/numbers", headers=headers)
    assert r.status_code == 200, r.text
    assert [n["phone_number_id"] for n in r.json()["numbers"]] == [vivo]


async def test_alta_byok_con_la_misma_app_reutiliza_y_lo_dice(app_client, monkeypatch) -> None:
    from _api_helpers import stub_meta_pair_ok

    import core.gateway.meta_byok as byok_mod

    stub_meta_pair_ok(monkeypatch)

    async def _verify_ok(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+1 555", "verified_name": "P0", "platform_type": "CLOUD_API",
                            "status": "CONNECTED"}

    async def _graph_ok(path, **kwargs):
        if path.endswith("/subscribed_apps"):
            return {"data": [{"id": "app"}], "success": True}
        return {"data": [], "success": True}

    async def _subscribed(*args, **kwargs):
        return True

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _verify_ok)
    monkeypatch.setattr(byok_mod, "graph_fetch", _graph_ok)
    monkeypatch.setattr(byok_mod, "_subscribe_waba_to_app", _subscribed)

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _upsert)

    tid = await _tenant("p0-byok")
    headers = await api_headers(app_client, tid)
    payload = {
        "app_id": uniq("app").replace("-", ""), "app_secret": "secret", "phone_number_id": uniq("phone").replace("-", ""),
        "access_token": "tok", "waba_id": uniq("waba").replace("-", ""), "environment": "sandbox",
    }
    r1 = await app_client.post("/api/v1/admin/meta/wizard/byok", headers=headers, json=payload)
    assert r1.status_code == 201, r1.text
    assert r1.json()["reused"] is False
    r2 = await app_client.post("/api/v1/admin/meta/wizard/byok", headers=headers, json=payload)
    assert r2.status_code == 201, r2.text
    assert r2.json()["reused"] is True
    assert r2.json()["connectionId"] == r1.json()["connectionId"]

    r_list = await app_client.get("/api/v1/admin/connections", headers=headers)
    ids = [c["id"] for c in r_list.json()["items"] if c["kind"] == "meta_connection"]
    assert ids == [r1.json()["connectionId"]]
