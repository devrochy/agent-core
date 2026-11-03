"""El reenganche automático de leads sí sale con la configuración por defecto.

Con la BD real y el envío interceptado (sin LLM ni WhatsApp): `follow_up_leads` busca leads «calificado»
con actividad más vieja que `follow_up_hours` **y** dentro de la ventana de 24 h de WhatsApp, la única en
que se puede mandar texto libre. Con `follow_up_hours = 24` (el valor que traía la config) las dos cotas
coincidían y la consulta no devolvía ningún lead: el cron corría cada día y no enviaba nada. Los tests
anteriores usaban `follow_up_hours = 1` y no lo veían.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("reeng")
    tid, _ = await onboard_tenant(
        name=f"Reenganche {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    return tid


async def _lead_inactivo(
    tid: uuid.UUID, *, horas: float, status: str = "calificado", abierta: bool = True, pausado: bool = False
) -> uuid.UUID:
    """Un lead con conversación propia cuya última actividad fue hace `horas`; devuelve el id de la conversación."""
    from pack_commerce.models import Lead

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name="Ana")
            session.add(cliente)
            await session.flush()
            conv = Conversation(
                tenant_id=tid, customer_id=cliente.id, status="open" if abierta else "closed", bot_paused=pausado
            )
            session.add(conv)
            await session.flush()
            session.add(
                Lead(
                    tenant_id=tid, customer_id=cliente.id, conversation_id=conv.id, name="Ana",
                    interest="tortas", status=status,
                    last_activity_at=datetime.now(UTC) - timedelta(hours=horas),
                )
            )
            return conv.id


async def _reenganchar(monkeypatch, tid: uuid.UUID, config) -> tuple[tuple[int, int], list[uuid.UUID]]:
    from pack_commerce.jobs import _follow_up_leads_del_negocio

    from core.gateway import sender
    from core.shared.tenant import tenant_scope

    enviados: list[uuid.UUID] = []

    async def _enviar(conversation_id, texto, *a, **k):
        enviados.append(conversation_id)
        return True

    monkeypatch.setattr(sender, "send_to_conversation", _enviar)
    async with tenant_scope(tid):
        return await _follow_up_leads_del_negocio(tid, config), enviados


def _config(**campos):
    from pack_commerce.config import CRMConfig

    return CRMConfig(**campos)


async def test_con_la_config_por_defecto_un_lead_inactivo_dentro_de_la_ventana_recibe_el_reenganche(monkeypatch) -> None:
    """Config tal como la deja el alta (`save_config(pack, {})`) y cargada como la carga el cron."""
    from pack_commerce.jobs import _config_del_negocio

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    async with tenant_scope(tid):
        config = await _config_del_negocio(tid, "lead")
    assert config is not None, "un negocio recién dado de alta tiene config: el cron no lo salta"
    conv = await _lead_inactivo(tid, horas=config.follow_up_hours + 1)

    (candidatos, enviados_n), enviados = await _reenganchar(monkeypatch, tid, config)

    assert (candidatos, enviados_n, enviados) == (1, 1, [conv])


async def test_un_lead_con_actividad_hace_25_horas_ya_no_se_reengancha(monkeypatch) -> None:
    """Fuera de la ventana de 24 h no se puede mandar texto libre, con cualquier `follow_up_hours`."""
    tid = await _negocio()
    await _lead_inactivo(tid, horas=25)

    for horas in (3, 23):
        (candidatos, _n), enviados = await _reenganchar(monkeypatch, tid, _config(follow_up_hours=horas))
        assert (candidatos, enviados) == (0, [])


async def test_follow_up_hours_3_con_actividad_hace_5_horas_envia(monkeypatch) -> None:
    tid = await _negocio()
    conv = await _lead_inactivo(tid, horas=5)

    (candidatos, enviados_n), enviados = await _reenganchar(monkeypatch, tid, _config(follow_up_hours=3))

    assert (candidatos, enviados_n, enviados) == (1, 1, [conv])


async def test_un_lead_con_menos_horas_de_inactividad_que_las_configuradas_espera(monkeypatch) -> None:
    tid = await _negocio()
    await _lead_inactivo(tid, horas=2)

    (candidatos, _n), enviados = await _reenganchar(monkeypatch, tid, _config(follow_up_hours=3))

    assert (candidatos, enviados) == (0, [])


@pytest.mark.parametrize("horas", [0, 24, 8760])
async def test_una_espera_que_no_cabe_en_la_ventana_se_lee_como_el_valor_por_defecto(monkeypatch, horas: int) -> None:
    """Un negocio que guardó 24 (el default anterior) recupera el seguimiento: nunca había enviado nada."""
    from pack_commerce.config import FOLLOW_UP_HORAS_POR_DEFECTO

    tid = await _negocio()
    conv = await _lead_inactivo(tid, horas=FOLLOW_UP_HORAS_POR_DEFECTO + 1)
    await _lead_inactivo(tid, horas=FOLLOW_UP_HORAS_POR_DEFECTO - 1)  # aún no cumple la espera

    (candidatos, enviados_n), enviados = await _reenganchar(monkeypatch, tid, _config(follow_up_hours=horas))

    assert (candidatos, enviados_n, enviados) == (1, 1, [conv]), f"follow_up_hours={horas}"


@pytest.mark.parametrize(
    "caso", [{"status": "nuevo"}, {"status": "contactado"}, {"abierta": False}, {"pausado": True}]
)
async def test_no_reengancha_si_el_lead_no_esta_calificado_o_la_conversacion_no_admite_bot(monkeypatch, caso) -> None:
    tid = await _negocio()
    await _lead_inactivo(tid, horas=5, **caso)

    (_c, enviados_n), enviados = await _reenganchar(monkeypatch, tid, _config(follow_up_hours=3))

    assert (enviados_n, enviados) == (0, [])
