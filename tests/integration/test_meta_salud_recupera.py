"""Una conexión de Meta en `error` o `degraded` se recupera sola (ADR-0102).

Desde ADR-0101 una conexión `degraded`/`error` presta su secreto y recibe mensajes, pero el
cron de salud no revisaba las `error` y el de suscripciones solo miraba las `connected`:
no volvían nunca a `connected`. Ahora sí, pero una conexión solo MEJORA de estado con el
diagnóstico verificado: si no, un alta BYOK fallida pasaba a `connected`, ganaba el orden
de `_resolve_byok_token` y le quitaba el secreto a la conexión sana.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

OK = {"ok": True}
SIN_DATO = {"ok": None}
FALLA = {"ok": False}
VERIFICADO = {
    "token_valid": OK, "app_subscription_active": OK, "own_app_subscribed": OK, "billing": SIN_DATO, "templates": OK,
}
SIN_VERIFICAR = {"app_subscription_active": SIN_DATO, "billing": SIN_DATO}


async def _tenant() -> uuid.UUID:
    from core.onboarding import onboard_tenant

    slug = f"recupera-{uuid.uuid4().hex[:6]}"
    tid, _ = await onboard_tenant(name=f"Recupera {slug}", slug=slug, persona={"tone": "cercano"})
    return tid


async def _conexion(
    tid: uuid.UUID,
    status: str,
    *,
    numeros: int = 1,
    app_secret: str | None = None,
    access_token: str | None = None,
    hace_dias: int = 0,
    status_reason: str | None = None,
    health: dict | None = None,
) -> tuple[uuid.UUID, str]:
    from core.db.models import MetaConnection, PhoneNumber, utcnow
    from core.db.session import get_session
    from core.gateway import meta_byok
    from core.shared.tenant import tenant_scope

    app_id = f"app-rec-{uuid.uuid4().hex[:8]}"
    async with tenant_scope(tid):
        async with get_session() as session:
            connection = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                status=status, status_reason=status_reason, connected_at=utcnow() - timedelta(days=hace_dias),
                health=health or {},
            )
            session.add(connection)
            await session.flush()
            for _ in range(numeros):
                session.add(
                    PhoneNumber(
                        tenant_id=tid, connection_id=connection.id, phone_number_id=f"ph-rec-{uuid.uuid4().hex[:10]}"
                    )
                )
            cid = connection.id
        if app_secret:
            await meta_byok.meta_token_store.put_token(cid, meta_byok.APP_SECRET_KIND, app_id, app_secret, tenant_id=tid)
        if access_token:
            await meta_byok.meta_token_store.put_token(
                cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, app_id, access_token, tenant_id=tid
            )
    return cid, app_id


async def _fila(tid: uuid.UUID, cid: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await session.get(MetaConnection, cid)


async def _secreto(tid: uuid.UUID) -> str | None:
    from core.gateway.meta_byok import resolve_byok_app_secret
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await resolve_byok_app_secret(tid)


def _checks_falsos(monkeypatch, checks: dict) -> None:
    async def fake_checks(_connection, _tenant_id):
        return checks

    monkeypatch.setattr("core.tasks.meta_health_check.run_health_checks", fake_checks)
    monkeypatch.setattr("core.gateway.meta_health.run_health_checks", fake_checks)


async def test_el_cron_revisa_las_conexiones_en_error_tengan_o_no_numeros() -> None:
    """Un reintento BYOK fallido se queda con el número: la que lo pierde es la conexión
    sana, y no puede quedar fuera del cron."""
    from core.tasks.meta_health_check import _connections_to_check

    tid = await _tenant()
    con_numeros, _ = await _conexion(tid, "error")
    sin_numeros, _ = await _conexion(tid, "error", numeros=0)
    desactivada, _ = await _conexion(tid, "revoked")

    ids = {c.id for c in await _connections_to_check()}

    assert con_numeros in ids
    assert sin_numeros in ids
    assert desactivada not in ids


async def test_el_cron_recupera_una_conexion_en_error_con_el_diagnostico_verificado(monkeypatch) -> None:
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    cid, _ = await _conexion(tid, "error", status_reason="Checks críticos fallando: token_valid")
    _checks_falsos(monkeypatch, VERIFICADO)

    await _check_one(await _fila(tid, cid))

    fila = await _fila(tid, cid)
    assert (fila.status, fila.status_reason) == ("connected", None)


async def test_sin_verificar_no_sale_de_error_y_conserva_la_razon(monkeypatch) -> None:
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    cid, _ = await _conexion(tid, "error", status_reason="Fallo guardando credenciales")
    _checks_falsos(monkeypatch, SIN_VERIFICAR)

    await _check_one(await _fila(tid, cid))

    fila = await _fila(tid, cid)
    assert (fila.status, fila.status_reason) == ("error", "Fallo guardando credenciales")
    assert fila.health["app_subscription_active"] == SIN_DATO  # el diagnóstico sí se guarda


async def test_un_alta_fallida_sin_token_no_le_quita_el_secreto_a_la_sana() -> None:
    """La trampa de punta a punta, con el `run_health_checks` real: sin credenciales, antes
    `decide_status` daba `connected`, la conexión nueva ganaba el orden y el negocio se
    quedaba sin secreto (todo su inbound daba 401). Una BYOK sin credenciales propias ahora
    falla `token_valid`."""
    from core.admin.meta_health_service import diagnose_connection
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    await _conexion(tid, "connected", app_secret="SEC-SANA", access_token="tok-sana", hace_dias=30)
    fallida, _ = await _conexion(tid, "error")  # más nueva, con el número, sin tokens
    assert await _secreto(tid) == "SEC-SANA"

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(fallida)) is None
    fila = await _fila(tid, fallida)
    assert fila.status == "error"
    assert fila.health["token_valid"]["ok"] is False
    assert fila.health["token_valid"]["repair_action"] == "reauthorize"
    assert await _secreto(tid) == "SEC-SANA"

    await _check_one(fila)
    assert (await _fila(tid, fallida)).status == "error"
    assert await _secreto(tid) == "SEC-SANA"


async def test_un_alta_fallida_que_ya_estaba_en_connected_baja_a_error() -> None:
    """Las filas que el "Diagnosticar" de antes ya pasó a `connected` siguen quitándole el
    secreto a la sana. Una BYOK sin credenciales propias falla `token_valid`, y empeorar
    siempre se puede: baja a `error` y la sana lo recupera."""
    from core.admin.meta_health_service import diagnose_connection
    from core.shared.tenant import tenant_scope

    tid = await _tenant()
    await _conexion(tid, "connected", app_secret="SEC-SANA", access_token="tok-sana", hace_dias=30)
    promovida, _ = await _conexion(tid, "connected")  # más nueva, sin tokens
    assert await _secreto(tid) is None  # hoy: el negocio no tiene secreto

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(promovida)) is None

    assert (await _fila(tid, promovida)).status == "error"
    assert await _secreto(tid) == "SEC-SANA"


async def test_la_sana_que_perdio_el_numero_se_recupera_y_recupera_el_secreto(monkeypatch) -> None:
    """Un reintento con otra app falló y se quedó con el número; después la sana pasó a
    `error` (token a 7 días de vencer). Las dos en `error`: gana la más nueva y el negocio da
    401. Al renovar el token, el cron revisa la sana aunque no tenga números y la recupera."""
    from core.tasks.meta_health_check import _check_one, _connections_to_check

    tid = await _tenant()
    sana, _ = await _conexion(tid, "error", numeros=0, app_secret="SEC-SANA", access_token="tok", hace_dias=30)
    await _conexion(tid, "error", app_secret="SEC-FALLIDA", access_token="tok-f")
    assert await _secreto(tid) == "SEC-FALLIDA"

    (listada,) = [c for c in await _connections_to_check() if c.id == sana]
    _checks_falsos(monkeypatch, VERIFICADO)
    await _check_one(listada)

    assert (await _fila(tid, sana)).status == "connected"
    assert await _secreto(tid) == "SEC-SANA"


async def test_un_reintento_con_otra_app_sin_suscribir_no_le_quita_el_secreto_a_la_sana(monkeypatch) -> None:
    """El reintento del wizard con otra app guardó sus tokens y falló al suscribir: su token
    es válido, y la WABA sigue suscrita a la app vieja, que es la que firma. Sin la
    suscripción de SU app confirmada, una BYOK no sale de `error`."""
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    await _conexion(tid, "connected", app_secret="SEC-VIEJA", access_token="tok-vieja", hace_dias=30)
    reintento, _ = await _conexion(tid, "error", app_secret="SEC-NUEVA", access_token="tok-nueva")
    _checks_falsos(monkeypatch, {**VERIFICADO, "app_subscription_active": SIN_DATO})

    await _check_one(await _fila(tid, reintento))

    assert (await _fila(tid, reintento)).status == "error"
    assert await _secreto(tid) == "SEC-VIEJA"


async def test_el_cron_parte_del_estado_de_la_fila_bloqueada(monkeypatch) -> None:
    """Se listó en `error` y el wizard la reconectó antes de que le tocara: un `error`
    viejo no le pisa el `connected`."""
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    cid, _ = await _conexion(tid, "error", status_reason="viejo")
    listada = await _fila(tid, cid)
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = await session.get(MetaConnection, cid)
            fila.status, fila.status_reason = "connected", None
    _checks_falsos(monkeypatch, SIN_VERIFICAR)

    await _check_one(listada)

    fila = await _fila(tid, cid)
    assert (fila.status, fila.status_reason) == ("connected", None)


async def test_un_diagnostico_de_una_conexion_que_el_wizard_reconecto_se_descarta(monkeypatch) -> None:
    """Se listó y, antes de escribir, el wizard la reconectó (nuevo `connected_at`): los
    checks son de credenciales que ya no usa. Ni estado ni `health` cambian."""
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    cid, _ = await _conexion(tid, "error", hace_dias=3, health={"token_valid": {"ok": False, "detail": "viejo"}})
    listada = await _fila(tid, cid)
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = await session.get(MetaConnection, cid)
            fila.status, fila.status_reason, fila.connected_at = "connected", None, utcnow()
    _checks_falsos(monkeypatch, {**VERIFICADO, "token_valid": FALLA})

    await _check_one(listada)

    fila = await _fila(tid, cid)
    assert (fila.status, fila.status_reason) == ("connected", None)
    assert fila.health["token_valid"]["detail"] == "viejo"


async def test_diagnosticar_descarta_el_diagnostico_si_el_wizard_reconecto_mientras_tanto(monkeypatch) -> None:
    from core.admin.meta_health_service import diagnose_connection
    from core.db.models import MetaConnection, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant()
    cid, _ = await _conexion(tid, "connected", hace_dias=3, health={"token_valid": {"ok": True, "detail": "previo"}})

    async def checks_mientras_reconecta(_connection, _tenant_id):
        # El wizard reconecta la conexión mientras Diagnosticar consulta a Meta.
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).connected_at = utcnow()
        return {**VERIFICADO, "token_valid": FALLA}

    monkeypatch.setattr("core.gateway.meta_health.run_health_checks", checks_mientras_reconecta)

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(cid)) is None

    fila = await _fila(tid, cid)
    assert fila.status == "connected"
    assert fila.health["token_valid"]["detail"] == "previo"


async def test_diagnosticar_conserva_coexistence_y_el_estado_de_facturacion(monkeypatch) -> None:
    """`coexistence` evita registrar un número que comparte la app de WhatsApp Business;
    el diagnóstico reemplazaba el JSON entero y lo borraba."""
    from core.admin.meta_health_service import diagnose_connection
    from core.shared.tenant import tenant_scope
    from core.tasks.meta_health_check import _check_one

    tid = await _tenant()
    cid, _ = await _conexion(
        tid, "connected", health={"coexistence": True, "billing": {"manual_status": "al_dia"}}
    )
    _checks_falsos(monkeypatch, VERIFICADO)

    async with tenant_scope(tid):
        assert await diagnose_connection(tid, str(cid)) is None
    health = (await _fila(tid, cid)).health
    assert health["coexistence"] is True
    assert health["billing"]["manual_status"] == "al_dia"

    await _check_one(await _fila(tid, cid))
    health = (await _fila(tid, cid)).health
    assert health["coexistence"] is True
    assert health["billing"]["manual_status"] == "al_dia"


async def test_el_cron_de_suscripciones_revisa_la_app_de_una_conexion_degradada(monkeypatch) -> None:
    """Meta desactivó la suscripción, el cron degradó la conexión y antes no volvía a
    revisar esa app: el `active=False` quedaba para siempre."""
    import core.tasks.meta_subscriptions_health as subs_cron
    from core.gateway.meta_subscriptions import list_app_subscriptions, persist_subscription_state

    tid = await _tenant()
    _, app_id = await _conexion(tid, "degraded", app_secret="SEC-DEG", access_token="tok-deg")
    await persist_subscription_state(
        app_id, "whatsapp_business_account", callback_url=None, active=False, fields=[], last_error="caída"
    )

    async def fake_token(*_a, **_k):
        return "app-token"

    async def fake_graph(path, **_k):
        assert path == f"/{app_id}/subscriptions"
        return {"data": [{"object": "whatsapp_business_account", "active": True, "fields": [{"name": "messages"}]}]}

    monkeypatch.setattr(subs_cron, "fetch_app_access_token", fake_token)
    monkeypatch.setattr(subs_cron, "graph_fetch", fake_graph)

    (group,) = [g for g in await subs_cron._group_connected_apps() if g.meta_app_id == app_id]
    assert group.app_secret == "SEC-DEG"
    await subs_cron._check_group(group)  # solo esta app: el resto de la BD es compartida

    (estado,) = await list_app_subscriptions(app_id)
    assert estado.active is True
