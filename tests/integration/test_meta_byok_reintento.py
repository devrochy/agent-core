"""Un reintento BYOK que falla no se queda con el número ni presta su secreto (BACKLOG Alta).

`save_byok_connection` reasigna el número a la conexión del intento ANTES de guardar tokens y
suscribir. Si eso falla, `_rollback_failed_alta` le devuelve el número a su conexión anterior
y, si la fila se creó en este intento, la da de baja. Con la BD real y Graph falso.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import stub_meta_pair_ok, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _negocio_con_conexion_sana(*, deleted: bool = False):
    """Negocio con la conexión BYOK A (app A, conectada, con secreto) dueña del número."""
    from core.db.models import MetaConnection, PhoneNumber, WabaAccount, utcnow
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("reintento")
    tid, _ = await onboard_tenant(name=f"Reintento {slug}", slug=slug, persona={"tone": "cercano"})
    app_a, waba, phone_id = uniq("app-a"), uniq("waba"), uniq("ph")
    async with tenant_scope(tid):
        async with get_session() as session:
            sana = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_a,
                status="revoked" if deleted else "connected", connected_at=utcnow(),
                deleted_at=utcnow() if deleted else None,
            )
            session.add(sana)
            await session.flush()
            session.add(WabaAccount(tenant_id=tid, connection_id=sana.id, waba_id=waba, subscribed=True))
            session.add(PhoneNumber(tenant_id=tid, connection_id=sana.id, phone_number_id=phone_id, registered=True))
            sana_id = sana.id
        await meta_token_store.put_token(sana_id, meta_byok.APP_SECRET_KIND, app_a, "secreto-a", tenant_id=tid)
    return tid, sana_id, app_a, waba, phone_id


def _meta_falso(monkeypatch, *, suscribe_waba: bool = True, falla_app: bool = False) -> None:
    import core.gateway.meta_byok as byok_mod
    from core.gateway.meta_subscriptions import SubscriptionError

    async def _verify_ok(phone_number_id, access_token, app_secret):
        return True, "ok", {"display_phone_number": "+57 300", "verified_name": "Negocio"}

    async def _subscribe(*_a, **_k):
        return suscribe_waba

    async def _upsert(*_a, **_k):
        if falla_app:
            raise SubscriptionError("Meta rechazó la suscripción de campos")

    async def _sin_expiracion(*_a, **_k):
        return None

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _verify_ok)
    stub_meta_pair_ok(monkeypatch)
    monkeypatch.setattr(byok_mod, "_subscribe_waba_to_app", _subscribe)
    monkeypatch.setattr(byok_mod, "_token_expiry", _sin_expiracion)
    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _upsert)


async def _estado(tid, phone_id, app_id):
    from sqlalchemy import select

    from core.db.models import MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            phone = (
                await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_id))
            ).scalar_one_or_none()
            intento = (
                await session.execute(
                    select(MetaConnection).where(MetaConnection.tenant_id == tid, MetaConnection.meta_app_id == app_id)
                )
            ).scalar_one_or_none()
            wabas_intento = (
                (await session.execute(select(WabaAccount).where(WabaAccount.connection_id == intento.id))).scalars().all()
                if intento is not None
                else []
            )
    return phone, intento, wabas_intento


async def _reintentar(tid, app_id, phone_id, waba):
    from core.gateway.meta_byok import ByokError, save_byok_connection
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        with pytest.raises(ByokError):
            await save_byok_connection(tid, None, app_id, "secreto-b", phone_id, "token-b", waba)


@pytest.mark.parametrize(
    ("suscribe_waba", "falla_app"),
    [(False, False), (True, True)],
    ids=["falla-subscribed_apps", "falla-campos-de-la-app-con-waba-suscrita"],
)
async def test_un_reintento_con_otra_app_que_falla_devuelve_el_numero_y_se_da_de_baja(
    app_client, monkeypatch, suscribe_waba, falla_app
) -> None:
    from core.gateway.meta_byok import resolve_byok_app_secret
    from core.shared.tenant import tenant_scope

    tid, sana_id, _app_a, waba, phone_id = await _negocio_con_conexion_sana()
    _meta_falso(monkeypatch, suscribe_waba=suscribe_waba, falla_app=falla_app)
    app_b = uniq("app-b")

    await _reintentar(tid, app_b, phone_id, waba)

    phone, intento, wabas_intento = await _estado(tid, phone_id, app_b)
    assert phone is not None and phone.connection_id == sana_id
    assert intento is not None
    assert intento.deleted_at is not None
    assert intento.status == "error" and "Fallo" in (intento.status_reason or "")
    assert wabas_intento == []
    async with tenant_scope(tid):
        assert await resolve_byok_app_secret(tid) == "secreto-a"


async def test_un_reintento_con_la_misma_app_devuelve_el_numero_pero_no_borra_la_conexion(
    app_client, monkeypatch
) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    # El número es de la conexión sana A; la app C ya tenía su propia conexión (reusada).
    tid, sana_id, _app_a, waba, phone_id = await _negocio_con_conexion_sana()
    app_c = uniq("app-c")
    async with tenant_scope(tid):
        async with get_session() as session:
            reusada = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_c,
                status="error",
            )
            session.add(reusada)
            await session.flush()
            reusada_id = reusada.id
    _meta_falso(monkeypatch, suscribe_waba=False)

    await _reintentar(tid, app_c, phone_id, waba)

    phone, intento, _ = await _estado(tid, phone_id, app_c)
    assert phone.connection_id == sana_id
    assert intento.id == reusada_id
    assert intento.deleted_at is None
    assert intento.status == "error"


async def test_si_la_conexion_anterior_esta_borrada_el_numero_no_vuelve_y_la_nueva_se_da_de_baja(
    app_client, monkeypatch
) -> None:
    tid, _sana_id, _app_a, waba, phone_id = await _negocio_con_conexion_sana(deleted=True)
    _meta_falso(monkeypatch, suscribe_waba=False)
    app_b = uniq("app-b")

    await _reintentar(tid, app_b, phone_id, waba)

    phone, intento, _ = await _estado(tid, phone_id, app_b)
    assert intento.deleted_at is not None
    # El número no se le devuelve a una conexión borrada: se libera con la del intento.
    assert phone is None


async def test_el_alta_que_sale_bien_se_queda_con_el_numero(app_client, monkeypatch) -> None:
    from core.gateway.meta_byok import save_byok_connection
    from core.shared.tenant import tenant_scope

    tid, _sana_id, _app_a, waba, phone_id = await _negocio_con_conexion_sana()
    _meta_falso(monkeypatch)
    app_b = uniq("app-b")

    async with tenant_scope(tid):
        nueva = await save_byok_connection(tid, None, app_b, "secreto-b", phone_id, "token-b", waba)

    phone, intento, wabas_intento = await _estado(tid, phone_id, app_b)
    assert nueva.status == "connected"
    assert phone.connection_id == intento.id == nueva.id
    assert intento.deleted_at is None
    assert [w.subscribed for w in wabas_intento] == [True]


async def test_otro_negocio_no_recibe_el_numero_de_vuelta(app_client, monkeypatch) -> None:
    # Defensa: la conexión anterior se busca con el tenant del alta; un id de otro negocio no
    # se acepta aunque exista.
    from core.gateway.meta_byok import _rollback_failed_alta

    tid, sana_id, _app_a, _waba, phone_id = await _negocio_con_conexion_sana()
    otro_tid, otra_id, _x, _y, _z = await _negocio_con_conexion_sana()
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await _rollback_failed_alta(
            tid, sana_id, phone_id, created_new=False, previous_connection_id=otra_id, reason="prueba"
        )
    phone, _intento, _ = await _estado(tid, phone_id, uuid.uuid4().hex)
    assert phone.connection_id == sana_id
    assert otro_tid != tid
