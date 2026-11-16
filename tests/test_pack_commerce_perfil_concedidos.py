"""Las tools de Ventas escriben el perfil con los `writeNamespaces` del agente (ADR-0114 §3, ADR-0179 §10).

Hallazgo del auditor multitenant sobre agent-core#816: `crear_lead`/`actualizar_lead` llamaban a
`guardar()` sin `concedidos` y `crear_pedido` escribía el perfil directo, saltándose la concesión y
la puerta del aviso de datos. El registro del pack (lead, pedido) se guarda igual; sin `perfil`
concedido solo se omite la copia al perfil.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from pack_commerce import skills
from pack_commerce.skills import LeadSkill, _guardar_en_perfil

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
    monkeypatch.setattr(skills, "guardar_datos", doble)
    ctx = _ctx(frozenset({"perfil"}))

    await _guardar_en_perfil(ctx, {"name": "Ana", "email": None, "phone": "  "}, accion="ventas.pedido_retiro")

    [llamada] = doble.llamadas
    assert llamada["concedidos"] == frozenset({"perfil"})
    assert llamada["agent_id"] == ctx.agent_id and llamada["customer_id"] == ctx.customer_id
    assert llamada["accion"] == "ventas.pedido_retiro" and llamada["source"] == "tool"
    # Los vacíos no pisan lo que el perfil ya tenía.
    assert llamada["atributos"] == {"name": "Ana"}


@pytest.mark.asyncio
async def test_sin_nada_que_guardar_no_llama(monkeypatch: pytest.MonkeyPatch) -> None:
    doble = _Guardar()
    monkeypatch.setattr(skills, "guardar_datos", doble)
    await _guardar_en_perfil(_ctx(frozenset({"perfil"})), {"email": None, "address": ""})
    assert doble.llamadas == []


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [NamespaceNoConcedido("namespace no concedido: perfil"), HechoInvalido("x"), RuntimeError("bd")])
async def test_la_negativa_no_rompe_la_tool(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    monkeypatch.setattr(skills, "guardar_datos", _Guardar(error))
    await _guardar_en_perfil(_ctx(frozenset()), {"name": "Ana"})


class _Resultado:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> Any:
        return SimpleNamespace(first=lambda: None)


class _Sesion:
    def __init__(self) -> None:
        self.agregados: list[Any] = []

    async def execute(self, *_a: Any, **_k: Any) -> _Resultado:
        return _Resultado()

    def add(self, obj: Any) -> None:
        self.agregados.append(obj)

    async def flush(self) -> None:
        for obj in self.agregados:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()


@pytest.mark.asyncio
async def test_crear_lead_sin_perfil_concedido_crea_el_lead_y_no_copia_al_perfil(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sesion = _Sesion()

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield sesion

    async def _sin_aviso(*_a: Any, **_k: Any) -> None:
        return None

    doble = _Guardar(NamespaceNoConcedido("namespace no concedido: perfil"))
    monkeypatch.setattr(skills, "get_session", _get_session)
    monkeypatch.setattr(skills, "get_current_tenant_id", lambda: uuid.uuid4())
    monkeypatch.setattr(skills, "_avisar_lead_nuevo", _sin_aviso)
    monkeypatch.setattr(skills, "guardar_datos", doble)
    ctx = _ctx(frozenset())

    res = await LeadSkill().crear_lead({"name": "Ana", "phone": "300 123", "interest": "pizza"}, ctx)

    assert res["status"] == "created" and len(sesion.agregados) == 1
    [llamada] = doble.llamadas
    assert llamada["concedidos"] == frozenset()
    assert llamada["operativos"] == skills.OPERATIVOS_DE_LEAD
    assert set(llamada["atributos"]) == {"name", "phone", "crm_lead_id"}
