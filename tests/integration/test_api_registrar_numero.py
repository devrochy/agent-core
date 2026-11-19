"""`POST /api/v1/admin/meta/connections/{id}/numbers/{phone_number_id}/register`.

Registrar un número en la Cloud API con PIN, para conexiones BYOK y manuales (antes solo
el wizard de Embedded Signup). Con la BD y Redis reales; Graph, falso.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _negocio_con_byok() -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """Negocio con una conexión BYOK lista (secreto y token) y un número suyo."""
    from core.alta_negocio import onboard_tenant
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.shared.tenant import tenant_scope

    slug = uniq("registrar")
    tid, _ = await onboard_tenant(
        name=f"Registrar {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    app_id, waba_id, phone_id = uniq("app"), uniq("waba"), uniq("ph")
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                status="connected",
            )
            session.add(connection)
            await session.flush()
            session.add(WabaAccount(tenant_id=tid, connection_id=connection.id, waba_id=waba_id, subscribed=True))
            session.add(PhoneNumber(tenant_id=tid, connection_id=connection.id, phone_number_id=phone_id))
            cid = connection.id
        await meta_token_store.put_token(cid, meta_byok.APP_SECRET_KIND, app_id, "sec", tenant_id=tid)
        await meta_token_store.put_token(cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, app_id, "tok", tenant_id=tid)
    return tid, cid, waba_id, phone_id


def _graph_falso(monkeypatch, waba_id: str) -> list[tuple[str, dict | None]]:
    import core.gateway.meta_phone_numbers as phone_numbers_mod

    llamadas: list[tuple[str, dict | None]] = []

    async def fake_graph_fetch(path, *, method="GET", body=None, **_kwargs):
        llamadas.append((path, body))
        if path == f"/{waba_id}/phone_numbers":
            return {"data": [], "paging": {"cursors": {}}}
        if path.endswith("/register") and method == "POST":
            return {"success": True}
        raise AssertionError(f"path no esperado: {method} {path}")

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", fake_graph_fetch)
    return llamadas


def _url(cid, phone_id) -> str:
    return f"/api/v1/admin/meta/connections/{cid}/numbers/{phone_id}/register"


async def test_registra_el_numero_con_el_pin_y_lo_marca(app_client, monkeypatch) -> None:
    from core.gateway import meta_token_store
    from core.gateway.meta_onboarding import PIN_KIND
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_id)
    headers = await api_headers(app_client, tid)

    r = await app_client.post(_url(cid, phone_id), json={"pin": "246810"}, headers=headers)

    assert r.status_code == 204, r.text
    assert (f"/{phone_id}/register", {"messaging_product": "whatsapp", "pin": "246810"}) in llamadas
    async with tenant_scope(tid):
        assert await meta_token_store.get_token(cid, PIN_KIND, phone_id, tenant_id=tid) == "246810"

    numeros = await app_client.get(f"/api/v1/admin/meta/connections/{cid}/numbers", headers=headers)
    assert numeros.status_code == 200, numeros.text
    (numero,) = numeros.json()["numbers"]
    assert numero["registered"] is True
    assert "246810" not in numeros.text


async def test_sin_pin_genera_uno(app_client, monkeypatch) -> None:
    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_id)

    r = await app_client.post(_url(cid, phone_id), json={}, headers=await api_headers(app_client, tid))

    assert r.status_code == 204, r.text
    (body,) = [b for path, b in llamadas if path.endswith("/register")]
    assert body is not None and len(body["pin"]) == 6 and body["pin"].isdigit()


async def test_la_conexion_de_otro_negocio_no_se_toca(app_client, monkeypatch) -> None:
    _tid_a, cid_a, waba_a, phone_a = await _negocio_con_byok()
    tid_b, _cid_b, _waba_b, _phone_b = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_a)

    r = await app_client.post(_url(cid_a, phone_a), json={}, headers=await api_headers(app_client, tid_b))

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "register_failed"
    assert llamadas == []


async def test_con_su_propia_conexion_un_negocio_no_registra_el_numero_de_otro(app_client, monkeypatch) -> None:
    # B usa SU conexión, con credenciales válidas, y el `phone_number_id` de A: su token podría
    # ver el número (un BYOK de agencia), así que el freno tiene que ser la base, no Meta.
    _tid_a, _cid_a, waba_a, phone_a = await _negocio_con_byok()
    tid_b, cid_b, _waba_b, _phone_b = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_a)

    r = await app_client.post(_url(cid_b, phone_a), json={}, headers=await api_headers(app_client, tid_b))

    assert r.status_code == 400
    assert r.json()["error"]["message"] == "El número no es de esta conexión"
    assert llamadas == []


@pytest.mark.parametrize(
    ("accion", "cuerpo"),
    [("request-code", {"code_method": "SMS", "language": "es"}), ("verify", {"code": "246810"})],
)
async def test_pedir_o_verificar_codigo_de_un_numero_ajeno_no_llega_a_meta(
    app_client, monkeypatch, accion, cuerpo
) -> None:
    _tid_a, _cid_a, waba_a, phone_a = await _negocio_con_byok()
    tid_b, cid_b, _waba_b, _phone_b = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_a)

    r = await app_client.post(
        f"/api/v1/admin/meta/connections/{cid_b}/numbers/{phone_a}/{accion}",
        json=cuerpo,
        headers=await api_headers(app_client, tid_b),
    )

    assert r.status_code == 400
    assert r.json()["error"]["message"] == "El número no es de esta conexión"
    assert llamadas == []


async def test_un_numero_de_otra_conexion_del_mismo_negocio_no_se_registra(app_client, monkeypatch) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.shared.tenant import tenant_scope

    tid, _cid, waba_id, phone_id = await _negocio_con_byok()
    otra_app = uniq("app")
    async with tenant_scope(tid):
        async with get_session() as session:
            otra = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="production", origin="byok",
                meta_app_id=otra_app, status="connected",
            )
            session.add(otra)
            await session.flush()
            otra_id = otra.id
        # Con credenciales: el rechazo tiene que venir de que el número no es suyo.
        await meta_token_store.put_token(otra_id, meta_byok.APP_SECRET_KIND, otra_app, "sec2", tenant_id=tid)
        await meta_token_store.put_token(
            otra_id, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, otra_app, "tok2", tenant_id=tid
        )
    llamadas = _graph_falso(monkeypatch, waba_id)

    # El número es de la primera conexión, no de `otra`.
    r = await app_client.post(_url(otra_id, phone_id), json={}, headers=await api_headers(app_client, tid))

    assert r.status_code == 400
    assert r.json()["error"]["message"] == "El número no es de esta conexión"
    assert llamadas == []


async def test_un_pin_errado_no_borra_el_guardado(app_client, monkeypatch) -> None:
    import core.gateway.meta_phone_numbers as phone_numbers_mod
    from core.gateway import meta_token_store
    from core.gateway.meta_errors import GraphError
    from core.gateway.meta_onboarding import PIN_KIND
    from core.shared.tenant import tenant_scope

    tid, cid, _waba_id, phone_id = await _negocio_con_byok()
    async with tenant_scope(tid):
        await meta_token_store.put_token(cid, PIN_KIND, phone_id, "135790", tenant_id=tid)

    async def fake_graph_fetch(path, *, method="GET", body=None, **_kwargs):
        assert path == f"/{phone_id}/register"
        raise GraphError("PIN mismatch", category="PERMANENT", http_status=400, code=133005)

    monkeypatch.setattr(phone_numbers_mod, "graph_fetch", fake_graph_fetch)

    r = await app_client.post(_url(cid, phone_id), json={"pin": "246810"}, headers=await api_headers(app_client, tid))

    assert r.status_code == 400
    assert "otro PIN" in r.json()["error"]["message"]
    async with tenant_scope(tid):
        assert await meta_token_store.get_token(cid, PIN_KIND, phone_id, tenant_id=tid) == "135790"


async def test_un_pin_mal_formado_da_400(app_client, monkeypatch) -> None:
    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_id)

    r = await app_client.post(_url(cid, phone_id), json={"pin": "12ab56"}, headers=await api_headers(app_client, tid))

    assert r.status_code == 400
    assert "6 dígitos" in r.json()["error"]["message"]
    assert llamadas == []


async def test_un_intento_por_minuto(app_client, monkeypatch) -> None:
    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    _graph_falso(monkeypatch, waba_id)
    headers = await api_headers(app_client, tid)

    assert (await app_client.post(_url(cid, phone_id), json={}, headers=headers)).status_code == 204
    segundo = await app_client.post(_url(cid, phone_id), json={}, headers=headers)

    assert segundo.status_code == 400
    assert "minuto" in segundo.json()["error"]["message"]


async def test_sin_permiso_de_escritura_da_403(app_client, monkeypatch) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    llamadas = _graph_falso(monkeypatch, waba_id)
    email = f"{uniq('colaborador')}@example.test"
    async with get_session() as session:
        colaborador = Admin(email=email, password_hash=hash_password("clave-segura-123"), role="colaborador")
        session.add(colaborador)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=colaborador.id, role="colaborador"))

    headers = await api_headers(app_client, tid, email=email, password="clave-segura-123")
    r = await app_client.post(_url(cid, phone_id), json={}, headers=headers)

    assert r.status_code == 403
    assert llamadas == []
