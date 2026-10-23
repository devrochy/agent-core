"""Alta de Instagram con Facebook Login de punta a punta (ADR-0152, HU-CHAN-04), con BD y Redis reales y Meta simulado.

Intento de un solo uso → descubrir las cuentas de Instagram vinculadas a las Páginas del usuario → conectar las
elegidas. Cada cuenta queda con su fila, el token cifrado de su Página vinculada, su conexión canónica y su
suscripción verificada; el token del usuario se revoca. Una cuenta tiene un solo dueño.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, APP_ID_PLATAFORMA, MetaSimulado, pagina_con_instagram, uniq
from sqlalchemy import select, text, update

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
        name=f"Tenant {slug}", slug=slug, business_description="Test del alta de Instagram.",
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
    import core.gateway.meta_instagram_alta as alta
    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", APP_ID_PLATAFORMA)
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_instagram_config_id", "ig-config")

    async def canje(version, params, settings_, *, redirect_path="/x"):
        assert redirect_path == "/admin/conexiones/wizard/instagram"
        assert params["code"] == "codigo-del-popup"
        return "token-del-usuario", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", canje)

    def instalar(paginas, **kw) -> MetaSimulado:
        simulado = MetaSimulado(paginas, **kw)
        monkeypatch.setattr(alta, "graph_fetch", simulado.graph_fetch)
        return simulado

    return instalar


async def _descubrir(tid: uuid.UUID, admin: uuid.UUID):
    from core.gateway import meta_instagram_alta as alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        intento = await alta.iniciar_intento(tid, admin)
        return await alta.descubrir_cuentas(tid, admin, "codigo-del-popup", intento)


async def _conectar(tid: uuid.UUID, admin: uuid.UUID, connection_id: uuid.UUID, ig_user_ids: list[str]):
    from core.gateway import meta_instagram_alta as alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await alta.conectar_cuentas(tid, admin, connection_id, ig_user_ids)


def _ids() -> tuple[str, str]:
    n = uuid.uuid4().int
    return str(n % 10**15), str(n // 10**15 % 10**17)


# ------------------------------------------------------------------ el camino feliz
async def test_el_alta_deja_la_cuenta_conectada_de_punta_a_punta(meta) -> None:
    from core.db.models import ChannelConnection, IgAccount, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.sender import resolve_credentials
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_ig_user
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-ok"), await _admin_id()
    page, ig = _ids()
    sin_ig = uniq("sin-ig")
    simulado = meta([pagina_con_instagram(page, ig, username="mitienda"), {"id": sin_ig, "name": "Sin IG", "tasks": ["MANAGE"]}])

    descubrimiento = await _descubrir(tid, admin)

    assert [(c.ig_user_id, c.username, c.page_id, c.elegible) for c in descubrimiento.cuentas] == [
        (ig, "mitienda", page, True)
    ]
    assert descubrimiento.paginas_sin_instagram == 1
    assert all(c.page_access_token is None for c in descubrimiento.cuentas)  # los tokens no salen del servicio

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])

    assert [(c.ig_user_id, c.suscrita) for c in resultado.conectadas] == [(ig, True)]
    assert resultado.omitidas == []
    # La suscripción se hizo con el token de la PÁGINA vinculada.
    assert {t for m, p, t in simulado.llamadas if p.endswith("/subscribed_apps")} == {f"tok-{page}"}

    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = await session.get(MetaConnection, descubrimiento.connection_id)
            fila = (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalar_one()
            canonica = (
                await session.execute(select(ChannelConnection).where(ChannelConnection.external_id == ig))
            ).scalar_one()
        assert (cuenta.status, cuenta.channel, cuenta.connected_by) == ("connected", "instagram", admin)
        assert (fila.username, fila.linked_page_id, fila.login_route) == ("mitienda", page, "ig_facebook_login")
        assert "messages" in fila.raw["subscribed_fields"]
        assert (canonica.type_id, canonica.channel, canonica.meta_connection_id) == (
            "instagram_meta", "instagram", cuenta.id,
        )
        assert canonica.label == "@mitienda" and canonica.credentials_encrypted is None
        # El token de la Página vinculada quedó cifrado y el del usuario, revocado.
        assert await meta_token_store.get_token(cuenta.id, "page_access", page, tenant_id=tid) == f"tok-{page}"
        assert await meta_token_store.get_token(cuenta.id, "user_access", "me", tenant_id=tid) is None
        # Con esto la cuenta ya envía por su Página y el webhook la rutea al negocio por el `entry.id`.
        proveedor, credenciales = await resolve_credentials(canonica.id, None, tid)
    assert (proveedor, credenciales["page_id"], credenciales["access_token"]) == ("meta", page, f"tok-{page}")
    assert await _resolve_tenant_for_ig_user(ig) == tid


async def test_una_segunda_alta_ve_las_cuentas_ya_conectadas(meta) -> None:
    tid, admin = await _new_tenant("ig-dos"), await _admin_id()
    page, ig = _ids()
    meta([pagina_con_instagram(page, ig)])
    primera = await _descubrir(tid, admin)
    await _conectar(tid, admin, primera.connection_id, [ig])

    segunda = await _descubrir(tid, admin)

    assert segunda.connection_id == primera.connection_id
    assert segunda.ya_conectadas == frozenset({ig})


async def test_reconectar_la_misma_cuenta_no_duplica_nada(meta) -> None:
    from core.db.models import ChannelConnection, IgAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-repite"), await _admin_id()
    page, ig = _ids()
    meta([pagina_con_instagram(page, ig)])
    primera = await _descubrir(tid, admin)
    await _conectar(tid, admin, primera.connection_id, [ig])
    segunda = await _descubrir(tid, admin)
    await _conectar(tid, admin, segunda.connection_id, [ig])

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalars().all()
            canonicas = (
                await session.execute(select(ChannelConnection).where(ChannelConnection.external_id == ig))
            ).scalars().all()
    assert len(filas) == 1 and len(canonicas) == 1


# ------------------------------------------------------------------ una cuenta, un dueño
async def test_otro_negocio_no_puede_conectar_una_cuenta_ya_conectada(meta) -> None:
    from core.db.models import IgAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_ig_user
    from core.shared.tenant import tenant_scope

    a, b, admin = await _new_tenant("ig-dueno-a"), await _new_tenant("ig-dueno-b"), await _admin_id()
    page, ig = _ids()
    meta([pagina_con_instagram(page, ig)])
    da = await _descubrir(a, admin)
    await _conectar(a, admin, da.connection_id, [ig])

    db = await _descubrir(b, admin)
    resultado = await _conectar(b, admin, db.connection_id, [ig])

    assert resultado.conectadas == []
    assert [(o.ig_user_id, o.code) for o in resultado.omitidas] == [(ig, "asset_claimed")]
    # El mensaje no dice de quién es, el ruteo sigue siendo de A y B no se quedó ni con el token.
    assert ig not in resultado.omitidas[0].motivo
    assert await _resolve_tenant_for_ig_user(ig) == a
    async with tenant_scope(b):
        async with get_session() as session:
            assert (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).first() is None
        assert await meta_token_store.get_token(db.connection_id, "page_access", page, tenant_id=b) is None


# ------------------------------------------------------------------ qué cuentas se pueden conectar
async def test_solo_se_conectan_las_cuentas_que_el_usuario_administra_con_rol(meta) -> None:
    tid, admin = await _new_tenant("ig-roles"), await _admin_id()
    (p_ok, ig_ok), (p_sin, ig_sin), (_p_ajena, ig_ajena) = _ids(), _ids(), _ids()
    meta([pagina_con_instagram(p_ok, ig_ok), pagina_con_instagram(p_sin, ig_sin, tasks=("ANALYZE",))])

    descubrimiento = await _descubrir(tid, admin)
    elegibles = {c.ig_user_id: c.elegible for c in descubrimiento.cuentas}
    assert elegibles == {ig_ok: True, ig_sin: False}

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig_ok, ig_sin, ig_ajena])

    assert [c.ig_user_id for c in resultado.conectadas] == [ig_ok]
    assert {(o.ig_user_id, o.code) for o in resultado.omitidas} == {
        (ig_sin, "insufficient_role"), (ig_ajena, "account_not_accessible"),
    }


async def test_si_meta_no_confirma_la_suscripcion_la_cuenta_queda_sin_suscribir(meta) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-sinsub"), await _admin_id()
    page, ig = _ids()
    meta([pagina_con_instagram(page, ig)], suscripcion_falla={page})
    descubrimiento = await _descubrir(tid, admin)

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])

    assert [(c.ig_user_id, c.suscrita, c.campos) for c in resultado.conectadas] == [(ig, False, ())]
    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = await session.get(MetaConnection, descubrimiento.connection_id)
    assert cuenta.status == "pending"  # nunca "connected" sin una suscripción confirmada


async def test_no_quita_los_campos_que_la_pagina_ya_tenia_suscritos(meta) -> None:
    """`POST subscribed_fields` reemplaza la lista: la Página ya suscrita por Messenger conserva sus campos."""
    tid, admin = await _new_tenant("ig-union"), await _admin_id()
    page, ig = _ids()
    simulado = meta(
        [pagina_con_instagram(page, ig)], campos_previos={page: ["messages", "message_echoes", "messaging_handovers"]}
    )
    descubrimiento = await _descubrir(tid, admin)

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])

    pedidos = set(simulado.pedidos[0].split(","))
    assert {"message_echoes", "messaging_handovers"} <= pedidos  # los de Messenger
    assert {"messages", "messaging_seen", "message_reactions"} <= pedidos  # los de Instagram
    assert resultado.conectadas[0].suscrita is True


async def test_sin_el_campo_messages_no_se_da_por_suscrita(meta) -> None:
    tid, admin = await _new_tenant("ig-sinmsg"), await _admin_id()
    page, ig = _ids()
    simulado = meta([pagina_con_instagram(page, ig)])
    descubrimiento = await _descubrir(tid, admin)
    # Meta confirma otros campos pero no `messages`: los mensajes directos no llegarían.
    original = simulado.graph_fetch

    async def sin_messages(path, **kw):
        datos = await original(path, **kw)
        if path.endswith("/subscribed_apps") and kw.get("method", "GET") == "GET":
            return {"data": [{"id": APP_ID_PLATAFORMA, "subscribed_fields": ["messaging_seen"]}]}
        return datos

    import core.gateway.meta_instagram_alta as alta

    alta.graph_fetch = sin_messages
    try:
        resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])
    finally:
        alta.graph_fetch = original
    assert resultado.conectadas[0].suscrita is False


# ------------------------------------------------------------------ el intento, el plan y la configuración
async def test_un_intento_de_messenger_no_sirve_para_instagram(meta) -> None:
    from core.gateway import meta_instagram_alta as alta
    from core.gateway import meta_messenger_alta as messenger
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-intento"), await _admin_id()
    meta([pagina_con_instagram(*_ids())])
    async with tenant_scope(tid):
        de_messenger = await messenger.iniciar_intento(tid, admin)
        with pytest.raises(alta.InstagramAltaError) as exc:
            await alta.descubrir_cuentas(tid, admin, "codigo-del-popup", de_messenger)
        propio = await alta.iniciar_intento(tid, admin)
        await alta.descubrir_cuentas(tid, admin, "codigo-del-popup", propio)
        with pytest.raises(alta.InstagramAltaError) as repetido:
            await alta.descubrir_cuentas(tid, admin, "codigo-del-popup", propio)
    assert exc.value.code == repetido.value.code == "invalid_attempt"


async def test_un_plan_sin_instagram_no_descubre_ni_conecta(meta) -> None:
    from core.gateway import meta_instagram_alta as alta
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-free", plan="free"), await _admin_id()
    meta([pagina_con_instagram(*_ids())])
    async with tenant_scope(tid):
        intento = await alta.iniciar_intento(tid, admin)
        with pytest.raises(alta.InstagramAltaError) as al_descubrir:
            await alta.descubrir_cuentas(tid, admin, "codigo-del-popup", intento)
        with pytest.raises(alta.InstagramAltaError) as al_conectar:
            await alta.conectar_cuentas(tid, admin, uuid.uuid4(), ["123"])
    for exc in (al_descubrir.value, al_conectar.value):
        assert (exc.code, exc.status) == ("plan_channel_not_included", 409)


async def test_todos_los_planes_de_pago_incluyen_instagram() -> None:
    from core.gateway.meta_instagram_alta import plan_incluye_instagram

    assert [plan_incluye_instagram(p) for p in ("free", "basic", "premium", "empresarial")] == [False, True, True, True]


async def test_sin_la_configuracion_de_instagram_no_hay_alta(meta, monkeypatch) -> None:
    from core.config import get_settings
    from core.gateway import meta_instagram_alta as alta

    tid, admin = await _new_tenant("ig-sin-config"), await _admin_id()
    meta([])
    monkeypatch.setattr(get_settings(), "meta_instagram_config_id", "")
    # No cae a la de Messenger ni a la de WhatsApp: son otros permisos.
    monkeypatch.setattr(get_settings(), "meta_messenger_config_id", "config-de-messenger")
    monkeypatch.setattr(get_settings(), "meta_config_id", "config-de-whatsapp")
    with pytest.raises(alta.InstagramAltaError) as exc:
        await _descubrir(tid, admin)
    assert (exc.value.code, exc.value.status) == ("platform_app_not_configured", 409)


# ------------------------------------------------------------------ credenciales de envío
async def _cuenta_conectada(meta, tid: uuid.UUID, admin: uuid.UUID):
    page, ig = _ids()
    meta([pagina_con_instagram(page, ig)])
    descubrimiento = await _descubrir(tid, admin)
    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])
    return page, ig, descubrimiento.connection_id, resultado.conectadas[0].channel_connection_id


async def _credenciales(tid: uuid.UUID, canonica: uuid.UUID, esperado: uuid.UUID | None = None):
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await resolve_credentials(canonica, None, esperado if esperado is not None else tid)


async def test_la_cuenta_pausada_o_eliminada_no_envia(meta) -> None:
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-pausa"), await _admin_id()
    _page, _ig, cuenta, canonica = await _cuenta_conectada(meta, tid, admin)
    assert (await _credenciales(tid, canonica))[1] != {}

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(update(MetaConnection).where(MetaConnection.id == cuenta).values(status="revoked"))
    assert await _credenciales(tid, canonica) == ("meta", {})

    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(MetaConnection).where(MetaConnection.id == cuenta).values(status="connected", deleted_at=utcnow())
            )
    assert await _credenciales(tid, canonica) == ("meta", {})


async def test_sin_el_token_de_la_pagina_vinculada_no_hay_credenciales(meta) -> None:
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-sintoken"), await _admin_id()
    page, _ig, cuenta, canonica = await _cuenta_conectada(meta, tid, admin)
    async with tenant_scope(tid):
        await meta_token_store.revoke_token(cuenta, "page_access", page, tenant_id=tid)
    assert await _credenciales(tid, canonica) == ("meta", {})


async def test_el_tenant_de_la_conversacion_debe_ser_el_de_la_cuenta(meta) -> None:
    tid, admin = await _new_tenant("ig-dueno"), await _admin_id()
    _page, _ig, _cuenta, canonica = await _cuenta_conectada(meta, tid, admin)
    assert await _credenciales(tid, canonica, esperado=uuid.uuid4()) == ("meta", {})


async def test_una_cuenta_de_instagram_no_usa_nunca_las_credenciales_de_whatsapp(meta) -> None:
    tid, admin = await _new_tenant("ig-wa"), await _admin_id()
    _page, _ig, _cuenta, canonica = await _cuenta_conectada(meta, tid, admin)
    _proveedor, credenciales = await _credenciales(tid, canonica)
    assert "phone_number_id" not in credenciales


# ------------------------------------------------------------------ dos cuentas, mismo identificador de cliente
async def test_el_mismo_igsid_en_dos_cuentas_del_negocio_son_dos_conversaciones(meta) -> None:
    """El IGSID que llega a dos cuentas profesionales del mismo negocio no mezcla clientes ni conversaciones, y cada
    conversación responde por SU cuenta (su conexión canónica y la Página vinculada a ella)."""
    from core.gateway.channels.whatsapp.inbound import WhatsAppMetaInbound
    from core.gateway.sender import resolve_credentials
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-dos-cuentas"), await _admin_id()
    (p1, ig1), (p2, ig2) = _ids(), _ids()
    meta([pagina_con_instagram(p1, ig1), pagina_con_instagram(p2, ig2)])
    descubrimiento = await _descubrir(tid, admin)
    await _conectar(tid, admin, descubrimiento.connection_id, [ig1, ig2])

    igsid = uniq("igsid")
    conversaciones = {}
    for ig in (ig1, ig2):
        (mensaje,) = WhatsAppMetaInbound().parse_inbound({
            "object": "instagram",
            "entry": [{"id": ig, "messaging": [{
                "sender": {"id": igsid}, "recipient": {"id": ig}, "message": {"mid": uniq("mid"), "text": "hola"},
            }]}],
        })
        async with tenant_scope(tid):
            conversacion, cliente, _ = await consumer._resolve_conversation({
                "channel": mensaje.channel, "customer_external_id": mensaje.customer_external_id,
                "connection_external_id": mensaje.connection_external_id,
            })
            _, credenciales = await resolve_credentials(conversacion.connection_id, None, tid)
        conversaciones[ig] = (conversacion.id, cliente.id, credenciales["page_id"])

    (conv1, cliente1, pagina1), (conv2, cliente2, pagina2) = conversaciones[ig1], conversaciones[ig2]
    assert conv1 != conv2 and cliente1 != cliente2
    assert (pagina1, pagina2) == (p1, p2)  # cada conversación responde por la Página de SU cuenta


# ------------------------------------------------------------------ conflicto con una conexión manual
async def test_una_conexion_manual_con_el_mismo_identificador_se_omite_y_no_deja_nada(meta) -> None:
    from core.db.models import ChannelConnection, IgAccount
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("ig-manual"), await _admin_id()
    page, ig = _ids()
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                ChannelConnection(
                    tenant_id=tid, channel="instagram", provider="meta", type_id="instagram_meta",
                    category="communication", label="manual", external_id=ig, credentials_encrypted="x",
                )
            )
    meta([pagina_con_instagram(page, ig)])
    descubrimiento = await _descubrir(tid, admin)

    resultado = await _conectar(tid, admin, descubrimiento.connection_id, [ig])

    assert resultado.conectadas == []
    assert [(o.ig_user_id, o.code) for o in resultado.omitidas] == [(ig, "manual_connection_conflict")]
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).first() is None
        # Ni fila ni token: el reclamo se deshizo y el token solo se guarda si todo lo demás salió bien.
        assert await meta_token_store.get_token(descubrimiento.connection_id, "page_access", page, tenant_id=tid) is None
