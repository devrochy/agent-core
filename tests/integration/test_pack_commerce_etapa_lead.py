"""HU-CRM-18 · La etapa del lead es confiable y deja historia (ADR-0181).

Con la BD real: `actualizar_lead` y `calificar_lead` (agente), el hook de la ficha (persona) y el
seguimiento automático (sistema) pasan por `pack_commerce.etapas.cambiar_etapa`; cada cambio deja un
registro con etapa anterior, nueva y quién, que aparece en la línea de tiempo; un reintento no lo
duplica; nada cruza negocios.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

URL = "/api/v1/admin/leads"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("etapa")
    tid, _ = await onboard_tenant(
        name=f"Etapa {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local",
        packs=["pack_commerce"], sembrar_ejemplos=False,
    )
    return tid


async def _cliente(tid: uuid.UUID, nombre: str = "Ana") -> tuple[uuid.UUID, uuid.UUID]:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('n')}", name=nombre)
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id)
            session.add(conv)
            await session.flush()
            return cliente.id, conv.id


async def _lead(tid: uuid.UUID, cliente: uuid.UUID, conv: uuid.UUID | None = None, **campos):
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            lead = Lead(tenant_id=tid, customer_id=cliente, conversation_id=conv, **campos)
            session.add(lead)
            await session.flush()
            return lead.id


async def _estado(tid: uuid.UUID, lead_id: uuid.UUID) -> tuple[str, datetime | None]:
    from pack_commerce.models import Lead
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = (await session.execute(select(Lead.status, Lead.stage_changed_at).where(Lead.id == lead_id))).one()
    return fila.status, fila.stage_changed_at


async def _cambios(tid: uuid.UUID, lead_id: uuid.UUID | None = None) -> list[tuple[str, str, str]]:
    from pack_commerce.models import LeadStageChange
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    consulta = select(LeadStageChange.from_stage, LeadStageChange.to_stage, LeadStageChange.actor).order_by(
        LeadStageChange.changed_at, LeadStageChange.id
    )
    if lead_id is not None:
        consulta = consulta.where(LeadStageChange.lead_id == lead_id)
    async with tenant_scope(tid):
        async with get_session() as session:
            return [tuple(f) for f in (await session.execute(consulta)).all()]


def _ctx(cliente: uuid.UUID, conv: uuid.UUID, clave: str | None = None):
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=conv, customer_id=cliente, idempotency_key=clave or uniq("k"), channel="whatsapp"
    )


async def _tool(tid: uuid.UUID, nombre: str, args: dict, cliente: uuid.UUID, conv: uuid.UUID, clave: str | None = None):
    from pack_commerce.skills import LeadSkill

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await getattr(LeadSkill(), nombre)(args, _ctx(cliente, conv, clave))


# ---------------------------------------------------------------- Gherkin
async def test_solo_etapas_validas_y_no_guarda_nada() -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, name="Ana", interest="tortas")

    r = await _tool(tid, "actualizar_lead", {"status": "interesado", "name": "Otra", "interest": "pan"}, cliente, conv)
    assert "interesado" in r["error"]
    assert r["etapas_validas"] == ["nuevo", "calificado", "contactado", "convertido", "perdido"]
    for etapa in r["etapas_validas"]:
        assert etapa in r["error"]
    # No guarda nada: ni la etapa ni el resto de los campos del mismo llamado.
    from pack_commerce.models import Lead

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            fila = await session.get(Lead, lead)
    assert (fila.status, fila.name, fila.interest) == ("nuevo", "Ana", "tortas")
    assert await _cambios(tid, lead) == []

    r = await _tool(tid, "actualizar_lead", {"status": "Contactado "}, cliente, conv)
    assert r["status"] == "actualizado"
    assert (await _estado(tid, lead))[0] == "contactado"
    assert await _cambios(tid, lead) == [("nuevo", "contactado", "agente")]


@pytest.mark.parametrize("etapa", ["contactado", "convertido", "perdido"])
async def test_calificar_no_retrocede(etapa: str) -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, status=etapa, score=2)

    r = await _tool(tid, "calificar_lead", {"score": 8}, cliente, conv)
    assert r["score"] == 8 and r["status"] == etapa
    assert (await _estado(tid, lead))[0] == etapa
    assert await _cambios(tid, lead) == []


async def test_calificar_pasa_un_lead_nuevo_a_calificado() -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv)

    r = await _tool(tid, "calificar_lead", {"score": 7}, cliente, conv)
    assert r["status"] == "calificado"
    estado, desde = await _estado(tid, lead)
    assert estado == "calificado" and desde is not None
    assert await _cambios(tid, lead) == [("nuevo", "calificado", "agente")]


async def test_reabrir_un_perdido_es_de_una_persona(app_client) -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, status="perdido")
    headers = await api_headers(app_client, tid)

    r = await _tool(tid, "actualizar_lead", {"status": "contactado"}, cliente, conv)
    assert "solo una persona" in r["error"]
    assert r["etapa_actual"] == "perdido"
    assert (await _estado(tid, lead))[0] == "perdido"
    assert await _cambios(tid, lead) == []

    r = await app_client.patch(
        f"{URL}/{cliente}/pack/pack_commerce", json={"valores": {"estado_lead": "contactado"}}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert (await _estado(tid, lead))[0] == "contactado"
    assert await _cambios(tid, lead) == [("perdido", "contactado", "persona")]


async def test_historia_en_la_linea_de_tiempo_y_fecha_de_la_etapa(app_client) -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    await _lead(tid, cliente, conv)
    headers = await api_headers(app_client, tid)

    await _tool(tid, "calificar_lead", {"score": 6}, cliente, conv)
    r = await app_client.patch(
        f"{URL}/{cliente}/pack/pack_commerce", json={"valores": {"estado_lead": "convertido"}}, headers=headers
    )
    assert r.status_code == 200, r.text

    eventos = (await app_client.get(f"{URL}/{cliente}/timeline", headers=headers)).json()["items"]
    etapas = [e for e in eventos if e["kind"] == "etapa"]
    assert [(e["title"], e["detail"]) for e in etapas] == [
        ("Etapa: Calificado → Convertido", "Lo hizo una persona"),
        ("Etapa: Nuevo → Calificado", "Lo hizo el agente"),
    ]

    ficha = (await app_client.get(f"{URL}/{cliente}", headers=headers)).json()
    assert ficha["extras"]["etapa_desde_aproximada"] is False
    desde = datetime.fromisoformat(ficha["extras"]["etapa_desde"])
    assert abs(datetime.now(UTC) - desde) < timedelta(minutes=5)


async def test_sin_cambio_no_hay_registro(app_client) -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, status="contactado")
    headers = await api_headers(app_client, tid)

    await _tool(tid, "actualizar_lead", {"status": "contactado", "interest": "pan"}, cliente, conv)
    r = await app_client.patch(
        f"{URL}/{cliente}/pack/pack_commerce", json={"valores": {"estado_lead": "contactado"}}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert await _cambios(tid, lead) == []
    assert (await _estado(tid, lead))[1] is None  # no inventa una fecha


async def test_idempotencia_del_mismo_cambio() -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv)

    await _tool(tid, "actualizar_lead", {"status": "calificado"}, cliente, conv, clave="reintento-1")
    # Alguien lo mueve y llega el reintento de red del primer envío: no deshace ni duplica.
    await _tool(tid, "actualizar_lead", {"status": "convertido"}, cliente, conv, clave="otro-turno")
    await _tool(tid, "actualizar_lead", {"status": "calificado"}, cliente, conv, clave="reintento-1")

    assert (await _estado(tid, lead))[0] == "convertido"
    assert await _cambios(tid, lead) == [("nuevo", "calificado", "agente"), ("calificado", "convertido", "agente")]


async def test_la_clave_de_idempotencia_es_unica_por_negocio() -> None:
    from pack_commerce.models import LeadStageChange
    from sqlalchemy.exc import IntegrityError

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, otro = await _negocio(), await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv)

    def fila(t: uuid.UUID) -> LeadStageChange:
        return LeadStageChange(
            tenant_id=t, lead_id=lead, customer_id=cliente, from_stage="nuevo", to_stage="calificado",
            actor="agente", idempotency_key="misma",
        )

    async with tenant_scope(otro):  # el mismo texto de clave en otro negocio no choca
        async with get_session() as session:
            session.add(fila(otro))
    async with tenant_scope(tid):
        async with get_session() as session:
            session.add(fila(tid))
    with pytest.raises(IntegrityError):
        async with tenant_scope(tid):
            async with get_session() as session:
                session.add(fila(tid))


async def test_el_seguimiento_deja_al_sistema_como_actor(monkeypatch) -> None:
    from pack_commerce.jobs import _follow_up_leads_del_negocio

    from core.gateway import sender
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, name="Ana", status="calificado", last_activity_at=datetime.now(UTC) - timedelta(hours=2))

    enviados: list[str] = []

    async def _enviar(conversation_id, texto, *a, **k):
        enviados.append(texto)

    monkeypatch.setattr(sender, "send_to_conversation", _enviar)
    config = SimpleNamespace(follow_up_hours=1, follow_up_lead_message="Hola{name}, ¿seguimos con {interest}?")
    async with tenant_scope(tid):
        candidatos, enviados_n = await _follow_up_leads_del_negocio(tid, config)

    assert (candidatos, enviados_n) == (1, 1) and len(enviados) == 1
    assert (await _estado(tid, lead))[0] == "contactado"
    assert await _cambios(tid, lead) == [("calificado", "contactado", "sistema")]


async def test_el_seguimiento_no_pisa_lo_que_cambio_una_persona(monkeypatch) -> None:
    from pack_commerce.jobs import _follow_up_leads_del_negocio

    from core.gateway import sender
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, status="calificado", last_activity_at=datetime.now(UTC) - timedelta(hours=2))

    async def _enviar(conversation_id, texto, *a, **k):
        # Mientras sale el mensaje, la persona marca el lead como convertido.
        from pack_commerce.etapas import cambiar_etapa
        from pack_commerce.models import Lead

        from core.db.session import get_session

        async with get_session() as session:
            await cambiar_etapa(session, await session.get(Lead, lead), "convertido", actor="persona")

    monkeypatch.setattr(sender, "send_to_conversation", _enviar)
    config = SimpleNamespace(follow_up_hours=1, follow_up_lead_message="Hola{name}")
    async with tenant_scope(tid):
        await _follow_up_leads_del_negocio(tid, config)

    assert (await _estado(tid, lead))[0] == "convertido"
    assert await _cambios(tid, lead) == [("calificado", "convertido", "persona")]


async def test_leads_de_antes_siguen_igual_y_la_fecha_es_aproximada(app_client) -> None:
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    ultima = datetime.now(UTC) - timedelta(days=9)
    lead = await _lead(tid, cliente, conv, status="calificado", last_activity_at=ultima)
    headers = await api_headers(app_client, tid)

    ficha = (await app_client.get(f"{URL}/{cliente}", headers=headers)).json()
    assert ficha["extras"]["estado_lead"] == "Calificado"
    assert ficha["extras"]["etapa_desde_aproximada"] is True
    assert datetime.fromisoformat(ficha["extras"]["etapa_desde"]) == ultima
    eventos = (await app_client.get(f"{URL}/{cliente}/timeline", headers=headers)).json()["items"]
    assert [e for e in eventos if e["kind"] == "etapa"] == []
    assert await _cambios(tid, lead) == []


async def test_un_lead_con_etapa_antigua_no_se_rompe_al_cambiarla() -> None:
    """Un texto libre de antes de la HU como etapa de partida: se puede salir de él, queda en la historia."""
    tid = await _negocio()
    cliente, conv = await _cliente(tid)
    lead = await _lead(tid, cliente, conv, status="interesado")

    r = await _tool(tid, "actualizar_lead", {"status": "calificado"}, cliente, conv)
    assert r["status"] == "actualizado"
    assert await _cambios(tid, lead) == [("interesado", "calificado", "agente")]


async def test_los_cambios_no_cruzan_negocios(app_client) -> None:
    tid, otro = await _negocio(), await _negocio()
    c1, conv1 = await _cliente(tid)
    c2, conv2 = await _cliente(otro)
    l1 = await _lead(tid, c1, conv1)
    l2 = await _lead(otro, c2, conv2)

    await _tool(tid, "calificar_lead", {"score": 9}, c1, conv1)

    assert await _cambios(tid) == [("nuevo", "calificado", "agente")]
    assert await _cambios(otro) == []
    assert (await _estado(otro, l2))[0] == "nuevo"
    assert (await _estado(tid, l1))[0] == "calificado"
    headers = await api_headers(app_client, otro)
    eventos = (await app_client.get(f"{URL}/{c2}/timeline", headers=headers)).json()["items"]
    assert [e for e in eventos if e["kind"] == "etapa"] == []
    # Y sin negocio en contexto la RLS no deja ver nada.
    from pack_commerce.models import LeadStageChange
    from sqlalchemy import func, select

    from core.db.session import get_session

    async with get_session() as session:
        assert (await session.execute(select(func.count()).select_from(LeadStageChange))).scalar() == 0
