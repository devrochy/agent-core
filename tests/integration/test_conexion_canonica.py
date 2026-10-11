"""Conexión canónica y conversación por conexión, primera parte (HU-AOS-11, ADR-0115).

- **Altas:** toda alta o reconexión de un número crea o religa su fila de `channel_connections`
  (sin credenciales, `is_primary = false`) en la transacción de `numero_para_conexion`; una
  conexión manual del mismo número gana y no se duplica.
- **Las filas canónicas no cambian lo de antes:** no rutean ni reclaman el número (0073), no
  aparecen como conexiones manuales y no cuentan para el gating.
- **Pausa por la conexión:** con la conversación ligada a la conexión canónica, las guardas y
  el sender resuelven la cuenta por `channel_connections.meta_connection_id`.
- **Envío por el número de la conexión,** sin respaldo a otro número de la cuenta.
- **Ruteo** con `AOS_CONNECTION_ROUTING` apagada (el de siempre, limpiando la release al
  cambiar de conexión) y encendida (una conversación por conexión, adopta huérfanas).
- **Backfill** idempotente, con dry-run y choques.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest
from _api_helpers import uniq
from test_api_registrar_numero import _negocio_con_byok

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # ruteo de números entre negocios y pausa (ADR-0120, nivel rápido)
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


# ---------------------------------------------------------------- helpers
async def _alta_numero(tid: uuid.UUID, cuenta: uuid.UUID, phone_id: str) -> None:
    """Alta de un número por el camino de siempre (`numero_para_conexion`, ADR-0122 §6)."""
    from core.db.session import get_session
    from core.gateway.meta_asset_claims import claim_assets, numero_para_conexion
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            await claim_assets(session, tid, waba_id=None, phone_number_id=phone_id)
            await numero_para_conexion(session, tid, cuenta, phone_id)


async def _otra_cuenta(tid: uuid.UUID, status: str = "connected") -> uuid.UUID:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.gateway import meta_byok, meta_token_store
    from core.shared.tenant import tenant_scope

    app_id = uniq("app")
    async with tenant_scope(tid):
        async with get_session() as session:
            cuenta = MetaConnection(
                tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=app_id,
                status=status,
            )
            session.add(cuenta)
            await session.flush()
            cid = cuenta.id
        await meta_token_store.put_token(cid, meta_byok.CLOUD_API_ACCESS_TOKEN_KIND, app_id, "tok-b", tenant_id=tid)
    return cid


async def _conexiones(tid: uuid.UUID, phone_id: str, *, todas: bool = False):
    from sqlalchemy import select

    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            query = select(ChannelConnection).where(
                ChannelConnection.tenant_id == tid, ChannelConnection.external_id == phone_id
            )
            if not todas:
                query = query.where(ChannelConnection.deleted_at.is_(None))
            return list((await session.execute(query)).scalars().all())


async def _entrada(tid: uuid.UUID, cliente: str, phone_id: str):
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        conversacion, _, _ = await consumer._resolve_conversation(
            {"channel": "whatsapp", "customer_external_id": cliente, "connection_external_id": phone_id}
        )
    return conversacion


async def _estado(tid: uuid.UUID, cid: uuid.UUID, status: str) -> None:
    from core.db.models import MetaConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            (await session.get(MetaConnection, cid)).status = status


def _bandera(monkeypatch, encendida: bool) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "aos_connection_routing", encendida)


# ---------------------------------------------------------------- altas posteriores
async def test_alta_crea_una_conexion_por_numero_sin_credenciales_y_la_religa_al_cambiar_de_cuenta() -> None:
    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-alta")

    await _alta_numero(tid, cuenta, numero)
    await _alta_numero(tid, cuenta, numero)  # reintentar no duplica
    (fila,) = await _conexiones(tid, numero)
    assert fila.meta_connection_id == cuenta
    assert fila.credentials_encrypted is None
    assert fila.is_primary is False
    assert (fila.type_id, fila.category, fila.channel, fila.provider) == (
        "whatsapp_meta", "comunicacion", "whatsapp", "meta",
    )

    # Reconectar el número en otra cuenta del negocio: la MISMA fila, religada.
    otra = await _otra_cuenta(tid)
    await _alta_numero(tid, otra, numero)
    (religada,) = await _conexiones(tid, numero)
    assert religada.id == fila.id
    assert religada.meta_connection_id == otra


async def test_una_conexion_manual_del_mismo_numero_gana_y_no_se_duplica() -> None:
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-manual")
    async with tenant_scope(tid):
        manual, _ = await save_connection(
            "Manual", {"phone_number_id": numero, "access_token": "t", "app_secret": "s", "verify_token": "v"}
        )

    await _alta_numero(tid, cuenta, numero)
    (fila,) = await _conexiones(tid, numero)
    assert fila.id == manual.id
    assert fila.meta_connection_id is None

    # Y al revés: una manual nueva sobre un número que ya tiene su conexión canónica se rechaza
    # con un error claro, no con el CHECK de la base.
    otro = uniq("ph-canonico")
    await _alta_numero(tid, cuenta, otro)
    async with tenant_scope(tid):
        with pytest.raises(ValueError, match="asistente de WhatsApp"):
            await save_connection(
                "Manual", {"phone_number_id": otro, "access_token": "t", "app_secret": "s", "verify_token": "v"}
            )


async def test_alta_byok_crea_la_conexion_y_el_rollback_la_devuelve_a_la_cuenta_anterior(monkeypatch) -> None:
    from _api_helpers import stub_meta_pair_ok

    import core.gateway.meta_byok as byok_mod
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-byok")
    await _alta_numero(tid, cuenta, numero)

    async def _verify_ok(*_a, **_k):
        return True, "ok", {"display_phone_number": "+57 300", "verified_name": "Canonica"}

    async def _ok(*_a, **_k):
        return True

    async def _nada(*_a, **_k):
        return None

    monkeypatch.setattr(byok_mod, "verify_meta_credentials", _verify_ok)
    stub_meta_pair_ok(monkeypatch)
    monkeypatch.setattr(byok_mod, "_subscribe_waba_to_app", _ok)
    monkeypatch.setattr("core.gateway.meta_subscriptions.upsert_app_subscription", _nada)

    async with tenant_scope(tid):
        nueva = await byok_mod.save_byok_connection(
            tid, None, uniq("app-otra"), "secreto", numero, "token", uniq("waba"), verify_token="vt",
        )
    (fila,) = await _conexiones(tid, numero)
    assert fila.meta_connection_id == nueva.id

    # Un alta que falla después de reasignar el número lo devuelve, con su conexión.
    async with tenant_scope(tid):
        await byok_mod._rollback_failed_alta(
            tid, nueva.id, numero, created_new=True, previous_connection_id=cuenta, reason="prueba"
        )
    (devuelta,) = await _conexiones(tid, numero)
    assert devuelta.id == fila.id
    assert devuelta.meta_connection_id == cuenta


# ---------------------------------------------------------------- sin cambio en lo de antes
async def test_la_conexion_canonica_no_rutea_ni_reclama_el_numero_ni_es_manual() -> None:
    from sqlalchemy import text

    from core.admin.meta_health_service import soft_delete_connection
    from core.db.session import get_session
    from core.gateway.connections import active_connection_type_ids, list_connections, set_active
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant
    from core.shared.tenant import resolve_tenant_for_phone, tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-ruteo")
    await _alta_numero(tid, cuenta, numero)
    (fila,) = await _conexiones(tid, numero)

    async with tenant_scope(tid):
        assert fila.id not in {c.id for c in await list_connections()}
        assert "whatsapp_meta" not in await active_connection_type_ids(tid)
        await set_active(fila.id, False)  # la API manual no la toca
    (sigue,) = await _conexiones(tid, numero)
    assert sigue.active is True

    assert await resolve_tenant_for_phone(numero) == tid
    # Dar de baja la cuenta deja la fila canónica viva (sin estado propio), pero el número ya
    # no rutea a este negocio ni queda reclamado: otro negocio puede conectarlo.
    async with tenant_scope(tid):
        assert await soft_delete_connection(tid, str(cuenta)) is None
    assert len(await _conexiones(tid, numero)) == 1
    assert await resolve_tenant_for_phone(numero) is None
    assert await asset_claimed_by_other_tenant(uuid.uuid4(), phone_number_id=numero) is False
    async with get_session() as session:  # sin negocio en contexto, como el webhook
        assert (await session.execute(text("select resolve_tenant_for_phone(:p)"), {"p": numero})).scalar() is None


# ---------------------------------------------------------------- pausa por la conexión
async def test_las_guardas_pausan_por_la_cuenta_de_la_conexion_con_y_sin_connection_id(monkeypatch) -> None:
    from sqlalchemy import select

    from core.api.errors import ApiError
    from core.api.routers.conversations import _rechazar_si_conexion_pausada
    from core.db.models import Conversation, Message
    from core.db.session import get_session
    from core.gateway.event_notices import send_event_notice
    from core.gateway.meta_connection_state import MOTIVO_PAUSADA, motivo_pausa_de_conversacion
    from core.gateway.sender import send_to_conversation
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, viejo = await _negocio_con_byok()  # `viejo`: número sin fila canónica
    numero = uniq("ph-pausa")
    await _alta_numero(tid, cuenta, numero)
    (fila,) = await _conexiones(tid, numero)

    con = await _entrada(tid, uniq("wa-con"), numero)
    sin = await _entrada(tid, uniq("wa-sin"), viejo)
    assert con.connection_id == fila.id
    assert sin.connection_id is None and sin.meta_connection_id == cuenta

    async with tenant_scope(tid):
        for conversacion in (con, sin):
            assert await motivo_pausa_de_conversacion(conversacion) is None

    await _estado(tid, cuenta, "revoked")
    encolados: list[str] = []
    import core.tasks.outbound_send as outbound_mod

    monkeypatch.setattr(outbound_mod.outbound_send_task, "delay", lambda *a, **_k: encolados.append(a[0]))

    async with tenant_scope(tid):
        for conversacion in (con, sin):
            assert await motivo_pausa_de_conversacion(conversacion) == MOTIVO_PAUSADA
            assert await send_to_conversation(conversacion.id, "hola") is False
            with pytest.raises(ApiError) as exc:
                await _rechazar_si_conexion_pausada(conversacion)
            assert exc.value.status_code == 409
            resultado = await send_event_notice(
                conversacion.id, "pedido_confirmado", {}, notice_key=uniq("aviso"), default_fallback_text="Listo"
            )
            assert resultado.modo == "conexion_pausada"
        async with get_session() as session:
            fallidos = (
                await session.execute(
                    select(Message.status).where(Message.conversation_id.in_([con.id, sin.id]))
                )
            ).scalars().all()
    assert encolados == []
    assert fallidos and set(fallidos) == {"failed"}

    # Lo encolado antes de pausar falla en la cola sin escalar (ConexionPausadaError).
    async with tenant_scope(tid):
        async with get_session() as session:
            mensaje = Message(
                tenant_id=tid, conversation_id=con.id, direction="outbound", sender_type="agent",
                content="encolado antes", status="queued", client_message_id=uniq("cm"),
            )
            session.add(mensaje)
            await session.flush()
            mid = mensaje.id
            assert (await session.get(Conversation, con.id)).connection_id == fila.id
    error = await outbound_mod._send_one(mid, tid)
    assert isinstance(error, outbound_mod.ConexionPausadaError)


async def test_lo_que_entra_por_la_conexion_pausada_se_guarda_sin_ia_y_avisa_con_la_cuenta(monkeypatch) -> None:
    import core.orchestrator.consumer as consumer_mod
    import core.tasks.push_tasks as push_tasks
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-entra")
    await _alta_numero(tid, cuenta, numero)
    await _estado(tid, cuenta, "revoked")

    async def _no_ia(*_a, **_k):
        raise AssertionError("corrió la IA con la conexión pausada")

    monkeypatch.setattr(consumer_mod, "process_conversation", _no_ia)
    monkeypatch.setattr(consumer_mod, "schedule_debounce", _no_ia)
    avisos: list[dict] = []
    monkeypatch.setattr(push_tasks.notify_owners_push, "delay", lambda *a, **_k: avisos.append(a[3]))

    async with tenant_scope(tid):
        await consumer_mod._handle_inbound_scoped(
            {
                "tenant_id": str(tid), "provider": "meta", "channel": "whatsapp",
                "external_message_id": uniq("wamid"), "customer_external_id": uniq("wa-entra"),
                "customer_name": "Cliente", "content": "hola", "connection_external_id": numero,
                "media": "[]", "media_context": "",
            }
        )
    assert avisos == [{"type": "core.connection_paused", "connectionId": str(cuenta)}]


# ---------------------------------------------------------------- credenciales por el número
async def test_el_sender_envia_por_el_numero_de_la_conexion_sin_respaldo() -> None:
    from sqlalchemy import update

    from core.db.models import PhoneNumber, utcnow
    from core.db.session import get_session
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, primero = await _negocio_con_byok()  # la cuenta ya tiene un número vivo
    segundo = uniq("ph-segundo")
    await _alta_numero(tid, cuenta, segundo)
    conversacion = await _entrada(tid, uniq("wa-num"), segundo)

    async with tenant_scope(tid):
        proveedor, creds = await resolve_credentials(
            conversacion.connection_id, conversacion.meta_connection_id, expected_tenant_id=tid
        )
    assert proveedor == "meta"
    assert creds == {"phone_number_id": segundo, "access_token": "tok"}

    # Si el número de la conexión deja de estar vivo en la cuenta, no sale por el otro.
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                update(PhoneNumber)
                .where(PhoneNumber.tenant_id == tid, PhoneNumber.phone_number_id == segundo)
                .values(deleted_at=utcnow())
            )
        assert await resolve_credentials(
            conversacion.connection_id, conversacion.meta_connection_id, expected_tenant_id=tid
        ) == ("meta", {})
    assert primero != segundo


async def test_las_plantillas_se_buscan_en_la_cuenta_de_la_conexion() -> None:
    from core.db.models import MessageTemplate, WabaAccount
    from core.db.session import get_session
    from core.gateway.event_notices import approved_template_for
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    numero = uniq("ph-plantilla")
    await _alta_numero(tid, cuenta, numero)
    conversacion = await _entrada(tid, uniq("wa-pl"), numero)

    otra = await _otra_cuenta(tid)
    async with tenant_scope(tid):
        async with get_session() as session:
            waba = WabaAccount(tenant_id=tid, connection_id=otra, waba_id=uniq("waba-b"), subscribed=True)
            session.add(waba)
            await session.flush()
            session.add(
                MessageTemplate(
                    tenant_id=tid, waba_account_id=waba.id, name="aviso_b", language="es", status="APPROVED",
                    category="UTILITY",
                )
            )
    # El número pasa a la otra cuenta (la vieja conserva otro número: el puente no se mueve).
    await _alta_numero(tid, otra, numero)
    async with tenant_scope(tid):
        assert conversacion.meta_connection_id == cuenta
        plantilla = await approved_template_for(conversacion, "aviso_b", "es")
    assert plantilla is not None and plantilla.name == "aviso_b"


# ---------------------------------------------------------------- ruteo
async def test_bandera_apagada_una_conversacion_por_cliente_y_limpia_la_release(monkeypatch) -> None:
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _bandera(monkeypatch, False)
    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    uno, dos = uniq("ph-a"), uniq("ph-b")
    await _alta_numero(tid, cuenta, uno)
    await _alta_numero(tid, cuenta, dos)
    (conexion_uno,) = await _conexiones(tid, uno)
    (conexion_dos,) = await _conexiones(tid, dos)
    cliente = uniq("wa-off")

    primera = await _entrada(tid, cliente, uno)
    manifest_hash = hashlib.sha256(b"{}").hexdigest()
    async with tenant_scope(tid):
        async with get_session() as session:
            release = (
                await session.execute(
                    text(
                        "insert into connection_releases (tenant_id, connection_id, number, manifest, manifest_hash) "
                        "values (:t, :c, 1, '{}'::jsonb, :h) returning id"
                    ),
                    {"t": tid, "c": conexion_uno.id, "h": manifest_hash},
                )
            ).scalar_one()
            await session.execute(
                text("update conversations set release_id = :r, release_lane = 'stable' where id = :id"),
                {"r": release, "id": primera.id},
            )

    segunda = await _entrada(tid, cliente, dos)
    assert segunda.id == primera.id
    assert segunda.connection_id == conexion_dos.id
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (
                await session.execute(
                    text("select connection_id, release_id, release_lane from conversations where id = :id"),
                    {"id": primera.id},
                )
            ).one()
    assert (fila.connection_id, fila.release_id, fila.release_lane) == (conexion_dos.id, None, None)


async def test_bandera_encendida_una_conversacion_por_conexion_sin_reescribir(monkeypatch) -> None:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    _bandera(monkeypatch, True)
    tid, cuenta, _waba, viejo = await _negocio_con_byok()
    uno, dos = uniq("ph-a"), uniq("ph-b")
    await _alta_numero(tid, cuenta, uno)
    await _alta_numero(tid, cuenta, dos)
    (conexion_uno,) = await _conexiones(tid, uno)
    cliente = uniq("wa-on")

    # Una conversación abierta de antes del backfill (sin conexión, por la misma cuenta) se
    # adopta en vez de abrir otra.
    previa = await _entrada(tid, cliente, viejo)
    assert previa.connection_id is None
    por_uno = await _entrada(tid, cliente, uno)
    assert por_uno.id == previa.id
    assert por_uno.connection_id == conexion_uno.id

    por_dos = await _entrada(tid, cliente, dos)
    assert por_dos.id != por_uno.id  # otra conexión, otra conversación
    otra_vez_uno = await _entrada(tid, cliente, uno)
    assert otra_vez_uno.id == por_uno.id
    assert otra_vez_uno.connection_id == conexion_uno.id  # no se reescribió

    # Sin índice único todavía: con dos abiertas de la misma conexión toma la más reciente.
    async with tenant_scope(tid):
        async with get_session() as session:
            customer = await session.get(Customer, por_uno.customer_id)
            duplicada = Conversation(
                tenant_id=tid, customer_id=customer.id, channel="whatsapp", connection_id=conexion_uno.id,
                meta_connection_id=cuenta,
            )
            session.add(duplicada)
            await session.flush()
            duplicada_id = duplicada.id
    assert (await _entrada(tid, cliente, uno)).id == duplicada_id

    # Volver a apagarla es seguro: el camino anterior toma la más reciente del cliente.
    _bandera(monkeypatch, False)
    assert (await _entrada(tid, cliente, dos)).id == duplicada_id


# ---------------------------------------------------------------- backfill
async def test_backfill_idempotente_con_dry_run_choques_tipos_y_conversaciones() -> None:
    from sqlalchemy import select

    from core.db.models import ChannelConnection, Conversation, PhoneNumber
    from core.db.session import get_session
    from core.gateway.conexion_canonica_backfill import backfill_conexiones
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    # Cuenta con un número (de antes: sin fila canónica) y una conversación por el puente.
    tid, cuenta, _waba, numero = await _negocio_con_byok()
    sola = await _entrada(tid, uniq("wa-bf"), numero)
    assert sola.connection_id is None

    # Cuenta con dos números (conversación ambigua), un número con conexión manual (choque) y
    # una conexión vieja sin tipo.
    varios = await _otra_cuenta(tid)
    n1, n2, choque = uniq("ph-v1"), uniq("ph-v2"), uniq("ph-choque")
    async with tenant_scope(tid):
        async with get_session() as session:
            for pid in (n1, n2, choque):
                session.add(PhoneNumber(tenant_id=tid, connection_id=varios, phone_number_id=pid))
            customer_conv = Conversation(
                tenant_id=tid, customer_id=sola.customer_id, channel="whatsapp", meta_connection_id=varios,
            )
            session.add(customer_conv)
            vieja = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="waweb", external_id=uniq("jid"), label="QR",
            )
            session.add(vieja)
            await session.flush()
            ambigua_id, vieja_id = customer_conv.id, vieja.id
        manual, _ = await save_connection(
            "Manual", {"phone_number_id": choque, "access_token": "t", "app_secret": "s", "verify_token": "v"}
        )

    seco = await backfill_conexiones(dry_run=True, tenant_id=tid)
    assert seco.numeros == {"creada": 3, "choque_manual": 1}
    assert [c.phone_number_id for c in seco.choques] == [choque]
    assert seco.tipos_puestos == 1
    assert (seco.conversaciones_ligadas, seco.conversaciones_ambiguas, seco.abiertas_sin_ligar) == (1, 1, 1)
    assert await _conexiones(tid, numero) == []  # no escribió nada

    real = await backfill_conexiones(dry_run=False, tenant_id=tid, lote=1)
    assert real.numeros == {"creada": 3, "choque_manual": 1}
    assert real.conversaciones_ligadas == 1
    (canonica,) = await _conexiones(tid, numero)
    assert canonica.meta_connection_id == cuenta and canonica.credentials_encrypted is None
    assert [c.id for c in await _conexiones(tid, choque)] == [manual.id]
    async with tenant_scope(tid):
        async with get_session() as session:
            assert (await session.get(Conversation, sola.id)).connection_id == canonica.id
            assert (await session.get(Conversation, ambigua_id)).connection_id is None
            assert (await session.get(ChannelConnection, vieja_id)).type_id == "whatsapp_waweb"
            vivas = (
                await session.execute(select(ChannelConnection.id).where(ChannelConnection.tenant_id == tid))
            ).scalars().all()

    otra_vez = await backfill_conexiones(dry_run=False, tenant_id=tid)
    assert otra_vez.numeros == {"ya_estaba": 3, "choque_manual": 1}
    assert (otra_vez.tipos_puestos, otra_vez.conversaciones_ligadas) == (0, 0)
    async with tenant_scope(tid):
        async with get_session() as session:
            assert len(
                (await session.execute(select(ChannelConnection.id).where(ChannelConnection.tenant_id == tid)))
                .scalars().all()
            ) == len(vivas)


async def test_numero_que_pasa_a_otro_negocio_no_queda_reclamado_ni_responde_por_el_anterior() -> None:
    from core.admin.meta_health_service import soft_delete_connection
    from core.gateway.meta_asset_claims import asset_claimed_by_other_tenant
    from core.gateway.sender import resolve_credentials
    from core.shared.tenant import resolve_tenant_for_phone, tenant_scope

    tid_a, cuenta_a, _waba_a, _phone_a = await _negocio_con_byok()
    tid_b, cuenta_b, _waba_b, _phone_b = await _negocio_con_byok()
    numero = uniq("ph-traspaso")
    await _alta_numero(tid_a, cuenta_a, numero)
    conversacion_a = await _entrada(tid_a, uniq("wa-a"), numero)
    assert await asset_claimed_by_other_tenant(tid_b, phone_number_id=numero) is True

    # A da de baja su cuenta: su fila canónica sigue viva, pero el número queda libre.
    async with tenant_scope(tid_a):
        assert await soft_delete_connection(tid_a, str(cuenta_a)) is None
    assert len(await _conexiones(tid_a, numero)) == 1
    assert await asset_claimed_by_other_tenant(tid_b, phone_number_id=numero) is False

    await _alta_numero(tid_b, cuenta_b, numero)  # `claim_assets` de B no se bloquea
    assert await resolve_tenant_for_phone(numero) == tid_b
    conversacion_b = await _entrada(tid_b, uniq("wa-b"), numero)

    async with tenant_scope(tid_a):
        assert await resolve_credentials(
            conversacion_a.connection_id, conversacion_a.meta_connection_id, expected_tenant_id=tid_a
        ) == ("meta", {})
    async with tenant_scope(tid_b):
        assert await resolve_credentials(
            conversacion_b.connection_id, conversacion_b.meta_connection_id, expected_tenant_id=tid_b
        ) == ("meta", {"phone_number_id": numero, "access_token": "tok"})
    # Desde B, la conexión de A no se puede leer: tampoco hay respaldo.
    async with tenant_scope(tid_b):
        assert await resolve_credentials(
            conversacion_a.connection_id, None, expected_tenant_id=tid_b
        ) == ("meta", {})


async def test_editar_una_manual_hacia_un_numero_con_conexion_canonica_da_error_claro() -> None:
    from core.gateway.connections import save_connection
    from core.shared.tenant import tenant_scope

    tid, cuenta, _waba, _phone = await _negocio_con_byok()
    canonico = uniq("ph-canonico")
    await _alta_numero(tid, cuenta, canonico)
    async with tenant_scope(tid):
        manual, _ = await save_connection(
            "Manual", {"phone_number_id": uniq("ph-m"), "access_token": "t", "app_secret": "s", "verify_token": "v"}
        )
        with pytest.raises(ValueError, match="asistente de WhatsApp"):
            await save_connection(
                "Manual",
                {"phone_number_id": canonico, "access_token": "t", "app_secret": "s", "verify_token": "v"},
                manual.id,
            )
