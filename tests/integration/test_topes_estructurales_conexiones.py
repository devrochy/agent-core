"""HU-BILL-19 · conexiones, correos, canales y habilidades por plan contra Postgres real (D-79, ADR-0208 §8-10).

- Free con una conexión viva: crear o activar otra (de cualquier canal) da 409 `plan_limit_conexiones` con
  `{metric, used, limit}` y no escribe nada (servicio, reanudar desde la consola y habilitar el chat web).
- Basic con un correo conectado: conectar otro da 409 `plan_limit_correos` (`POST /connections/manual`).
- Basic no conecta Instagram ni Messenger y Free no conecta Telegram: 409 `plan_channel_not_included`.
- WhatsApp y el chat web se permiten en todos los planes.
- Basic no habilita un pack que pide Premium (aquí Cobranza simulado; hoy todos son Free): 409 `plan_plugin_not_included` y nada queda escrito.
- Un negocio por encima del tope conserva sus conexiones (edita y pausa) y solo no crece.
- Concurrencia: dos altas simultáneas con un solo cupo, entra una (`enforce_growth` y su lock).
- Aislamiento: el conteo y el tope de un negocio no ven las conexiones de otro.

Sin LLM ni red.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # topes por negocio y conteos entre negocios (ADR-0208 §8)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"
_EMAIL = {"host": "smtp.ejemplo.com", "port": 587, "username": "u", "password": "p", "use_tls": True}
_WHATSAPP = {"app_secret": "s", "access_token": "t", "verify_token": "v"}


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio(plan: str) -> uuid.UUID:
    """Negocio en `plan` SIN conexiones conversacionales (las que cree el alta se borran: el test cuenta desde 0)."""
    from _habilidades import onboard_tenant

    slug = f"bill19-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"BILL19 {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    await _sql(tid, "update channel_connections set deleted_at = now(), active = false where tenant_id = :t", t=tid)
    return tid


async def _conexion(tid: uuid.UUID, channel: str = "instagram", *, active: bool = True) -> uuid.UUID:
    """Una conexión viva escrita a mano (sin pasar por el enforcement): el estado de partida del test."""
    cid = uuid.uuid4()
    tipos = {"instagram": ("meta", "instagram_meta"), "whatsapp": ("meta", "whatsapp_meta"), "email": ("smtp", "email_smtp")}
    provider, type_id = tipos[channel]
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id, active) "
        "values (:id, :t, :c, :p, :ext, :ty, :a)",
        id=cid, t=tid, c=channel, p=provider, ext=f"{channel}-{uuid.uuid4().hex[:10]}", ty=type_id, a=active,
    )
    return cid


async def _contar(tid: uuid.UUID, metrica: str) -> int:
    from core.billing.metrics import count_metric

    return int(await count_metric(tid, metrica))


async def _vivas(tid: uuid.UUID) -> int:
    filas = await _sql(tid, "select count(*) from channel_connections where tenant_id = :t and deleted_at is null", t=tid)
    return int(filas[0][0])


async def _dueno(app_client, tid: uuid.UUID) -> dict[str, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno-b19')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _guardar(tid: uuid.UUID, type_id: str, creds: dict[str, Any], connection_id: uuid.UUID | None = None):
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await save_connection("prueba", creds, connection_id, type_id=type_id)


def _wa(numero: str | None = None) -> dict[str, Any]:
    return dict(_WHATSAPP, phone_number_id=numero or f"pn{uuid.uuid4().int % 10**12}")


def _correo() -> dict[str, Any]:
    return dict(_EMAIL, from_address=f"{uuid.uuid4().hex[:8]}@ejemplo.com")


# ------------------------------------------------------------------ conexiones
async def test_free_con_una_conexion_no_crea_otra() -> None:
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio("free")
    await _conexion(tid, "instagram")
    assert await _contar(tid, "conexiones") == 1

    with pytest.raises(QuotaExceeded) as exc:
        await _guardar(tid, "whatsapp_meta", _wa())
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("conexiones", 1, 1)
    assert await _vivas(tid) == 1  # no se escribió nada


async def test_free_al_tope_no_reanuda_otra_ni_habilita_el_chat_web(app_client) -> None:
    tid = await _negocio("free")
    await _conexion(tid, "instagram")
    pausada = await _conexion(tid, "whatsapp", active=False)
    h = await _dueno(app_client, tid)

    r = await app_client.post(f"/api/v1/admin/connections/{pausada}/action", headers=h, json={"action": "activate"})
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_conexiones"
    assert error["details"] == {"metric": "conexiones", "used": 1, "limit": 1}
    assert (await _sql(tid, "select active from channel_connections where id = :i", i=pausada))[0][0] is False

    r = await app_client.put("/api/v1/admin/settings/web-chat", headers=h, json={"enabled": True, "allowed_origins": []})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_limit_conexiones"), r.text
    assert await _contar(tid, "conexiones") == 1


@pytest.mark.parametrize("plan", ["free", "basic", "premium", "empresarial"])
async def test_whatsapp_y_chat_web_en_todos_los_planes(app_client, plan) -> None:
    tid = await _negocio(plan)
    h = await _dueno(app_client, tid)

    r = await app_client.put("/api/v1/admin/settings/web-chat", headers=h, json={"enabled": True, "allowed_origins": []})
    assert r.status_code == 200, r.text
    assert await _contar(tid, "conexiones") == 1
    if plan != "free":  # Free ya está al tope con el chat web
        conexion, creada = await _guardar(tid, "whatsapp_meta", _wa())
        assert creada and conexion.channel == "whatsapp"
        assert await _contar(tid, "conexiones") == 2


async def test_por_encima_del_tope_conserva_y_solo_no_crece(app_client) -> None:
    """Plan bajado: 3 conexiones en Free (tope 1). Edita y pausa las suyas; reanudar o crear otra es 409."""
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio("free")
    conexion, _ = await _guardar(tid, "whatsapp_meta", _wa())  # la primera entra
    await _conexion(tid, "instagram")
    await _conexion(tid, "whatsapp")
    assert await _contar(tid, "conexiones") == 3

    editada, creada = await _guardar(tid, "whatsapp_meta", _wa(conexion.external_id), conexion.id)
    assert (editada.id, creada, editada.active) == (conexion.id, False, True)

    h = await _dueno(app_client, tid)
    r = await app_client.post(f"/api/v1/admin/connections/{conexion.id}/action", headers=h, json={"action": "deactivate"})
    assert r.status_code == 204, r.text
    r = await app_client.post(f"/api/v1/admin/connections/{conexion.id}/action", headers=h, json={"action": "activate"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_limit_conexiones")
    assert r.json()["error"]["details"] == {"metric": "conexiones", "used": 2, "limit": 1}
    with pytest.raises(QuotaExceeded):
        await _guardar(tid, "whatsapp_meta", _wa())
    assert await _contar(tid, "conexiones") == 2  # nada se apagó solo: las dos vivas siguen


# ------------------------------------------------------------------ correos
async def test_basic_con_un_correo_no_conecta_otro(app_client) -> None:
    tid = await _negocio("basic")
    h = await _dueno(app_client, tid)
    cuerpo = {"type_id": "email_smtp", "label": "Correo", "values": _correo()}

    r = await app_client.post("/api/v1/admin/connections/manual", headers=h, json=cuerpo)
    assert r.status_code == 204, r.text
    r = await app_client.post("/api/v1/admin/connections/manual", headers=h, json={**cuerpo, "values": _correo()})
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_correos"
    assert error["details"] == {"metric": "correos", "used": 1, "limit": 1}
    assert await _contar(tid, "correos") == 1
    assert await _contar(tid, "conexiones") == 0  # el correo no suma a conexiones


async def test_free_no_conecta_ningun_correo() -> None:
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio("free")
    with pytest.raises(QuotaExceeded) as exc:
        await _guardar(tid, "email_smtp", _correo())
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("correos", 0, 0)


# ------------------------------------------------------------------ canales
async def test_free_no_conecta_telegram() -> None:
    from core.billing.canales_del_plan import CanalNoIncluido

    tid = await _negocio("free")
    with pytest.raises(CanalNoIncluido) as exc:
        await _guardar(tid, "telegram", {"bot_token": "1:AA", "bot_id": "1", "webhook_secret": "s"})
    assert exc.value.code == "plan_channel_not_included"
    assert await _vivas(tid) == 0


@pytest.mark.parametrize("canal", ["messenger", "instagram"])
async def test_basic_no_conecta_instagram_ni_messenger(app_client, monkeypatch, canal) -> None:
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "platform-app-test")
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_messenger_config_id", "platform-config")
    monkeypatch.setattr(settings, "meta_instagram_config_id", "platform-config")
    tid = await _negocio("basic")
    h = await _dueno(app_client, tid)

    r = await app_client.post(f"/api/v1/admin/meta/{canal}/attempt", headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "plan_channel_not_included"), r.text


async def test_premium_al_tope_no_abre_el_popup_de_meta(app_client, monkeypatch) -> None:
    """El tope se mide antes del OAuth: Premium con 6 conexiones (ninguna de Messenger) no abre el popup."""
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "platform-app-test")
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_messenger_config_id", "platform-config")
    tid = await _negocio("premium")
    for _ in range(6):
        await _conexion(tid, "whatsapp")
    h = await _dueno(app_client, tid)

    r = await app_client.post("/api/v1/admin/meta/messenger/attempt", headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"] == {"metric": "conexiones", "used": 6, "limit": 6}


# ------------------------------------------------------------------ habilidades

def _con_candado(monkeypatch, **min_planes: str) -> None:
    """Pone `min_plan` a packs concretos: hoy todos son Free (el candado es de superpoderes), pero el
    mecanismo (HU-BILL-19, ADR-0208 §10) sigue vivo y se prueba con un pack «de pago» simulado."""
    from core.billing import habilidades_del_plan as hdp

    real = hdp._min_planes
    monkeypatch.setattr(hdp, "_min_planes", lambda packs: {**real(packs), **{p: m for p, m in min_planes.items() if p in set(packs)}})


async def test_basic_no_habilita_cobranza_y_nada_queda_escrito(app_client, monkeypatch) -> None:
    _con_candado(monkeypatch, pack_cobranza="premium")
    from core.agents.habilitacion import asegurar_habilidades
    from core.billing.habilidades_del_plan import HabilidadNoIncluida
    from core.plugin_manager import plugin_manager
    from core.releases.packs import sync_packs
    from core.shared.tenant import tenant_scope

    await sync_packs(plugin_manager.scan())
    await plugin_manager.sync_registry()
    tid = await _negocio("basic")

    async with tenant_scope(tid):
        with pytest.raises(HabilidadNoIncluida):
            await asegurar_habilidades(tid, ["pack_cobranza"], actor=None)

    h = await _dueno(app_client, tid)
    r = await app_client.post("/api/v1/admin/agent-types/cobranza/agents", headers=h, json={})
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_plugin_not_included"
    assert error["details"] == {"skills": ["pack_cobranza"], "min_plan": "premium", "plan": "basic"}

    assert not await _sql(tid, "select 1 from plugin_configs where tenant_id = :t and plugin_id = 'pack_cobranza'", t=tid)
    assert not await _sql(
        tid, "select 1 from pack_installations where tenant_id = :t and pack_id = 'pack_cobranza'", t=tid
    )
    assert not await _sql(tid, "select 1 from agents where tenant_id = :t and key = 'cobranza'", t=tid)


# ------------------------------------------------------------------ concurrencia y aislamiento
async def test_dos_altas_simultaneas_con_un_cupo_entra_una() -> None:
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio("free")
    resultados = await asyncio.gather(
        _guardar(tid, "whatsapp_meta", _wa()), _guardar(tid, "whatsapp_meta", _wa()), return_exceptions=True
    )
    entraron = [r for r in resultados if not isinstance(r, BaseException)]
    rechazadas = [r for r in resultados if isinstance(r, QuotaExceeded)]
    assert (len(entraron), len(rechazadas)) == (1, 1), resultados
    assert await _contar(tid, "conexiones") == 1


async def test_dos_correos_simultaneos_con_un_cupo_entra_uno() -> None:
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio("basic")
    resultados = await asyncio.gather(
        _guardar(tid, "email_smtp", _correo()), _guardar(tid, "email_smtp", _correo()), return_exceptions=True
    )
    assert sorted(type(r).__name__ for r in resultados) == sorted(["tuple", QuotaExceeded.__name__]), resultados
    assert await _contar(tid, "correos") == 1


async def test_el_tope_de_un_negocio_no_ve_al_otro() -> None:
    lleno, vacio = await _negocio("free"), await _negocio("free")
    await _conexion(lleno, "instagram")
    await _conexion(lleno, "email")

    assert (await _contar(lleno, "conexiones"), await _contar(lleno, "correos")) == (1, 1)
    assert (await _contar(vacio, "conexiones"), await _contar(vacio, "correos")) == (0, 0)
    conexion, creada = await _guardar(vacio, "whatsapp_meta", _wa())  # el otro al tope no le quita su cupo
    assert creada and conexion.tenant_id == vacio
    assert await _contar(lleno, "conexiones") == 1
