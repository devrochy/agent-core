"""HU-BILL-26 · campañas, recordatorios y seguimiento de leads por plan, contra la BD real.

Los tres Gherkin: en Basic el envío a segmento responde 409 `plan_feature_not_included` y no sale nada (en Premium
sale); en Free el recordatorio de la cita no sale y queda `failed` con el motivo (en Basic sale); en Basic el
seguimiento automático de leads no envía (en Premium sí). Además: lo frenado no se reenvía al subir de plan, y el
plan de un negocio no decide por otro.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

MOTIVO = "plan_feature_not_included"


async def _negocio(prefijo: str, plan: str, packs: list[str]) -> uuid.UUID:
    from _habilidades import onboard_tenant

    slug = uniq(prefijo)
    tid, _ = await onboard_tenant(
        name=f"Campañas {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=packs, sembrar_ejemplos=False, plan=plan,
    )
    return tid


async def _cambiar_plan(tid: uuid.UUID, plan: str) -> None:
    from core.db.models import TenantSubscription
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            sub = (
                await session.execute(select(TenantSubscription).where(TenantSubscription.tenant_id == tid))
            ).scalar_one()
            sub.plan_id = plan


async def _eventos(tid: uuid.UUID, topic: str) -> list:
    from core.db.models import DomainEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(DomainEvent).where(DomainEvent.tenant_id == tid, DomainEvent.topic == topic)
                    )
                ).scalars().all()
            )


async def _tarjeta(tid: uuid.UUID, cron: str):
    from core.db.models import TenantCronConfig
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return (
                await session.execute(
                    select(TenantCronConfig).where(
                        TenantCronConfig.tenant_id == tid, TenantCronConfig.cron_name == cron
                    )
                )
            ).scalar_one_or_none()


# ---------------------------------------------------------------- envío a segmento


@pytest.fixture
def _sin_guarda_de_test(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.admin import envios_segmento

    monkeypatch.setattr(envios_segmento, "lista_permitida", lambda: None)


async def _envios_del_negocio(tid: uuid.UUID) -> list:
    from core.db.models import SegmentSend
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (await session.execute(select(SegmentSend).where(SegmentSend.tenant_id == tid))).scalars().all()
            )


async def test_basic_el_envio_a_segmento_es_409_y_no_sale_nada(app_client, _sin_guarda_de_test) -> None:
    """Gherkin «Campañas»: en Basic la vista previa y la confirmación dan 409 `plan_feature_not_included`; ni un
    borrador ni un lote. En Premium sale como hoy. El plan de un negocio no decide por el otro."""
    from test_api_envios_a_segmentos import ACC, _cuerpo, _escenario

    basic = await _negocio("camp-basic", "basic", ["pack_commerce"])
    premium = await _negocio("camp-prem", "premium", ["pack_commerce"])
    ids_basic, ids_premium = await _escenario(basic), await _escenario(premium)
    h_basic, h_premium = await api_headers(app_client, basic), await api_headers(app_client, premium)

    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids_basic), headers=h_basic)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == MOTIVO
    assert error["details"] == {"feature": "campanas", "plan": "basic"}
    assert await _envios_del_negocio(basic) == []

    # Premium, al mismo tiempo: sale como hoy.
    r = await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids_premium), headers=h_premium)
    assert r.status_code == 200, r.text
    vista = r.json()
    r = await app_client.post(
        f"{ACC}/{vista['envio_id']}/confirmar", headers={**h_premium, "Idempotency-Key": uniq("clave-p")}
    )
    assert r.status_code == 202, r.text
    assert len(await _eventos(premium, "crm.segment_send.batch")) == 1
    assert await _eventos(basic, "crm.segment_send.batch") == []


async def test_un_borrador_armado_antes_de_bajar_a_basic_no_se_confirma(app_client, _sin_guarda_de_test) -> None:
    from test_api_envios_a_segmentos import ACC, _cuerpo, _escenario

    tid = await _negocio("camp-baja", "premium", ["pack_commerce"])
    ids = await _escenario(tid)
    h = await api_headers(app_client, tid)
    vista = (await app_client.post(f"{ACC}/vista-previa", json=_cuerpo(ids), headers=h)).json()
    assert vista["envio_id"]
    await _cambiar_plan(tid, "basic")

    clave = uniq("clave-b")
    r = await app_client.post(f"{ACC}/{vista['envio_id']}/confirmar", headers={**h, "Idempotency-Key": clave})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == MOTIVO
    (envio,) = await _envios_del_negocio(tid)
    assert envio.estado == "borrador" and envio.idempotency_key is None and envio.confirmed_at is None
    assert await _eventos(tid, "crm.segment_send.batch") == []

    # Lo ya confirmado con derecho se devuelve igual al repetir la clave (idempotencia), aunque luego baje el plan.
    await _cambiar_plan(tid, "premium")
    r = await app_client.post(f"{ACC}/{vista['envio_id']}/confirmar", headers={**h, "Idempotency-Key": clave})
    assert r.status_code == 202, r.text
    await _cambiar_plan(tid, "basic")
    r = await app_client.post(f"{ACC}/{vista['envio_id']}/confirmar", headers={**h, "Idempotency-Key": clave})
    assert r.status_code == 202, r.text
    assert len(await _eventos(tid, "crm.segment_send.batch")) == 1


# ---------------------------------------------------------------- recordatorios de citas


async def _cita_con_conversacion(tid: uuid.UUID, ahora: datetime) -> tuple[uuid.UUID, uuid.UUID]:
    """Cita para dentro de ~24 h (agendada hace 2 días) de un cliente con conversación de WhatsApp."""
    from pack_calendar.models import Appointment

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('c')}", name="Ana")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
            session.add(conv)
            cita = Appointment(
                tenant_id=tid, customer_id=cliente.id, title="Demo",
                start_at=ahora + timedelta(hours=23, minutes=50), end_at=ahora + timedelta(hours=24, minutes=50),
                created_at=ahora - timedelta(days=2),
            )
            session.add(cita)
            await session.flush()
            return cita.id, conv.id


async def _mensajes(tid: uuid.UUID, conversation_id: uuid.UUID) -> list:
    from core.db.models import Message
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return list(
                (
                    await session.execute(
                        select(Message).where(Message.tenant_id == tid, Message.conversation_id == conversation_id)
                    )
                ).scalars().all()
            )


async def _entregar(tid: uuid.UUID, evento) -> None:
    from core.events.core_handlers import on_appointment_reminder
    from core.events.outbox import payload_para_handlers
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await on_appointment_reminder(**payload_para_handlers(evento.payload))


async def test_free_el_recordatorio_no_sale_queda_con_motivo_y_no_se_reenvia_al_subir(monkeypatch) -> None:
    """Gherkin «Recordatorios»: en Free el recordatorio no sale y queda `failed` con el motivo; en Basic sale. Al
    subir de plan no se reenvía (mismo criterio que la pausa, D-76)."""
    from pack_calendar import jobs
    from test_calendar_interno import _configurar

    from core.gateway import event_notices

    free = await _negocio("camp-free", "free", ["pack_calendar"])
    basic = await _negocio("camp-rec", "basic", ["pack_calendar"])
    for tid in (free, basic):
        await _configurar(tid)
    ahora = datetime.now(UTC)
    cita_free, conv_free = await _cita_con_conversacion(free, ahora)
    cita_basic, _ = await _cita_con_conversacion(basic, ahora)

    async def _solo_estos():
        return [free, basic]

    monkeypatch.setattr(jobs, "_tenant_ids", _solo_estos)
    await jobs._recordatorios_de_citas(ahora)

    (ev_free,) = await _eventos(free, "appointment.reminder")
    (ev_basic,) = await _eventos(basic, "appointment.reminder")
    assert ev_free.payload["appointment_id"] == str(cita_free) and ev_free.payload["frenado"] == MOTIVO
    assert ev_basic.payload["appointment_id"] == str(cita_basic) and "frenado" not in ev_basic.payload

    tarjeta = await _tarjeta(free, jobs.CRON_RECORDATORIOS)
    assert tarjeta is not None and tarjeta.last_status == "skipped"
    assert "no enviados" in (tarjeta.last_resumen or "")
    assert (await _tarjeta(basic, jobs.CRON_RECORDATORIOS)).last_status == "success"

    # El core no lo envía: un solo `failed` con el motivo bajo el id estable del aviso, sin tocar el proveedor.
    async def _no_envia(*_a, **_k):
        raise AssertionError("un recordatorio frenado no se envía")

    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _no_envia)
    monkeypatch.setattr("core.gateway.sender.send_template_to_conversation", _no_envia)
    await _entregar(free, ev_free)
    (msg,) = await _mensajes(free, conv_free)
    assert (msg.status, msg.error_reason, msg.client_message_id) == ("failed", MOTIVO, f"appt_reminder:{cita_free}:24")

    # Sube a Basic: el cron no lo vuelve a emitir y una re-entrega del evento no lo envía.
    await _cambiar_plan(free, "basic")
    await jobs._recordatorios_de_citas(ahora + timedelta(minutes=15))
    assert len(await _eventos(free, "appointment.reminder")) == 1
    assert len(await _eventos(basic, "appointment.reminder")) == 1
    resultado: list = []
    real = event_notices.send_event_notice

    async def _espia(*a, **k):
        r = await real(*a, **k)
        resultado.append(r)
        return r

    monkeypatch.setattr(event_notices, "send_event_notice", _espia)
    await _entregar(free, ev_free)
    assert [r.modo for r in resultado] == ["duplicado"]
    assert len(await _mensajes(free, conv_free)) == 1


# ---------------------------------------------------------------- seguimiento de leads


async def test_basic_el_seguimiento_de_leads_no_envia_y_premium_si(monkeypatch) -> None:
    """Gherkin «Seguimiento de leads»: en Basic no envía, el lead no cambia y la tarjeta queda `skipped` con el
    motivo; en Premium sí. Corren juntos: el plan de uno no decide por el otro."""
    from test_leads_reenganche_ventana import _lead_inactivo

    from core.db.models import Lead
    from core.db.session import get_session
    from core.gateway import sender
    from core.leads import config as config_leads
    from core.leads import jobs
    from core.shared.tenant import tenant_scope

    basic = await _negocio("camp-lead-b", "basic", ["pack_commerce"])
    premium = await _negocio("camp-lead-p", "premium", ["pack_commerce"])
    convs = {}
    for tid in (basic, premium):
        async with tenant_scope(tid):
            async with get_session() as session:
                await config_leads.guardar(session, tid, config_leads.ConfigCaptarLeads(follow_up_hours=3))
        convs[tid] = await _lead_inactivo(tid, horas=5)

    enviados: list[uuid.UUID] = []

    async def _enviar(conversation_id, texto, *a, **k):
        enviados.append(conversation_id)
        return True

    async def _solo_estos():
        return [basic, premium]

    async def _si(*_a, **_k):
        return True

    monkeypatch.setattr(sender, "send_to_conversation", _enviar)
    monkeypatch.setattr(jobs, "_negocios_con_captar_leads", _solo_estos)
    monkeypatch.setattr(jobs, "debe_correr", _si)
    await jobs.follow_up_leads()

    assert enviados == [convs[premium]]

    async def _estado(tid):
        async with tenant_scope(tid):
            async with get_session() as session:
                return (await session.execute(select(Lead.status).where(Lead.tenant_id == tid))).scalar_one()

    assert await _estado(basic) == "calificado"
    assert await _estado(premium) == "contactado"
    tarjeta = await _tarjeta(basic, jobs.CRON_LEADS)
    assert tarjeta is not None and tarjeta.last_status == "skipped" and "plan" in (tarjeta.last_resumen or "")
    assert (await _tarjeta(premium, jobs.CRON_LEADS)).last_status == "success"
