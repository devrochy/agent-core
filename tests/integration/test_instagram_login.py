"""Instagram Login de punta a punta (ruta b de HU-CHAN-04, ADR-0152), con BD y Redis reales y Meta simulado.

`iniciar_login` → `completar_login` → la cuenta queda con su fila, su token de 60 días cifrado, su conexión canónica y su
suscripción verificada. Y el refresco diario: a quién toca, a quién no, y qué pasa cuando Meta lo rechaza.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from _api_helpers import ADMIN_EMAIL, uniq
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

APP_IG = "ig-app-test"


# ------------------------------------------------------------------ helpers
async def _new_tenant(prefix: str, *, plan: str = "basic") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq(prefix)
    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}", slug=slug, business_description="Test de Instagram Login.",
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


def _ig_id() -> str:
    return str(uuid.uuid4().int % 10**17)


class MetaIG:
    """Instagram simulado: qué cuenta autoriza cada código y qué responde la suscripción."""

    def __init__(self, monkeypatch):
        import core.gateway.meta_instagram_login_alta as alta
        from core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "instagram_app_id", APP_IG)
        monkeypatch.setattr(settings, "instagram_app_secret", "ig-secreto-test")
        monkeypatch.setattr(settings, "instagram_login_redirect_uri", "https://panel.test/conexiones/instagram/callback")
        monkeypatch.setattr(settings, "instagram_login_enabled", True)
        self.cuentas: dict[str, tuple[str, str]] = {}  # código → (ig_user_id, username)
        self.suscribe = (True, ("messages", "messaging_seen"))
        self.llamadas: list[str] = []

        async def canjear_codigo(code):
            self.llamadas.append("canjear_codigo")
            ig, _ = self.cuentas[code]
            return f"corto-{code}", ig

        async def token_largo(corto):
            self.llamadas.append("token_largo")
            return f"largo-{corto}", datetime.now(UTC) + timedelta(days=60)

        async def datos(token):
            code = token.removeprefix("largo-corto-")
            ig, user = self.cuentas[code]
            return ig, user

        async def suscribir(ig_user_id, token, version):
            self.llamadas.append("suscribir")
            return self.suscribe

        monkeypatch.setattr(alta, "canjear_codigo", canjear_codigo)
        monkeypatch.setattr(alta, "canjear_por_token_largo", token_largo)
        monkeypatch.setattr(alta, "datos_de_la_cuenta", datos)
        monkeypatch.setattr(alta, "suscribir_y_verificar", suscribir)

    def autoriza(self, ig_user_id: str, username: str = "mitienda") -> str:
        code = uuid.uuid4().hex[:12]
        self.cuentas[code] = (ig_user_id, username)
        return code


@pytest.fixture
def meta_ig(monkeypatch) -> MetaIG:
    return MetaIG(monkeypatch)


async def _login(tid: uuid.UUID, admin: uuid.UUID, code: str):
    from core.gateway import meta_instagram_login_alta as alta
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        iniciado = await alta.iniciar_login(tid, admin)
        state = parse_qs(urlparse(iniciado.url).query)["state"][0]
        return await alta.completar_login(tid, admin, code, state)


# ------------------------------------------------------------------ el camino feliz
async def test_el_login_deja_la_cuenta_conectada_de_punta_a_punta(meta_ig) -> None:
    from core.db.models import ChannelConnection, IgAccount, MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.meta_instagram import estado_de
    from core.gateway.sender import resolve_credentials
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_ig_user
    from core.shared.tenant import tenant_scope

    tid, admin, ig = await _new_tenant("igl-ok"), await _admin_id(), _ig_id()

    cuenta = await _login(tid, admin, meta_ig.autoriza(ig, "mitienda"))

    assert (cuenta.ig_user_id, cuenta.username, cuenta.suscrita) == (ig, "mitienda", True)
    assert meta_ig.llamadas == ["canjear_codigo", "token_largo", "suscribir"]
    async with tenant_scope(tid):
        async with get_session() as session:
            conexion = await session.get(MetaConnection, cuenta.connection_id)
            fila = (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalar_one()
            canonica = (
                await session.execute(select(ChannelConnection).where(ChannelConnection.external_id == ig))
            ).scalar_one()
        # La cuenta de Meta es la de la app de Instagram, no la de Facebook Login.
        assert (conexion.channel, conexion.meta_app_id, conexion.status, conexion.connected_by) == (
            "instagram", APP_IG, "connected", admin,
        )
        assert (fila.username, fila.login_route, fila.linked_page_id) == ("mitienda", "ig_instagram_login", None)
        assert estado_de(fila.raw) == ("connected", None) and "messages" in fila.raw["subscribed_fields"]
        assert (canonica.type_id, canonica.channel, canonica.meta_connection_id) == (
            "instagram_meta", "instagram", conexion.id,
        )
        token = await meta_token_store.get_token(conexion.id, "long_lived_user", ig, tenant_id=tid)
        vence = await meta_token_store.get_token_expires_at(conexion.id, "long_lived_user", ig, tenant_id=tid)
        # Se envía con el token de la propia cuenta, no con el de una Página.
        proveedor, credenciales = await resolve_credentials(canonica.id, None, tid)
    assert token and token.startswith("largo-corto-")
    assert timedelta(days=59) < vence - datetime.now(UTC) <= timedelta(days=60)
    assert (proveedor, credenciales["login_route"], credenciales["ig_user_id"], credenciales["access_token"]) == (
        "meta", "ig_instagram_login", ig, token,
    )
    assert "page_id" not in credenciales
    assert await _resolve_tenant_for_ig_user(ig) == tid


async def test_el_state_es_de_un_solo_uso_y_de_un_solo_admin_y_negocio(meta_ig) -> None:
    from core.gateway import meta_instagram_login_alta as alta
    from core.shared.tenant import tenant_scope

    a, b, admin = await _new_tenant("igl-state-a"), await _new_tenant("igl-state-b"), await _admin_id()
    code = meta_ig.autoriza(_ig_id())
    async with tenant_scope(a):
        state = parse_qs(urlparse((await alta.iniciar_login(a, admin)).url).query)["state"][0]
        await alta.completar_login(a, admin, code, state)
        with pytest.raises(alta.InstagramAltaError) as repetido:
            await alta.completar_login(a, admin, code, state)
        otro_admin = parse_qs(urlparse((await alta.iniciar_login(a, admin)).url).query)["state"][0]
        with pytest.raises(alta.InstagramAltaError) as de_otro:
            await alta.completar_login(a, uuid.uuid4(), code, otro_admin)
        with pytest.raises(alta.InstagramAltaError) as inventado:
            await alta.completar_login(a, admin, code, "inventado")
    async with tenant_scope(b):
        ajeno = await alta.iniciar_login(b, admin)
    async with tenant_scope(a):
        with pytest.raises(alta.InstagramAltaError) as de_otro_negocio:
            await alta.completar_login(a, admin, code, parse_qs(urlparse(ajeno.url).query)["state"][0])
    codigos = {repetido.value.code, de_otro.value.code, inventado.value.code, de_otro_negocio.value.code}
    assert codigos == {"invalid_attempt"}


async def test_un_intento_de_facebook_login_no_sirve_para_instagram_login(meta_ig) -> None:
    from core.gateway import meta_instagram_alta as facebook_login
    from core.gateway import meta_instagram_login_alta as alta
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("igl-cruce"), await _admin_id()
    code = meta_ig.autoriza(_ig_id())
    async with tenant_scope(tid):
        de_facebook = await facebook_login.iniciar_intento(tid, admin)
        with pytest.raises(alta.InstagramAltaError) as exc:
            await alta.completar_login(tid, admin, code, de_facebook)
    assert exc.value.code == "invalid_attempt"
    assert meta_ig.llamadas == []  # no se canjeó nada


# ------------------------------------------------------------------ disponibilidad
async def test_apagada_o_sin_configuracion_no_hay_login(meta_ig, monkeypatch) -> None:
    from core.config import get_settings
    from core.gateway import meta_instagram_login_alta as alta

    tid, admin = await _new_tenant("igl-off"), await _admin_id()
    monkeypatch.setattr(get_settings(), "instagram_login_enabled", False)
    with pytest.raises(alta.InstagramAltaError) as apagada:
        await alta.iniciar_login(tid, admin)
    monkeypatch.setattr(get_settings(), "instagram_login_enabled", True)
    monkeypatch.setattr(get_settings(), "instagram_app_secret", "")
    with pytest.raises(alta.InstagramAltaError) as sin_secreto:
        await alta.iniciar_login(tid, admin)
    for exc in (apagada.value, sin_secreto.value):
        assert (exc.code, exc.status) == ("instagram_login_unavailable", 409)


async def test_un_plan_sin_instagram_no_arranca_el_login(meta_ig) -> None:
    from core.gateway import meta_instagram_login_alta as alta
    from core.shared.tenant import tenant_scope

    tid, admin = await _new_tenant("igl-free", plan="free"), await _admin_id()
    async with tenant_scope(tid):
        with pytest.raises(alta.InstagramAltaError) as exc:
            await alta.iniciar_login(tid, admin)
    assert (exc.value.code, exc.value.status) == ("plan_channel_not_included", 409)


# ------------------------------------------------------------------ una cuenta, un dueño
async def test_otro_negocio_no_puede_conectar_una_cuenta_ya_conectada_por_instagram_login(meta_ig) -> None:
    from core.db.models import IgAccount
    from core.db.session import get_session
    from core.gateway import meta_instagram_login_alta as alta
    from core.gateway.webhook_route_tenant import _resolve_tenant_for_ig_user
    from core.shared.tenant import tenant_scope

    a, b, admin, ig = await _new_tenant("igl-dueno-a"), await _new_tenant("igl-dueno-b"), await _admin_id(), _ig_id()
    await _login(a, admin, meta_ig.autoriza(ig))

    with pytest.raises(alta.InstagramAltaError) as exc:
        await _login(b, admin, meta_ig.autoriza(ig))

    assert (exc.value.code, exc.value.status) == ("asset_claimed", 409)
    assert await _resolve_tenant_for_ig_user(ig) == a
    async with tenant_scope(b):
        async with get_session() as session:
            assert (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).first() is None


async def test_la_misma_cuenta_conectada_dos_veces_no_duplica_nada(meta_ig) -> None:
    from core.db.models import ChannelConnection, IgAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin, ig = await _new_tenant("igl-repite"), await _admin_id(), _ig_id()
    await _login(tid, admin, meta_ig.autoriza(ig))
    await _login(tid, admin, meta_ig.autoriza(ig))

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalars().all()
            canonicas = (
                await session.execute(select(ChannelConnection).where(ChannelConnection.external_id == ig))
            ).scalars().all()
    assert len(filas) == 1 and len(canonicas) == 1


async def test_una_conexion_manual_con_el_mismo_identificador_se_omite_y_no_deja_nada(meta_ig) -> None:
    from core.db.models import ChannelConnection, IgAccount
    from core.db.session import get_session
    from core.gateway import meta_instagram_login_alta as alta
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    tid, admin, ig = await _new_tenant("igl-manual"), await _admin_id(), _ig_id()
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(
                ChannelConnection(
                    tenant_id=tid, channel="instagram", provider="meta", type_id="instagram_meta",
                    category="communication", label="manual", external_id=ig, credentials_encrypted="x",
                )
            )
    with pytest.raises(alta.InstagramAltaError) as exc:
        await _login(tid, admin, meta_ig.autoriza(ig))

    assert exc.value.code == "manual_connection_conflict"
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).first() is None
            conexion = await alta._cuenta_del_negocio(tid, __import__("core.config", fromlist=["x"]).get_settings())
        assert await meta_token_store.get_token(conexion.id, "long_lived_user", ig, tenant_id=tid) is None


async def test_si_la_suscripcion_no_se_confirma_la_cuenta_queda_guardada_pero_no_conectada(meta_ig) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, admin, ig = await _new_tenant("igl-sinsub"), await _admin_id(), _ig_id()
    meta_ig.suscribe = (False, ())

    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))

    assert (cuenta.suscrita, cuenta.campos) == (False, ())
    async with tenant_scope(tid):
        async with get_session() as session:
            conexion = await session.get(MetaConnection, cuenta.connection_id)
    assert conexion.status == "pending"  # nunca «connected» sin una suscripción confirmada


# ------------------------------------------------------------------ credenciales
async def test_un_token_vencido_o_una_cuenta_pausada_no_envian(meta_ig) -> None:
    from sqlalchemy import update

    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_token_store
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, admin, ig = await _new_tenant("igl-cred"), await _admin_id(), _ig_id()
    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))
    async with tenant_scope(tid):
        assert (await resolve_credentials(cuenta.channel_connection_id, None, tid))[1] != {}
        # Vencido: sin credenciales (el envío falla con claridad), no se manda con un token muerto.
        await meta_token_store.put_token(
            cuenta.connection_id, "long_lived_user", ig, "token-viejo",
            expires_at=datetime.now(UTC) - timedelta(minutes=1), tenant_id=tid,
        )
        assert await resolve_credentials(cuenta.channel_connection_id, None, tid) == ("meta", {})
        await meta_token_store.put_token(
            cuenta.connection_id, "long_lived_user", ig, "token-nuevo",
            expires_at=datetime.now(UTC) + timedelta(days=60), tenant_id=tid,
        )
        assert (await resolve_credentials(cuenta.channel_connection_id, None, tid))[1]["access_token"] == "token-nuevo"
        async with get_session() as session:
            await session.execute(update(MetaConnection).where(MetaConnection.id == cuenta.connection_id).values(status="revoked"))
        assert await resolve_credentials(cuenta.channel_connection_id, None, tid) == ("meta", {})
        assert await resolve_credentials(cuenta.channel_connection_id, None, uuid.uuid4()) == ("meta", {})


# ------------------------------------------------------------------ el refresco diario
async def _cuenta_con_token(meta_ig, tid, admin, *, emitido_hace: timedelta):
    """Una cuenta de Instagram Login cuyo token se emitió hace `emitido_hace` (vence a los 60 días de emitido)."""
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    ig = _ig_id()
    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))
    async with tenant_scope(tid):
        await meta_token_store.put_token(
            cuenta.connection_id, "long_lived_user", ig, f"token-de-{ig}",
            expires_at=datetime.now(UTC) - emitido_hace + timedelta(days=60), tenant_id=tid,
        )
    return ig, cuenta.connection_id


async def _estado(tid, connection_id, ig):
    from core.db.models import IgAccount
    from core.db.session import get_session
    from core.gateway.meta_instagram import estado_de
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (
                await session.execute(
                    select(IgAccount).where(IgAccount.connection_id == connection_id, IgAccount.ig_user_id == ig)
                )
            ).scalar_one()
    return estado_de(fila.raw)


async def _vence(tid, connection_id, ig):
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await meta_token_store.get_token_expires_at(connection_id, "long_lived_user", ig, tenant_id=tid)


async def test_el_refresco_solo_toca_los_tokens_por_vencer_con_al_menos_24_horas(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea

    tid, admin = await _new_tenant("igl-ref"), await _admin_id()
    por_vencer, connection_a = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(days=55))
    reciente, connection_b = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(hours=3))
    lejano, connection_c = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(days=20))
    refrescados: list[str] = []

    async def refrescar(token):
        refrescados.append(token)
        return f"{token}-renovado", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)

    resumen = await tarea.refresh_instagram_tokens()

    # El refresco recorre a todos los negocios: puede tocar cuentas de otras pruebas, pero de estas tres solo la que está
    # en la ventana de 10 días y tiene al menos 24 h.
    assert f"token-de-{por_vencer}" in refrescados
    assert f"token-de-{reciente}" not in refrescados and f"token-de-{lejano}" not in refrescados
    assert resumen["refrescadas"] >= 1
    assert (await _vence(tid, connection_a, por_vencer)) > datetime.now(UTC) + timedelta(days=59)
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):  # el de 3 h no se tocó: Meta rechaza refrescar uno de menos de 24 h
        assert await meta_token_store.get_token(connection_b, "long_lived_user", reciente, tenant_id=tid) == f"token-de-{reciente}"
    assert (await _vence(tid, connection_c, lejano)) < datetime.now(UTC) + timedelta(days=42)  # sin tocar (aún lejos)


async def test_si_meta_rechaza_el_refresco_la_cuenta_queda_degradada_y_no_se_reintenta(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea
    from core.gateway.meta_errors import GraphError

    tid, admin = await _new_tenant("igl-degrada"), await _admin_id()
    ig, connection_id = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(days=58))
    intentos = []

    async def rechaza(token):
        intentos.append(token)
        raise GraphError("Session has expired", category="PERMANENT", code=190)

    monkeypatch.setattr(tarea, "refrescar_token", rechaza)

    resumen = await tarea.refresh_instagram_tokens()

    assert intentos == [f"token-de-{ig}"]  # una sola vez en el ciclo
    assert resumen["degradadas"] >= 1
    estado, motivo = await _estado(tid, connection_id, ig)
    assert estado == "degraded" and "reconecta" in motivo
    assert await _vence(tid, connection_id, ig) < datetime.now(UTC) + timedelta(days=3)  # el token no cambió


async def test_una_cuenta_degradada_vuelve_a_conectada_cuando_el_refresco_funciona(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea

    tid, admin = await _new_tenant("igl-recupera"), await _admin_id()
    ig, connection_id = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(days=58))
    await tarea._marcar(connection_id, tid, ig, "degraded", "falló antes")

    async def refrescar(token):
        return "token-nuevo", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)
    await tarea.refresh_instagram_tokens()

    assert await _estado(tid, connection_id, ig) == ("connected", None)


async def test_el_refresco_cubre_a_todos_los_negocios_y_una_falla_no_frena_a_las_demas(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea

    a, b, admin = await _new_tenant("igl-multi-a"), await _new_tenant("igl-multi-b"), await _admin_id()
    ig_a, con_a = await _cuenta_con_token(meta_ig, a, admin, emitido_hace=timedelta(days=57))
    ig_b, con_b = await _cuenta_con_token(meta_ig, b, admin, emitido_hace=timedelta(days=57))

    async def refrescar(token):
        if token == f"token-de-{ig_a}":
            raise RuntimeError("error inesperado")  # ni siquiera un GraphError: el ciclo sigue
        return "token-b-nuevo", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)

    resumen = await tarea.refresh_instagram_tokens()

    assert resumen["errores"] >= 1 and resumen["refrescadas"] >= 1
    assert (await _vence(b, con_b, ig_b)) > datetime.now(UTC) + timedelta(days=59)
    assert (await _vence(a, con_a, ig_a)) < datetime.now(UTC) + timedelta(days=4)  # el de A quedó como estaba


async def test_el_refresco_ignora_las_cuentas_de_facebook_login(meta_ig, monkeypatch) -> None:
    """Las de Facebook Login usan el token de la Página, que no se refresca por aquí."""
    from _api_helpers import MetaSimulado, pagina_con_instagram

    import core.gateway.meta_instagram_alta as facebook_login
    import core.tasks.instagram_token_refresh as tarea

    tid, admin = await _new_tenant("igl-fb"), await _admin_id()
    page, ig = str(uuid.uuid4().int % 10**12), _ig_id()
    simulado = MetaSimulado([pagina_con_instagram(page, ig)])
    monkeypatch.setattr(facebook_login, "graph_fetch", simulado.graph_fetch)

    async def canje(version, params, settings_, *, redirect_path="/x"):
        return "token-usuario", False

    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", canje)
    monkeypatch.setattr(get_settings(), "meta_app_id", "platform-app-test")
    monkeypatch.setattr(get_settings(), "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(get_settings(), "meta_instagram_config_id", "ig-config")
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        intento = await facebook_login.iniciar_intento(tid, admin)
        d = await facebook_login.descubrir_cuentas(tid, admin, "c", intento)
        await facebook_login.conectar_cuentas(tid, admin, d.connection_id, [ig])
    llamadas = []

    async def refrescar(token):
        llamadas.append(token)
        return "x", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)
    await tarea.refresh_instagram_tokens()
    # El refresco recorre a todos los negocios: puede tocar cuentas de otras pruebas, pero nunca el token de la Página ni
    # la cuenta de Facebook Login.
    assert f"tok-{page}" not in llamadas and not any(ig in token for token in llamadas)
    assert (await _estado(tid, d.connection_id, ig)) == ("connected", None)


# ------------------------------------------------------------------ cambio de ruta de la misma cuenta
async def _conectar_por_facebook(monkeypatch, tid, admin, ig, page):
    """La cuenta `ig` conectada por Facebook Login (ruta a), con Meta simulado."""
    from _api_helpers import MetaSimulado, pagina_con_instagram

    import core.gateway.meta_instagram_alta as facebook_login
    import core.gateway.meta_onboarding as onboarding
    from core.config import get_settings
    from core.shared.tenant import tenant_scope

    simulado = MetaSimulado([pagina_con_instagram(page, ig)])
    monkeypatch.setattr(facebook_login, "graph_fetch", simulado.graph_fetch)

    async def canje(version, params, settings_, *, redirect_path="/x"):
        return "token-usuario", False

    monkeypatch.setattr(onboarding, "_fetch_exchange_token", canje)
    monkeypatch.setattr(get_settings(), "meta_app_id", "platform-app-test")
    monkeypatch.setattr(get_settings(), "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(get_settings(), "meta_instagram_config_id", "ig-config")
    async with tenant_scope(tid):
        intento = await facebook_login.iniciar_intento(tid, admin)
        d = await facebook_login.descubrir_cuentas(tid, admin, "c", intento)
        resultado = await facebook_login.conectar_cuentas(tid, admin, d.connection_id, [ig])
    return d.connection_id, resultado.conectadas[0].channel_connection_id


async def _filas(tid, ig):
    from core.db.models import IgAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (await session.execute(select(IgAccount).where(IgAccount.ig_user_id == ig))).scalars().all()


async def test_pasar_de_facebook_login_a_instagram_login_deja_una_sola_cuenta_vigente(meta_ig, monkeypatch) -> None:
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, admin, ig, page = await _new_tenant("igl-cambia"), await _admin_id(), _ig_id(), str(uuid.uuid4().int % 10**12)
    con_facebook, canonica_a = await _conectar_por_facebook(monkeypatch, tid, admin, ig, page)
    async with tenant_scope(tid):
        assert (await resolve_credentials(canonica_a, None, tid))[1]["login_route"] == "ig_facebook_login"

    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))

    assert cuenta.channel_connection_id == canonica_a  # la misma conexión canónica, religada a la ruta nueva
    async with tenant_scope(tid):
        credenciales = (await resolve_credentials(canonica_a, None, tid))[1]
    assert credenciales["login_route"] == "ig_instagram_login" and "page_id" not in credenciales
    vigentes = [f for f in await _filas(tid, ig) if (f.raw or {}).get("superseded") != "true"]
    assert [(f.login_route, f.connection_id) for f in vigentes] == [("ig_instagram_login", cuenta.connection_id)]
    assert {f.connection_id for f in await _filas(tid, ig)} == {con_facebook, cuenta.connection_id}  # nada se borra


async def test_y_de_instagram_login_de_vuelta_a_facebook_login(meta_ig, monkeypatch) -> None:
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, admin, ig, page = await _new_tenant("igl-vuelve"), await _admin_id(), _ig_id(), str(uuid.uuid4().int % 10**12)
    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))
    con_facebook, canonica = await _conectar_por_facebook(monkeypatch, tid, admin, ig, page)

    assert canonica == cuenta.channel_connection_id
    async with tenant_scope(tid):
        assert (await resolve_credentials(canonica, None, tid))[1]["login_route"] == "ig_facebook_login"
    vigentes = [f for f in await _filas(tid, ig) if (f.raw or {}).get("superseded") != "true"]
    assert [(f.login_route, f.connection_id) for f in vigentes] == [("ig_facebook_login", con_facebook)]


async def test_la_fila_reemplazada_no_se_refresca_ni_se_lista(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea
    from core.gateway import meta_token_store
    from core.shared.tenant import tenant_scope

    tid, admin, ig, page = await _new_tenant("igl-reemp"), await _admin_id(), _ig_id(), str(uuid.uuid4().int % 10**12)
    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))
    async with tenant_scope(tid):  # su token ya está en la ventana de refresco
        await meta_token_store.put_token(
            cuenta.connection_id, "long_lived_user", ig, f"token-de-{ig}",
            expires_at=datetime.now(UTC) + timedelta(days=3), tenant_id=tid,
        )
    await _conectar_por_facebook(monkeypatch, tid, admin, ig, page)  # ahora la cuenta sirve por Facebook Login
    llamadas = []

    async def refrescar(token):
        llamadas.append(token)
        return "x", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)
    await tarea.refresh_instagram_tokens()

    assert f"token-de-{ig}" not in llamadas  # la fila de Instagram Login quedó sin uso


async def test_si_el_alta_falla_antes_de_activar_la_ruta_no_se_religa_la_conexion(meta_ig, monkeypatch) -> None:
    """La cuenta que ya funcionaba por Facebook Login sigue funcionando si el alta por Instagram Login se cae a la mitad."""
    import core.gateway.meta_instagram_login_alta as alta
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, admin, ig, page = await _new_tenant("igl-falla"), await _admin_id(), _ig_id(), str(uuid.uuid4().int % 10**12)
    _con, canonica = await _conectar_por_facebook(monkeypatch, tid, admin, ig, page)

    async def activar_roto(*a, **kw):
        raise RuntimeError("se cayó a la mitad")

    monkeypatch.setattr(alta, "activar_ruta", activar_roto)
    with pytest.raises(RuntimeError):
        await _login(tid, admin, meta_ig.autoriza(ig))

    async with tenant_scope(tid):
        assert (await resolve_credentials(canonica, None, tid))[1]["login_route"] == "ig_facebook_login"


# ------------------------------------------------------------------ identificador de la cuenta y errores de red
async def test_el_identificador_de_la_cuenta_es_el_de_me_aunque_el_del_canje_difiera(meta_ig, monkeypatch) -> None:
    import core.gateway.meta_instagram_login_alta as alta

    tid, admin, ig = await _new_tenant("igl-ids"), await _admin_id(), _ig_id()
    code = meta_ig.autoriza(ig)

    async def canjear_con_otro_id(code_):
        return f"corto-{code_}", "999000111"  # el id con alcance de la app, no la cuenta profesional

    monkeypatch.setattr(alta, "canjear_codigo", canjear_con_otro_id)

    cuenta = await _login(tid, admin, code)

    assert cuenta.ig_user_id == ig and not await _filas(tid, "999000111")


async def test_un_corte_de_red_en_el_canje_es_un_error_controlado_no_un_500(meta_ig, monkeypatch) -> None:
    import httpx

    import core.gateway.meta_instagram_login_alta as alta

    tid, admin = await _new_tenant("igl-red"), await _admin_id()

    async def sin_red(code_):
        raise httpx.ConnectTimeout("sin red")

    monkeypatch.setattr(alta, "canjear_codigo", sin_red)
    with pytest.raises(alta.InstagramAltaError) as exc:
        await _login(tid, admin, meta_ig.autoriza(_ig_id()))
    assert exc.value.code == "meta_error"


async def test_un_corte_de_red_al_suscribir_deja_la_cuenta_guardada_pero_sin_suscribir(meta_ig, monkeypatch) -> None:
    import httpx

    import core.gateway.meta_instagram_login_alta as alta

    tid, admin, ig = await _new_tenant("igl-red2"), await _admin_id(), _ig_id()

    async def sin_red(*a, **kw):
        raise httpx.ReadTimeout("sin red")

    monkeypatch.setattr(alta, "suscribir_y_verificar", sin_red)
    cuenta = await _login(tid, admin, meta_ig.autoriza(ig))
    assert cuenta.suscrita is False and len(await _filas(tid, ig)) == 1


async def test_dos_altas_simultaneas_del_mismo_negocio_comparten_la_cuenta_de_meta(meta_ig) -> None:
    import asyncio

    import core.gateway.meta_instagram_login_alta as alta
    from core.config import get_settings
    from core.shared.tenant import tenant_scope

    tid = await _new_tenant("igl-carrera")

    async def abrir():
        async with tenant_scope(tid):
            return (await alta._cuenta_del_negocio(tid, get_settings())).id

    ids = await asyncio.gather(*[abrir() for _ in range(5)])
    assert len(set(ids)) == 1


# ------------------------------------------------------------------ el cron ante errores transitorios y fallas aisladas
async def test_un_error_transitorio_no_degrada_la_cuenta(meta_ig, monkeypatch) -> None:
    import httpx

    import core.tasks.instagram_token_refresh as tarea
    from core.gateway.meta_errors import GraphError

    tid, admin = await _new_tenant("igl-transit"), await _admin_id()
    ig, connection_id = await _cuenta_con_token(meta_ig, tid, admin, emitido_hace=timedelta(days=58))

    for error in (GraphError("rate limit", category="RATE_LIMIT", code=4), httpx.ReadTimeout("sin red")):
        async def falla(token, error=error):
            raise error

        monkeypatch.setattr(tarea, "refrescar_token", falla)
        resumen = await tarea.refresh_instagram_tokens()
        assert resumen["degradadas"] == 0 and resumen["errores"] >= 1
        assert await _estado(tid, connection_id, ig) == ("connected", None)


async def test_una_lectura_que_falla_en_un_negocio_no_frena_el_refresco_de_los_demas(meta_ig, monkeypatch) -> None:
    import core.tasks.instagram_token_refresh as tarea

    a, b, admin = await _new_tenant("igl-aisla-a"), await _new_tenant("igl-aisla-b"), await _admin_id()
    await _cuenta_con_token(meta_ig, a, admin, emitido_hace=timedelta(days=57))
    ig_b, con_b = await _cuenta_con_token(meta_ig, b, admin, emitido_hace=timedelta(days=57))
    original = tarea.tenant_scope

    def con_falla_en_a(tenant_id):
        if tenant_id == a:
            raise RuntimeError("fila corrupta")
        return original(tenant_id)

    monkeypatch.setattr(tarea, "tenant_scope", con_falla_en_a)

    async def refrescar(token):
        return "token-b-nuevo", datetime.now(UTC) + timedelta(days=60)

    monkeypatch.setattr(tarea, "refrescar_token", refrescar)
    await tarea.refresh_instagram_tokens()  # no levanta

    assert (await _vence(b, con_b, ig_b)) > datetime.now(UTC) + timedelta(days=59)
