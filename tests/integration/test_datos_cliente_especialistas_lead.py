"""HU-AOS-93: cada especialista enriquece el lead que el Coordinador ya creó, sin crear un segundo.

Con la BD real: el Coordinador deja el `Customer` y el `Lead` base; un agente de cada tipo (Ventas, Agenda,
Cobranza, Suscripciones) completa el contacto con `actualizar_lead`. Debe quedar UN solo lead, con los datos en el
perfil, y la operación de su módulo debe ver esos datos como ya registrados según su `data_policy`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import func, select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PACKS = ["pack_commerce", "pack_calendar", "pack_cobranza", "pack_subscriptions"]
# tipo de agente → (acción de datos de su módulo, una tool suya)
TIPOS = {
    "ventas": ("ventas.pedido_domicilio", "crear_pedido"),
    "agenda": ("agenda.agendar_cita", "create_calendar_event"),
    "cobranza": ("cobranza.promesa_de_pago", "cob_registrar_promesa_pago"),
    "suscripciones": ("suscripciones.inscribirse", "sub_inscribir"),
}


async def _negocio() -> uuid.UUID:
    from _habilidades import onboard_tenant

    slug = uniq("aos93")
    tid, _ = await onboard_tenant(
        name=f"N {slug}",
        slug=slug,
        persona={"tone": "cercano"},
        admin_email="admin@local",
        packs=PACKS,
        sembrar_ejemplos=False,
    )
    return tid


async def _agente(
    tid: uuid.UUID, key: str, tool: str, data_policy: dict | None = None
) -> uuid.UUID:
    from core.db.models import AgentDraftRecord, AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        agente = AgentRecord(
            id=uuid.uuid4(),
            tenant_id=tid,
            key=key,
            kind="specialist",
            name=key.title(),
            data_policy=data_policy or {},
        )
        session.add(agente)
        await session.flush()
        session.add(
            AgentDraftRecord(
                tenant_id=tid,
                agent_id=agente.id,
                content={"tools": [{"tool": tool, "enabled": True}]},
            )
        )
        return agente.id


async def _cliente_con_lead_del_coordinador(tid: uuid.UUID) -> uuid.UUID:
    """Lo que deja el Coordinador: el cliente y su lead base, sin datos de contacto."""
    from core.db.models import Customer
    from core.db.session import get_session
    from core.leads import servicio
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        cliente = Customer(
            tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:{uniq('c')}", name=""
        )
        session.add(cliente)
        await session.flush()
        await servicio.asegurar(session, tid, cliente.id, origen="whatsapp")
        return cliente.id


def _ctx(customer_id: uuid.UUID, agent_id: uuid.UUID):
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=f"aos93:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
        agent_id=agent_id,
        write_namespaces=frozenset({"perfil"}),
    )


async def _leads_vivos(tid: uuid.UUID, customer_id: uuid.UUID) -> int:
    from core.db.models import Lead
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Lead)
                .where(
                    Lead.tenant_id == tid,
                    Lead.customer_id == customer_id,
                    Lead.deleted_at.is_(None),
                )
            )
        ).scalar_one()


@pytest.mark.parametrize("tipo", sorted(TIPOS))
async def test_el_especialista_enriquece_el_lead_del_coordinador_sin_crear_otro(
    app_client, tipo: str
) -> None:
    from core.datos_cliente import Nivel, niveles_efectivos, registro_datos
    from core.datos_cliente.servicio import evaluar_accion
    from core.leads.tools import LeadSkill
    from core.memory import entity as entity_memory
    from core.shared.tenant import tenant_scope

    accion, tool = TIPOS[tipo]
    tid = await _negocio()
    cliente = await _cliente_con_lead_del_coordinador(tid)
    agente = await _agente(tid, tipo, tool)
    assert await _leads_vivos(tid, cliente) == 1

    async with tenant_scope(tid):
        antes = await evaluar_accion(accion, {}, agent_id=agente)
        r = await LeadSkill().actualizar_lead(
            {"name": "Ana Pérez", "email": "ana@example.test", "phone": "+573001112233"},
            _ctx(cliente, agente),
        )
        perfil = await entity_memory.get_profile(cliente)
        despues = await evaluar_accion(
            accion, {k: v for k, v in perfil.items() if v}, agent_id=agente
        )

    assert r["status"] == "actualizado", r
    assert await _leads_vivos(tid, cliente) == 1, "el especialista no debe crear un segundo lead"
    assert perfil.get("email") == "ana@example.test"
    assert antes is not None and despues is not None
    # La operación del módulo ve como registrado lo que el especialista aportó (sus datos pedidos, no los `no_pedir`).
    pedidos = {
        k
        for k, nivel in niveles_efectivos(registro_datos.obtener(accion)).items()
        if nivel is not Nivel.NO_PEDIR
    }
    aportados = {"name", "email", "phone"} & pedidos
    assert aportados <= set(despues.ya_registrados), (despues, pedidos)
    assert not (aportados & set(despues.faltan))
    assert len(despues.faltan) <= len(antes.faltan)


async def test_la_politica_del_agente_gobierna_lo_que_cuenta_como_faltante(app_client) -> None:
    """Dos agentes de Cobranza del mismo negocio: el que exige el documento lo pide; el otro no."""
    from core.datos_cliente.servicio import evaluar_accion
    from core.shared.tenant import tenant_scope

    accion = "cobranza.promesa_de_pago"
    tid = await _negocio()
    estricto = await _agente(
        tid,
        "cobranza",
        "cob_registrar_promesa_pago",
        {"politica": {accion: {"id_document": "requerido"}}},
    )
    comun = await _agente(tid, "cobranza2", "cob_registrar_promesa_saldo")
    async with tenant_scope(tid):
        ev_estricto = await evaluar_accion(accion, {}, agent_id=estricto)
        ev_comun = await evaluar_accion(accion, {}, agent_id=comun)
    assert ev_estricto is not None and ev_comun is not None
    assert "id_document" in ev_estricto.faltan and not ev_estricto.puede_continuar
    assert "id_document" not in ev_comun.faltan and ev_comun.puede_continuar
