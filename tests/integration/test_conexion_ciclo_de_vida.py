"""Ciclo de vida seguro de las conexiones de Meta (HU-CONN-03, ADR-0122).

Los cinco escenarios de la HU más revivir filas al reconectar:
- **Pausa real:** lo que entra a una conexión pausada se guarda, no corre la IA y se avisa a los
  dueños una vez; no sale nada por ella.
- **Eliminar sin perder historia:** conexión, números y WABAs con `deleted_at`, tokens revocados,
  plantillas conservadas, y la app de la plataforma desuscrita de la WABA (Embedded Signup).
- **Activar con criterio:** solo una pausada con el diagnóstico verificado.
- **Sin acciones sobre borradas.**
- **Sin respaldo silencioso:** con la conexión inutilizable, el sender no cae a otra ruta.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # reclamo de números entre negocios y ruteo (ADR-0120, nivel rápido)
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

VERIFICADO = {k: {"ok": True} for k in ("token_valid", "app_subscription_active", "own_app_subscribed")}


async def _estado(tid: uuid.UUID, cid: uuid.UUID, status: str) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).status = status


async def _conexion(tid: uuid.UUID, cid: uuid.UUID):
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await session.get(MetaConnection, cid)


async def _conversacion(tid: uuid.UUID, phone_id: str):
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "whatsapp", "customer_external_id": uniq("wa-cv"), "connection_external_id": phone_id}
        )
    return conversacion


async def _mensajes(tid: uuid.UUID, conversation_id: uuid.UUID):
    from sqlalchemy import select

    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at)
                )
            ).scalars().all()


def _sin_ia(monkeypatch) -> list[str]:
    """La IA no debe correr: si corre, el test falla. Devuelve los pushes a los dueños."""
    import core.orchestrator.consumer as consumer_mod
    import core.tasks.push_tasks as push_tasks

    async def _no_ia(*_a, **_k):
        raise AssertionError("corrió la IA con la conexión pausada")

    monkeypatch.setattr(consumer_mod, "process_conversation", _no_ia)
    monkeypatch.setattr(consumer_mod, "schedule_debounce", _no_ia)
    pushes: list[str] = []
    monkeypatch.setattr(push_tasks.notify_owners_push, "delay", lambda _tid, titulo, *_a, **_k: pushes.append(titulo))
    return pushes


# ---------------------------------------------------------------- pausa real
async def test_lo_que_entra_a_una_conexion_pausada_se_guarda_sin_ia_y_avisa_una_vez(monkeypatch) -> None:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    await _estado(tid, cid, "revoked")
    pushes = _sin_ia(monkeypatch)
    cliente = uniq("wa-pausa")

    async with tenant_scope(tid):
        for i in range(2):
            await consumer._handle_inbound_scoped(
                {
                    "tenant_id": str(tid), "provider": "meta", "channel": "whatsapp",
                    "external_message_id": uniq(f"wamid-{i}"), "customer_external_id": cliente,
                    "customer_name": "Cliente", "content": "hola, ¿están?", "connection_external_id": phone_id,
                    "media": "[]", "media_context": "",
                }
            )

    from sqlalchemy import select

    from core.db.models import Conversation, Customer, Message
    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente_row = (await session.execute(select(Customer).where(Customer.external_id == cliente))).scalar_one()
            conv = (await session.execute(select(Conversation).where(Conversation.customer_id == cliente_row.id))).scalar_one()
            mensajes = (await session.execute(select(Message).where(Message.conversation_id == conv.id))).scalars().all()
    assert [m.direction for m in mensajes] == ["inbound", "inbound"], "se guardan y no se responde nada"
    assert pushes == ["WhatsApp desactivado"], "un solo aviso por conexión y día"


async def test_no_sale_nada_por_una_conexion_pausada(monkeypatch) -> None:
    from core.gateway.meta_connection_state import MOTIVO_PAUSADA
    from core.gateway.sender import resolve_credentials, send_to_conversation
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    conversacion = await _conversacion(tid, phone_id)
    await _estado(tid, cid, "revoked")

    async with tenant_scope(tid):
        assert await send_to_conversation(conversacion.id, "hola") is False
        assert await resolve_credentials(None, cid, expected_tenant_id=tid) == ("meta", {})

    (mensaje,) = await _mensajes(tid, conversacion.id)
    assert (mensaje.status, mensaje.error_reason) == ("failed", MOTIVO_PAUSADA)


async def test_lo_encolado_antes_de_pausar_falla_sin_escalar(monkeypatch) -> None:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope
    from core.tasks import outbound_send

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    conversacion = await _conversacion(tid, phone_id)
    async with tenant_scope(tid):
        async with get_session() as session:
            mensaje = Message(
                tenant_id=tid, conversation_id=conversacion.id, direction="outbound", sender_type="agent",
                content="respuesta", status="queued",
            )
            session.add(mensaje)
            await session.flush()
            mid = mensaje.id
    await _estado(tid, cid, "revoked")
    escalaciones: list[str] = []

    async def _escalar(_cid, reason, summary=""):
        escalaciones.append(reason)

    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escalar)

    error = await outbound_send._send_one(mid, tid)
    assert isinstance(error, outbound_send.ConexionPausadaError)
    assert outbound_send.decide_retry(error, attempts_made=0).action == "fail_permanent"
    await outbound_send._mark_failed(mid, tid, error, dlq=False, escalar=False)

    (fila,) = await _mensajes(tid, conversacion.id)
    assert fila.status == "failed"
    assert escalaciones == []


# ---------------------------------------------------------------- sin respaldo silencioso
async def test_sin_numero_ni_conexion_viva_no_cae_a_otra_ruta(monkeypatch) -> None:
    from core.admin.meta_health_service import soft_delete_connection
    from core.config import get_settings
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr(get_settings(), "whatsapp_phone_number_id", "numero-de-la-plataforma")
    monkeypatch.setattr(get_settings(), "whatsapp_access_token", "token-de-la-plataforma")
    tid, cid, _waba, _phone = await _negocio_con_byok()

    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(cid)) is None
        assert await resolve_credentials(None, cid, expected_tenant_id=tid) == ("meta", {})
        # Sin ruta propia sí se usa el número de la plataforma, como antes.
        _proveedor, creds = await resolve_credentials(None, None, expected_tenant_id=tid)
    assert creds["phone_number_id"] == "numero-de-la-plataforma"


# ---------------------------------------------------------------- eliminar
async def test_eliminar_conserva_la_historia_y_revoca_los_tokens() -> None:
    from sqlalchemy import select

    from core.admin.meta_health_service import soft_delete_connection
    from core.db.models import MessageTemplate, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    conexion = await _conexion(tid, cid)
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            session.add(
                MessageTemplate(
                    tenant_id=tid, waba_account_id=waba.id, meta_template_id=uniq("tpl"), name="aviso",
                    language="es", status="APPROVED",
                )
            )

        assert await soft_delete_connection(tid, str(cid)) is None

        async with get_session() as session:
            numero = (await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_id))).scalar_one()
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            plantillas = (
                await session.execute(select(MessageTemplate).where(MessageTemplate.waba_account_id == waba.id))
            ).scalars().all()
        token = await meta_token_store.get_token(
            cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, conexion.meta_app_id, tenant_id=tid
        )
    assert numero.deleted_at is not None and waba.deleted_at is not None
    assert len(plantillas) == 1
    assert token is None
    assert (await _conexion(tid, cid)).deleted_at is not None


async def test_eliminar_una_de_embedded_signup_desuscribe_la_waba_salvo_que_otra_viva_la_use(monkeypatch) -> None:
    from types import SimpleNamespace

    from core.admin.meta_health_service import soft_delete_connection
    from core.db.models import MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, _phone = await _negocio_con_byok()
    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).origin = "embedded_signup"
            # Otra conexión viva de la plataforma con la misma WABA.
            otra = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="production", origin="embedded_signup",
                meta_app_id=uniq("app-otra"), status="connected",
            )
            session.add(otra)
            await session.flush()
            otra_id = otra.id
            session.add(WabaAccount(tenant_id=tid, connection_id=otra_id, waba_id=waba_id))

    llamadas: list[tuple[str, str]] = []

    async def _creds(_connection, **_k):
        return SimpleNamespace(access_token="tok-es", app_secret="sec-plat", graph_api_version="v26.0")

    async def _graph(path, *, method="GET", **_k):
        llamadas.append((method, path))
        return {"success": True}

    monkeypatch.setattr("core.gateway.meta_credentials.credentials_for", _creds)
    monkeypatch.setattr("core.gateway.meta_graph.graph_fetch", _graph)

    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(cid)) is None
    assert llamadas == [], "la otra conexión viva todavía usa la WABA"

    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(otra_id)) is None
    assert llamadas == [("DELETE", f"/{waba_id}/subscribed_apps")]


# ---------------------------------------------------------------- activar con criterio
@pytest.mark.parametrize("estado", ["draft", "pending", "degraded", "error"])
async def test_activar_no_aplica_si_no_esta_pausada(estado: str) -> None:
    from core.admin.meta_health_service import set_connection_status
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, _phone = await _negocio_con_byok()
    await _estado(tid, cid, estado)

    async with tenant_scope(tid):
        error = await set_connection_status(tid, str(cid), active=True)
    assert error is not None
    assert (await _conexion(tid, cid)).status == estado


async def test_activar_una_pausada_exige_el_diagnostico_verificado(monkeypatch) -> None:
    from core.admin.meta_health_service import set_connection_status
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, _phone = await _negocio_con_byok()
    await _estado(tid, cid, "revoked")
    checks = {"token_valid": {"ok": False, "detail": "Token inválido"}}

    async def _diagnostico(_connection, _tenant_id):
        return checks

    async def _sin_refresco(_connection):
        return None

    monkeypatch.setattr("core.gateway.meta_health.run_health_checks", _diagnostico)
    monkeypatch.setattr("core.tasks.meta_subscriptions_health.refrescar_suscripcion_de_conexion", _sin_refresco)

    async with tenant_scope(tid):
        error = await set_connection_status(tid, str(cid), active=True)
    assert error is not None and "Token inválido" in error
    assert (await _conexion(tid, cid)).status == "revoked"

    checks = VERIFICADO
    async with tenant_scope(tid):
        assert await set_connection_status(tid, str(cid), active=True) is None
    conexion = await _conexion(tid, cid)
    assert conexion.status == "connected"
    assert conexion.health["token_valid"]["ok"] is True


# ---------------------------------------------------------------- sin acciones sobre borradas
async def test_no_se_puede_operar_sobre_una_conexion_eliminada(monkeypatch) -> None:
    from core.admin.meta_health_service import (
        diagnose_connection,
        set_billing_status,
        set_connection_status,
        soft_delete_connection,
    )
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, _phone = await _negocio_con_byok()
    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(cid)) is None
        assert await set_connection_status(tid, str(cid), active=True) == "Conexión no encontrada"
        assert await set_connection_status(tid, str(cid), active=False) == "Conexión no encontrada"
        assert await soft_delete_connection(tid, str(cid)) == "Conexión no encontrada"
        assert await set_billing_status(tid, str(cid), "ok") == "Conexión no encontrada"
        assert await diagnose_connection(tid, str(cid)) == "Conexión no encontrada"


# ---------------------------------------------------------------- revivir filas
async def test_reconectar_revive_el_numero_y_la_waba_con_sus_plantillas_y_conversaciones() -> None:
    from sqlalchemy import select

    from core.admin.meta_health_service import soft_delete_connection
    from core.db.models import Conversation, MessageTemplate, MetaConnection, WabaAccount
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import numero_para_conexion, waba_para_conexion
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    conversacion = await _conversacion(tid, phone_id)
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            waba_row_id = waba.id
            session.add(
                MessageTemplate(
                    tenant_id=tid, waba_account_id=waba.id, meta_template_id=uniq("tpl"), name="aviso",
                    language="es", status="APPROVED",
                )
            )
        assert await soft_delete_connection(tid, str(cid)) is None

        async with get_session() as session:
            nueva = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok",
                meta_app_id=uniq("app-nueva"), status="pending",
            )
            session.add(nueva)
            await session.flush()
            revivida = await waba_para_conexion(session, tid, nueva.id, waba_id)
            numero, anterior = await numero_para_conexion(session, tid, nueva.id, phone_id, waba_account_id=revivida.id)
            nueva_id = nueva.id

        async with get_session() as session:
            conv = await session.get(Conversation, conversacion.id)
            plantillas = (
                await session.execute(select(MessageTemplate).where(MessageTemplate.waba_account_id == revivida.id))
            ).scalars().all()
    assert revivida.id == waba_row_id and revivida.deleted_at is None and revivida.subscribed is False
    assert numero.deleted_at is None and numero.connection_id == nueva_id and anterior == cid
    assert len(plantillas) == 1, "las plantillas siguen en su WABA"
    assert conv.meta_connection_id == nueva_id, "las conversaciones pasan a la conexión nueva"


async def test_otro_negocio_puede_reclamar_un_numero_eliminado() -> None:
    from core.admin.meta_health_service import soft_delete_connection
    from core.alta_negocio import onboard_tenant
    from core.db.models import MessageTemplate, MetaConnection, PhoneNumber, WabaAccount
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, cid, waba_id, phone_id = await _negocio_con_byok()
    meta_template_id = uniq("tpl")
    async with tenant_scope(tid):
        async with get_session() as session:
            from sqlalchemy import select

            waba = (await session.execute(select(WabaAccount).where(WabaAccount.waba_id == waba_id))).scalar_one()
            session.add(
                MessageTemplate(
                    tenant_id=tid, waba_account_id=waba.id, meta_template_id=meta_template_id, name="aviso",
                    language="es", status="APPROVED",
                )
            )
        assert await soft_delete_connection(tid, str(cid)) is None

    slug = uniq("reclama")
    tid_b, _ = await onboard_tenant(name=f"Reclama {slug}", slug=slug, persona={"tone": "cercano"})
    async with tenant_scope(tid_b):
        async with get_session() as session:
            conexion = MetaConnection(
                tenant_id=tid_b, channel="whatsapp", environment="sandbox", origin="byok",
                meta_app_id=uniq("app-b"), status="connected",
            )
            session.add(conexion)
            await session.flush()
            waba_b = WabaAccount(tenant_id=tid_b, connection_id=conexion.id, waba_id=waba_id)
            session.add(waba_b)
            await session.flush()
            session.add(PhoneNumber(tenant_id=tid_b, connection_id=conexion.id, phone_number_id=phone_id))
            # El sync de plantillas del dueño nuevo trae los mismos `meta_template_id`.
            session.add(
                MessageTemplate(
                    tenant_id=tid_b, waba_account_id=waba_b.id, meta_template_id=meta_template_id, name="aviso",
                    language="es", status="APPROVED",
                )
            )


async def test_no_se_revive_un_numero_que_otro_negocio_tomo_por_conexion_manual() -> None:
    """El índice parcial no ve `channel_connections`: el reclamo se revisa antes de revivir."""
    from test_meta_reclamo_waba_numero import _new_tenant

    from core.admin.meta_health_service import soft_delete_connection
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import MetaAssetError, numero_para_conexion
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(cid)) is None
    await _new_tenant("toma-manual", phone_in_channel_connections=phone_id)

    async with tenant_scope(tid):
        with pytest.raises(MetaAssetError):
            async with get_session() as session:
                nueva = MetaConnection(
                    tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok",
                    meta_app_id=uniq("app-rev"), status="pending",
                )
                session.add(nueva)
                await session.flush()
                await numero_para_conexion(session, tid, nueva.id, phone_id)


async def test_el_alta_de_embedded_signup_no_guarda_activos_en_una_conexion_eliminada(monkeypatch) -> None:
    from _api_helpers import stub_meta_pair_ok
    from sqlalchemy import select
    from test_meta_reclamo_waba_numero import _new_tenant

    import core.gateway.meta_onboarding as onboarding
    from core.admin.meta_health_service import soft_delete_connection
    from core.config import get_settings
    from core.db.models import MetaConnection, PhoneNumber
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    settings = get_settings()
    monkeypatch.setattr(settings, "meta_app_id", "platform-app-test")
    monkeypatch.setattr(settings, "meta_app_secret", "platform-secret-test")
    monkeypatch.setattr(settings, "meta_config_id", "platform-config")
    stub_meta_pair_ok(monkeypatch)
    tid = await _new_tenant("es-borrada")
    async with tenant_scope(tid):
        wizard = await onboarding.start_or_resume_wizard(tid)
        async with get_session() as session:
            (await session.get(MetaConnection, wizard.connection_id)).status = "connected"
        assert await soft_delete_connection(tid, str(wizard.connection_id)) is None

        phone_id = uniq("ph-es-borrada")
        with pytest.raises(onboarding.WizardError):
            await onboarding._attach_finish_assets(
                wizard.id, tid, wizard.connection_id, "tok", waba_id=uniq("waba-es-borrada"), phone_number_id=phone_id
            )
        async with get_session() as session:
            filas = (await session.execute(select(PhoneNumber).where(PhoneNumber.phone_number_id == phone_id))).all()
    assert filas == []
