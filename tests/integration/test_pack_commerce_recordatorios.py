"""HU-CRM-22 · Recordatorios de seguimiento y resumen diario de ventas.

Con la BD real y el job tal como corre en el worker (`_recordar_del_negocio`, con la hora inyectada):
el aviso sale cuando llega la hora del seguimiento y abre la ficha del cliente; sale una sola vez aunque
el job corra mil veces, y otra si la fecha cambia; espera al horario de atención; no avisa de un lead
resuelto ni de lo que el agente creó solo; el resumen sale una vez al día, a su hora local, y callado si
no hay nada; se apaga con la configuración; nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

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

BOGOTA = ZoneInfo("America/Bogota")
CONFIG = SimpleNamespace(seguimiento_recordatorios=True, seguimiento_resumen_diario=True, seguimiento_resumen_hora=8)
# Martes 3 de noviembre de 2026, 10:30 en Bogotá (15:30 UTC).
AHORA = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)


async def _negocio(horario: dict | None = None) -> uuid.UUID:
    from sqlalchemy import select

    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("rec")
    tid, _ = await onboard_tenant(
        name=f"Recordatorios {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (await session.execute(select(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one()
            fila.timezone = "America/Bogota"
            if horario is not None:
                fila.escalation_config = {**(fila.escalation_config or {}), "business_hours": horario}
    return tid


async def _lead(tid: uuid.UUID, nombre: str = "Ana", *, en: datetime | None, nota: str | None = "Llamar", **campos) -> uuid.UUID:
    from pack_commerce.models import Lead

    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(cliente)
            await session.flush()
            lead = Lead(tenant_id=tid, customer_id=cliente.id, next_action_at=en, next_action_note=nota, **campos)
            session.add(lead)
            await session.flush()
            return cliente.id


async def _mover(tid: uuid.UUID, cliente: uuid.UUID, **campos) -> None:
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            lead = (await session.execute(select(Lead).where(Lead.customer_id == cliente))).scalar_one()
            for k, v in campos.items():
                setattr(lead, k, v)


async def _correr(tid: uuid.UUID, ahora: datetime = AHORA, config=CONFIG):
    from pack_commerce.jobs import _recordar_del_negocio

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await _recordar_del_negocio(tid, config, ahora)


async def _avisos(tid: uuid.UUID, evento: str | None = None):
    from sqlalchemy import select

    from core.db.models import Notification
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            consulta = select(Notification).where(Notification.tenant_id == tid).order_by(Notification.created_at, Notification.id)
            if evento:
                consulta = consulta.where(Notification.event_key == evento)
            return list((await session.execute(consulta)).scalars().all())


# ---------------------------------------------------------------- recordatorio


async def test_avisa_cuando_llega_la_hora_y_abre_la_ficha() -> None:
    tid = await _negocio()
    cliente = await _lead(tid, "Ana Pérez", en=AHORA - timedelta(minutes=10), nota="Enviar la cotización")
    conteo, _ = await _correr(tid)
    assert conteo.enviados == 1
    (aviso,) = await _avisos(tid, "commerce.seguimiento_vencido")
    assert aviso.title == "Seguimiento: Ana Pérez"
    assert "Enviar la cotización" in aviso.body and "10:20" in aviso.body  # 15:20 UTC = 10:20 en Bogotá
    assert aviso.target == {"entity": "cliente", "id": str(cliente), "section": "leads"}
    assert aviso.section == "leads" and aviso.category == "ventas"
    assert "Ana" not in str(aviso.data) and "cotización" not in str(aviso.data)  # data lleva solo ids


async def test_un_seguimiento_futuro_todavia_no_avisa() -> None:
    tid = await _negocio()
    await _lead(tid, en=AHORA + timedelta(minutes=5))
    conteo, _ = await _correr(tid)
    assert conteo.enviados == 0
    assert await _avisos(tid, "commerce.seguimiento_vencido") == []


async def test_avisa_una_sola_vez_y_otra_si_la_fecha_cambia() -> None:
    tid = await _negocio()
    cliente = await _lead(tid, en=AHORA - timedelta(minutes=10))
    for _ in range(3):  # el job corre cada 5 minutos, o se reinicia
        await _correr(tid)
    assert len(await _avisos(tid, "commerce.seguimiento_vencido")) == 1
    # Se mueve a una hora nueva que también ya llegó: es otro seguimiento y vuelve a avisar.
    await _mover(tid, cliente, next_action_at=AHORA - timedelta(minutes=2))
    conteo, _ = await _correr(tid)
    assert conteo.enviados == 1
    assert len(await _avisos(tid, "commerce.seguimiento_vencido")) == 2


async def test_fuera_de_horario_espera_a_que_abra_el_negocio() -> None:
    horario = {"tz": "America/Bogota", "ranges": [{"day": d, "from": "08:00", "to": "18:00"} for d in range(7)]}
    tid = await _negocio(horario)
    # Programado a las 02:00 de Bogotá (07:00 UTC) y el job corre a las 03:00 (08:00 UTC): cerrado.
    await _lead(tid, en=datetime(2026, 11, 3, 7, 0, tzinfo=UTC))
    conteo, _ = await _correr(tid, ahora=datetime(2026, 11, 3, 8, 0, tzinfo=UTC))
    assert conteo.enviados == 0 and conteo.saltados_horario == 1
    assert await _avisos(tid, "commerce.seguimiento_vencido") == []
    # A las 08:00 de Bogotá (13:00 UTC) abre y el aviso sale, una sola vez.
    conteo, _ = await _correr(tid, ahora=datetime(2026, 11, 3, 13, 0, tzinfo=UTC))
    assert conteo.enviados == 1
    await _correr(tid, ahora=datetime(2026, 11, 3, 13, 5, tzinfo=UTC))
    assert len(await _avisos(tid, "commerce.seguimiento_vencido")) == 1


async def test_un_seguimiento_de_hace_mas_de_cuatro_dias_no_genera_aviso() -> None:
    tid = await _negocio()
    await _lead(tid, en=AHORA - timedelta(days=5))
    conteo, _ = await _correr(tid)
    assert conteo.enviados == 0


@pytest.mark.parametrize("etapa", ["convertido", "perdido"])
async def test_un_lead_resuelto_no_avisa(etapa: str) -> None:
    tid = await _negocio()
    await _lead(tid, en=AHORA - timedelta(minutes=10), status=etapa)
    conteo, _ = await _correr(tid)
    assert conteo.enviados == 0 and conteo.saltados_resueltos == 1
    assert await _avisos(tid, "commerce.seguimiento_vencido") == []


async def test_un_lead_sin_seguimiento_no_genera_nada() -> None:
    """Lo que crea el agente por su cuenta no tiene fecha: no hay recordatorio que dispare solo."""
    tid = await _negocio()
    await _lead(tid, en=None, nota=None, status="calificado")
    conteo, _ = await _correr(tid, ahora=datetime(2026, 11, 3, 20, 0, tzinfo=UTC))  # 15:00 Bogotá, fuera del resumen
    assert conteo.enviados == 0
    assert await _avisos(tid, "commerce.seguimiento_vencido") == []


async def test_apagar_los_recordatorios_no_toca_el_seguimiento() -> None:
    tid = await _negocio()
    cliente = await _lead(tid, en=AHORA - timedelta(minutes=10))
    apagada = SimpleNamespace(seguimiento_recordatorios=False, seguimiento_resumen_diario=False, seguimiento_resumen_hora=8)
    conteo, resumen = await _correr(tid, config=apagada)
    assert conteo.enviados == 0 and resumen is False
    assert await _avisos(tid) == []
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            lead = (await session.execute(select(Lead).where(Lead.customer_id == cliente))).scalar_one()
    assert lead.next_action_at == AHORA - timedelta(minutes=10) and lead.next_action_note == "Llamar"


async def test_cada_negocio_recibe_solo_lo_suyo() -> None:
    uno, otro = await _negocio(), await _negocio()
    await _lead(uno, "Del uno", en=AHORA - timedelta(minutes=10))
    await _lead(otro, "Del otro", en=AHORA - timedelta(minutes=10))
    await _correr(uno)
    await _correr(otro)
    (a,) = await _avisos(uno, "commerce.seguimiento_vencido")
    (b,) = await _avisos(otro, "commerce.seguimiento_vencido")
    assert a.title == "Seguimiento: Del uno" and b.title == "Seguimiento: Del otro"


# ---------------------------------------------------------------- resumen diario


async def test_resumen_a_las_8_de_su_zona_con_los_conteos_de_la_lista() -> None:
    tid = await _negocio()
    ahora = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)  # 08:00 en Bogotá
    await _lead(tid, "Vencido", en=ahora - timedelta(days=1))
    await _lead(tid, "De hoy", en=ahora + timedelta(hours=3))
    await _lead(tid, "Más adelante", en=ahora + timedelta(days=10))
    await _lead(tid, "Sin seguimiento", en=None, nota=None)
    await _lead(tid, "Resuelto", en=ahora - timedelta(days=1), status="convertido")
    _, resumen = await _correr(tid, ahora=ahora)
    assert resumen is True
    (aviso,) = await _avisos(tid, "commerce.resumen_ventas")
    assert aviso.title == "Tu día de ventas"
    assert aviso.body == "1 seguimiento para hoy · 1 vencido · 5 leads nuevos desde ayer."
    assert aviso.target == {"entity": "lead", "section": "leads", "filtro": "seguimiento:vencidos"}
    assert aviso.data["hoy"] == 1 and aviso.data["vencidos"] == 1


async def test_resumen_sale_una_sola_vez_al_dia_y_otra_al_dia_siguiente() -> None:
    tid = await _negocio()
    ahora = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
    await _lead(tid, en=ahora + timedelta(hours=2))
    for minutos in (0, 5, 60):
        await _correr(tid, ahora=ahora + timedelta(minutes=minutos))
    assert len(await _avisos(tid, "commerce.resumen_ventas")) == 1
    await _correr(tid, ahora=ahora + timedelta(days=1))
    assert len(await _avisos(tid, "commerce.resumen_ventas")) == 2


async def test_resumen_no_sale_antes_de_su_hora_ni_pasada_la_manana() -> None:
    tid = await _negocio()
    await _lead(tid, en=datetime(2026, 11, 3, 20, 0, tzinfo=UTC))
    _, antes = await _correr(tid, ahora=datetime(2026, 11, 3, 12, 55, tzinfo=UTC))  # 07:55 Bogotá
    _, tarde = await _correr(tid, ahora=datetime(2026, 11, 3, 18, 0, tzinfo=UTC))  # 13:00 Bogotá
    assert antes is False and tarde is False
    assert await _avisos(tid, "commerce.resumen_ventas") == []


async def test_resumen_vacio_no_manda_nada() -> None:
    tid = await _negocio()
    _, resumen = await _correr(tid, ahora=datetime(2026, 11, 3, 13, 0, tzinfo=UTC))
    assert resumen is False
    assert await _avisos(tid) == []


async def test_resumen_respeta_la_hora_configurada_y_el_apagado() -> None:
    tid = await _negocio()
    await _lead(tid, en=datetime(2026, 11, 3, 20, 0, tzinfo=UTC))
    a_las_9 = SimpleNamespace(seguimiento_recordatorios=True, seguimiento_resumen_diario=True, seguimiento_resumen_hora=9)
    _, temprano = await _correr(tid, ahora=datetime(2026, 11, 3, 13, 0, tzinfo=UTC), config=a_las_9)  # 08:00 Bogotá
    assert temprano is False
    _, a_tiempo = await _correr(tid, ahora=datetime(2026, 11, 3, 14, 0, tzinfo=UTC), config=a_las_9)  # 09:00 Bogotá
    assert a_tiempo is True
    otro = await _negocio()
    await _lead(otro, en=datetime(2026, 11, 3, 20, 0, tzinfo=UTC))
    sin_resumen = SimpleNamespace(seguimiento_recordatorios=True, seguimiento_resumen_diario=False, seguimiento_resumen_hora=8)
    _, apagado = await _correr(otro, ahora=datetime(2026, 11, 3, 13, 0, tzinfo=UTC), config=sin_resumen)
    assert apagado is False and await _avisos(otro, "commerce.resumen_ventas") == []


async def test_resumen_cuenta_el_dia_del_negocio_no_el_de_utc() -> None:
    """A las 21:30 de Bogotá ya es el día siguiente en UTC; el resumen del 3 sale a las 8:00 del 3 de Bogotá."""
    tid = await _negocio()
    ahora = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)  # 08:00 del 3 en Bogotá
    # 22:00 de Bogotá del 3 = 03:00 UTC del 4: sigue siendo «hoy» para el negocio.
    await _lead(tid, en=datetime(2026, 11, 4, 3, 0, tzinfo=UTC))
    # 00:30 de Bogotá del 4 = 05:30 UTC del 4: ya es mañana.
    await _lead(tid, en=datetime(2026, 11, 4, 5, 30, tzinfo=UTC))
    await _correr(tid, ahora=ahora)
    (aviso,) = await _avisos(tid, "commerce.resumen_ventas")
    assert aviso.data["hoy"] == 1 and aviso.data["vencidos"] == 0
    assert aviso.target["filtro"] == "seguimiento:hoy"
