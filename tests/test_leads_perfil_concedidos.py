"""Las tools de Captar leads escriben el perfil con los `writeNamespaces` del agente (ADR-0114 §3, ADR-0179 §10).

Hallazgo del auditor multitenant sobre agent-core#816 (cuando vivían en commerce): `crear_lead`/`actualizar_lead`
llamaban a `guardar()` sin `concedidos`. El lead se guarda igual; sin `perfil` concedido solo se omite la copia al
perfil. Desde ADR-0193 son del core (`core.leads.tools`).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from core.leads import tools
from core.leads.tools import LeadSkill, _guardar_en_perfil
from core.mcp.types import ToolContext
from core.memory.facts import HechoInvalido, NamespaceNoConcedido


def _ctx(write_namespaces: frozenset[str] | None) -> ToolContext:
    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
        agent_id=uuid.uuid4(),
        write_namespaces=write_namespaces,
    )


class _Guardar:
    """Doble de `datos_cliente.guardar` que anota cada llamada y opcionalmente falla."""

    def __init__(self, error: Exception | None = None) -> None:
        self.llamadas: list[dict[str, Any]] = []
        self.error = error

    async def __call__(self, customer_id: uuid.UUID, atributos: dict[str, Any], **kw: Any) -> None:
        self.llamadas.append({"customer_id": customer_id, "atributos": dict(atributos), **kw})
        if self.error is not None:
            raise self.error


@pytest.mark.asyncio
async def test_pasa_los_concedidos_y_el_agente_del_contexto(monkeypatch: pytest.MonkeyPatch) -> None:
    doble = _Guardar()
    monkeypatch.setattr(tools, "guardar_datos", doble)
    ctx = _ctx(frozenset({"perfil"}))

    await _guardar_en_perfil(ctx, {"name": "Ana", "email": None, "phone": "  "})

    [llamada] = doble.llamadas
    assert llamada["concedidos"] == frozenset({"perfil"})
    assert llamada["agent_id"] == ctx.agent_id and llamada["customer_id"] == ctx.customer_id
    assert llamada["operativos"] == tools.OPERATIVOS_DE_LEAD and llamada["source"] == "tool"
    # Los vacíos no pisan lo que el perfil ya tenía.
    assert llamada["atributos"] == {"name": "Ana"}


@pytest.mark.asyncio
async def test_sin_nada_que_guardar_no_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    doble = _Guardar()
    monkeypatch.setattr(tools, "guardar_datos", doble)
    await _guardar_en_perfil(_ctx(frozenset({"perfil"})), {"email": None, "phone": ""})
    assert doble.llamadas == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [NamespaceNoConcedido("namespace no concedido: perfil"), HechoInvalido("x"), RuntimeError("bd")])
async def test_la_negativa_no_rompe_la_tool(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(tools, "guardar_datos", _Guardar(error))
    await _guardar_en_perfil(_ctx(frozenset()), {"name": "Ana"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("creado", "nombre_previo", "status"),
    [(True, None, "created"), (False, None, "created"), (False, "Ana María", "actualizado")],
)
async def test_crear_lead_sin_perfil_concedido_guarda_el_lead_y_no_copia_al_perfil(
    monkeypatch: pytest.MonkeyPatch, creado: bool, nombre_previo: str | None, status: str
) -> None:
    lead = SimpleNamespace(id=uuid.uuid4(), customer_id=None, name=nombre_previo, phone=None, email=None, interest="")
    avisos: list[uuid.UUID] = []

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield object()

    async def _asegurar(_s: Any, _t: Any, customer_id: uuid.UUID, **_k: Any) -> tuple[Any, bool]:
        lead.customer_id = customer_id
        return lead, creado

    async def _aviso(_s: Any, lead_id: uuid.UUID, *_a: Any) -> None:
        avisos.append(lead_id)

    doble = _Guardar(NamespaceNoConcedido("namespace no concedido: perfil"))
    monkeypatch.setattr(tools, "get_session", _get_session)
    monkeypatch.setattr(tools, "get_current_tenant_id", lambda: uuid.uuid4())
    monkeypatch.setattr(tools.servicio, "asegurar", _asegurar)
    monkeypatch.setattr(tools, "avisar_lead_nuevo", _aviso)
    monkeypatch.setattr(tools, "guardar_datos", doble)
    ctx = _ctx(frozenset())

    res = await LeadSkill().crear_lead({"name": "Ana", "phone": "300 123", "interest": "pizza"}, ctx)

    # ADR-0193: `created` si el lead nació en esta llamada o recibe sus primeros datos de contacto (el turno ya lo
    # había asegurado vacío); si ya tenía datos, `actualizado` con los datos puestos.
    assert res == {"lead_id": str(lead.id), "status": status}
    assert (lead.name, lead.phone, lead.interest) == ("Ana", "300 123", "pizza")
    assert avisos == ([lead.id] if creado else [])
    [llamada] = doble.llamadas
    assert llamada["concedidos"] == frozenset()
    assert llamada["operativos"] == tools.OPERATIVOS_DE_LEAD
    assert set(llamada["atributos"]) == {"name", "phone", "crm_lead_id"}
