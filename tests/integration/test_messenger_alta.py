"""Alta de Messenger de punta a punta (ADR-0152, HU-CHAN-01), con BD y Redis reales y Meta simulado.

Intento de un solo uso → descubrir las Páginas del usuario → conectar las elegidas. Cada Página queda con
su fila, su token cifrado, su conexión canónica y su suscripción verificada; el token del usuario se revoca.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, APP_ID_PLATAFORMA, MetaSimulado, uniq
from _api_helpers import pagina_de_meta as _pagina
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ------------------------------------------------------------------ helpers
async def _new_tenant(prefix: str, *, plan: str = "basic") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test del alta de Messenger.",
        persona={"tone": "cercano"}, admin_email=ADMIN_EMAIL, whatsapp=None,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("update tenant_subscriptions set plan_id = :p where tenant_id = :t"), {"p": plan, "t": tid}
            )
    return tid


async def _admin_id() -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        return (await session.execute(select(Admin.id).where(Admin.email == ADMIN_EMAIL))).scalar_one()


@pytest.fixture
def meta(monkeypatch):
    import core.gateway.meta_messenger_alta as alta
    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", APP_ID_PLATAFORMA)
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_messenger_config_id", "platform-config")

    async def canje(version, params, settings_, *, redirect_path="/x"):
        assert redirect_path == "/admin/conexiones/wizard/messenger"
        assert params["code"] == "codigo-del-popup"
        return "token-del-usuario", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", canje)

    def instalar(paginas, **kw) -> MetaSimulado:
        simulado = MetaSimulado(paginas, **kw)
        monkeypatch.setattr(alta, "graph_fetch", simulado.graph_fetch)
        return simulado

    return instalar


async def _descubrir(tid: uuid.UUID, admin: uuid.UUID):
    from core.gateway import meta_messenger_alta as alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        intento = await alta.iniciar_intento(tid, admin)
        return await alta.descubrir_paginas(tid, admin, "codigo-del-popup", intento)


async def _conectar(tid: uuid.UUID, admin: uuid.UUID, connection_id: uuid.UUID, page_ids: list[str]):
    from core.gateway import meta_messenger_alta as alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await alta.conectar_paginas(tid, admin, connection_id, page_ids)


# ------------------------------------------------------------------ el camino feliz
async def test_el_alta_deja_la_pagina_conectada_de_punta_a_punta(meta) -> None:
    from core.db.models import ChannelConnection, FbPage, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.sender import resolve_credentials
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_page
    from core.shared.tenant import tenant_scope

    tid, admin, page = await _new_tenant("alta-ok"), await _admin_id(), uniq("page")
    simulado = meta([_pagina(page), _pagina(uniq("sin-rol"), tasks=("ANALYZE",))])

    descubrimiento = await _descubrir(tid, admin)

    elegibles = {p.page_id: p.elegible for p in descubrimiento.paginas}
    assert elegibles[page] is True and list(elegibles.values()).count(False) == 1
    assert all(p.access_token is None for p in descubrimiento.paginas)  # los tokens no salen del servicio
    assert descubrimiento.ya_conectadas == frozenset()

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [page])

    assert [(p.page_id, p.suscrita) for p in resultado.conectadas] == [(page, True)]
    assert resultado.conectadas[0].campos == ("messages", "messaging_postbacks")
    assert resultado.omitidas == []
    # La suscripción se hizo con el token de la PÁGINA, no con el del usuario.
    assert [t for m, p, t in simulado.llamadas if p.endswith("/subscribed_apps")] == [f"tok-{page}"] * 2

    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = await session.get(MetaConnection, descubrimiento.connection_id)
            fila = (await session.execute(select(FbPage).where(FbPage.page_id == page))).scalar_one()
            canonica = (
                await session.execute(select(ChannelConnection).where(ChannelConnection.external_id == page))
            ).scalar_one()
        assert (cuenta.status, cuenta.channel, cuenta.connected_by) == ("connected", "messenger", admin)
        assert sorted(fila.subscribed_fields) == ["messages", "messaging_postbacks"]
        assert (canonica.type_id, canonica.channel, canonica.meta_connection_id) == (
            "messenger_meta", "messenger", cuenta.id,
        )
        assert canonica.credentials_encrypted is None
        # El token de la Página quedó cifrado y el del usuario, revocado.
        assert await meta_token_store.get_token(cuenta.id, "page_access", page, tenant_id=tid) == f"tok-{page}"
        assert await meta_token_store.get_token(cuenta.id, "user_access", "me", tenant_id=tid) is None
        # Con esto la Página ya envía (credenciales de la entrega anterior) y el webhook la rutea al negocio.
        proveedor, credenciales = await resolve_credentials(canonica.id, None, tid)
    assert (proveedor, credenciales["page_id"], credenciales["access_token"]) == ("meta", page, f"tok-{page}")
    assert await _resolve_tenant_for_page(page) == tid


async def test_una_segunda_alta_ve_las_paginas_ya_conectadas(meta) -> None:
    tid, admin, page = await _new_tenant("alta-dos"), await _admin_id(), uniq("page")
    meta([_pagina(page)])
    primera = await _descubrir(tid, admin)
    await _conectar(tid, admin, primera.connection_id, [page])

    segunda = await _descubrir(tid, admin)

    assert segunda.connection_id == primera.connection_id  # la misma cuenta agrupa las Páginas del negocio
    assert segunda.ya_conectadas == frozenset({page})


# ------------------------------------------------------------------ el intento
async def test_un_intento_es_de_un_solo_uso_y_de_un_solo_admin(meta) -> None:
    from core.gateway import meta_messenger_alta as alta
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("alta-intento"), await _admin_id()
    meta([_pagina(uniq("page"))])
    async with tenant_scope(tid):
        intento = await alta.iniciar_intento(tid, admin)
        await alta.descubrir_paginas(tid, admin, "codigo-del-popup", intento)
        with pytest.raises(alta.MessengerAltaError) as repetido:
            await alta.descubrir_paginas(tid, admin, "codigo-del-popup", intento)
        ajeno = await alta.iniciar_intento(tid, admin)
        with pytest.raises(alta.MessengerAltaError) as de_otro:
            await alta.descubrir_paginas(tid, uuid.uuid4(), "codigo-del-popup", ajeno)
        with pytest.raises(alta.MessengerAltaError) as inventado:
            await alta.descubrir_paginas(tid, admin, "codigo-del-popup", "inventado")

    assert {repetido.value.code, de_otro.value.code, inventado.value.code} == {"invalid_attempt"}


async def test_un_intento_de_otro_negocio_no_sirve(meta) -> None:
    from core.gateway import meta_messenger_alta as alta
    from core.shared.tenant import tenant_scope

    a, b, admin = await _new_tenant("alta-a"), await _new_tenant("alta-b"), await _admin_id()
    meta([_pagina(uniq("page"))])
    async with tenant_scope(a):
        intento = await alta.iniciar_intento(a, admin)
    async with tenant_scope(b):
        with pytest.raises(alta.MessengerAltaError) as exc:
            await alta.descubrir_paginas(b, admin, "codigo-del-popup", intento)
    assert exc.value.code == "invalid_attempt"


# ------------------------------------------------------------------ plan y configuración
async def test_un_plan_sin_messenger_no_descubre_ni_conecta(meta) -> None:
    from core.gateway import meta_messenger_alta as alta
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("alta-free", plan="free"), await _admin_id()
    meta([_pagina(uniq("page"))])
    async with tenant_scope(tid):
        intento = await alta.iniciar_intento(tid, admin)
        with pytest.raises(alta.MessengerAltaError) as al_descubrir:
            await alta.descubrir_paginas(tid, admin, "codigo-del-popup", intento)
        with pytest.raises(alta.MessengerAltaError) as al_conectar:
            await alta.conectar_paginas(tid, admin, uuid.uuid4(), ["123"])

    for exc in (al_descubrir.value, al_conectar.value):
        assert (exc.code, exc.status) == ("plan_channel_not_included", 409)


async def test_sin_la_app_de_la_plataforma_no_hay_alta(meta, monkeypatch) -> None:
    from core.config import get_settings
    from core.gateway import meta_messenger_alta as alta

    tid, admin = await _new_tenant("alta-sin-app"), await _admin_id()
    meta([])
    monkeypatch.setattr(get_settings(), "meta_messenger_config_id", "")
    with pytest.raises(alta.MessengerAltaError) as exc:
        await _descubrir(tid, admin)
    assert (exc.value.code, exc.value.status) == ("platform_app_not_configured", 409)


# ------------------------------------------------------------------ qué Páginas se pueden conectar
async def test_solo_se_conectan_las_paginas_que_el_usuario_administra_con_rol(meta) -> None:
    tid, admin = await _new_tenant("alta-roles"), await _admin_id()
    buena, sin_rol, ajena = uniq("buena"), uniq("sin-rol"), uniq("ajena")
    meta([_pagina(buena), _pagina(sin_rol, tasks=("ANALYZE", "ADVERTISE"))])
    descubrimiento = await _descubrir(tid, admin)

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [buena, sin_rol, ajena])

    assert [p.page_id for p in resultado.conectadas] == [buena]
    assert {o.page_id: o.code for o in resultado.omitidas} == {
        sin_rol: "insufficient_role", ajena: "page_not_accessible",
    }


async def test_hace_falta_mensajeria_y_un_rol_de_administracion(meta) -> None:
    from core.gateway.meta_messenger_alta import evaluar_pagina

    assert evaluar_pagina(["MESSAGING", "MANAGE"])[0]
    assert evaluar_pagina(["MESSAGING", "MODERATE"])[0]
    assert evaluar_pagina(["MESSAGING", "CREATE_CONTENT"])[0]
    assert not evaluar_pagina(["MANAGE", "MODERATE"])[0]  # sin MESSAGING
    assert not evaluar_pagina(["MESSAGING"])[0]  # sin rol de administración
    assert not evaluar_pagina([])[0]


async def test_una_pagina_de_otro_negocio_no_se_conecta_ni_deja_token(meta) -> None:
    from core.db.models import FbPage
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    duena, otra, admin = await _new_tenant("alta-duena"), await _new_tenant("alta-otra"), await _admin_id()
    page = uniq("compartida")
    meta([_pagina(page)])
    de_la_duena = await _descubrir(duena, admin)
    assert (await _conectar(duena, admin, de_la_duena.connection_id, [page])).conectadas

    de_la_otra = await _descubrir(otra, admin)
    resultado = await _conectar(otra, admin, de_la_otra.connection_id, [page])

    assert resultado.conectadas == []
    assert [(o.page_id, o.code) for o in resultado.omitidas] == [(page, "asset_claimed")]
    async with tenant_scope(otra):
        async with get_session() as session:
            assert (await session.execute(select(FbPage).where(FbPage.page_id == page))).first() is None
        assert await meta_token_store.get_token(de_la_otra.connection_id, "page_access", page, tenant_id=otra) is None
    # Y los mensajes de esa Página siguen yendo solo a su dueña.
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_page

    assert await _resolve_tenant_for_page(page) == duena


# ------------------------------------------------------------------ fallos de Meta
async def test_si_meta_no_suscribe_la_pagina_queda_conectada_pero_sin_suscribir(meta) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin, page = await _new_tenant("alta-sub"), await _admin_id(), uniq("page")
    meta([_pagina(page)], suscripcion_falla={page})
    descubrimiento = await _descubrir(tid, admin)

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [page])

    assert [(p.page_id, p.suscrita, p.campos) for p in resultado.conectadas] == [(page, False, ())]
    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = await session.get(MetaConnection, descubrimiento.connection_id)
    assert cuenta.status == "pending"  # no se da por conectada una cuenta sin ninguna Página suscrita


async def test_si_el_canje_falla_no_se_crea_nada_que_ruteé(meta, monkeypatch) -> None:
    import core.gateway.meta_onboarding as onboarding
    from core.gateway import meta_messenger_alta as alta
    from core.gateway.meta_errors import GraphError

    tid, admin = await _new_tenant("alta-canje"), await _admin_id()
    meta([])

    async def rechazado(*_a, **_k):
        raise GraphError("(#100) This authorization code has expired", category="PERMANENT", code=100)

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", rechazado)
    with pytest.raises(alta.MessengerAltaError) as exc:
        await _descubrir(tid, admin)
    assert exc.value.code == "meta_error"


async def test_sin_token_del_usuario_hay_que_volver_a_conectar(meta) -> None:
    from core.gateway import meta_messenger_alta as alta
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    tid, admin, page = await _new_tenant("alta-vencido"), await _admin_id(), uniq("page")
    meta([_pagina(page)])
    descubrimiento = await _descubrir(tid, admin)
    async with tenant_scope(tid):
        await meta_token_store.revoke_token(descubrimiento.connection_id, "user_access", "me", tenant_id=tid)

    with pytest.raises(alta.MessengerAltaError) as exc:
        await _conectar(tid, admin, descubrimiento.connection_id, [page])
    assert exc.value.code == "user_token_expired"


async def test_una_cuenta_de_otro_negocio_no_se_puede_usar(meta) -> None:
    from core.gateway import meta_messenger_alta as alta

    a, b, admin = await _new_tenant("alta-cuenta-a"), await _new_tenant("alta-cuenta-b"), await _admin_id()
    meta([_pagina(uniq("page"))])
    de_a = await _descubrir(a, admin)

    with pytest.raises(alta.MessengerAltaError) as exc:
        await _conectar(b, admin, de_a.connection_id, ["123"])
    assert (exc.value.code, exc.value.status) == ("not_found", 404)
