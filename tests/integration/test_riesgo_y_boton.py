"""Política de modelo por riesgo y confirmación con botón, contra el stack real (HU-AOS-09).

Gherkin de la HU:
- **Etapa de riesgo alto:** con la tarea en una etapa `risk: high`, el turno usa el modelo con
  thinking y queda en `usage_ledger` (`result.politica_modelo`).
- **Reintento con respaldo:** tras un rechazo del verificador, el reintento usa `model.fallback`.
- **Botón:** el turno que pide la confirmación entrega un mensaje interactivo cuyo cuerpo sale de
  los argumentos del servidor (no del texto del modelo) y cuyo botón trae el payload firmado; al
  tocarlo, el payload registra `confirmed` sin interpretar texto, y la acción se ejecuta en el
  turno siguiente con los mismos argumentos.
- **Bandera apagada:** nada de lo anterior (mismo proveedor, mismo texto, ledger sin la política).

Modelo guionado (sin LLM real).
"""

from __future__ import annotations

import os
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PACK = "pack_prueba_aos09"
PROC_ID = f"{PACK}.compra"
LLAMADAS: list[tuple[str, dict[str, Any]]] = []

PROCEDIMIENTO = {
    "id": PROC_ID,
    "version": "1.0.0",
    "title": "Compra de prueba",
    "slots": {
        "periodicidad": {"enum": ["mensual", "anual"]},
        "reference": {"type": "string", "setBy": "action:crear_pedido_r"},
    },
    "stages": [
        {"id": "confirmar", "actions": ["cotizar_r", "aceptar_r"], "confirmation": "interactive_button",
         "exitWhen": {"action": "aceptar_r", "ok": True}},
        {"id": "pagar", "risk": "high", "actions": ["crear_pedido_r"], "exitWhen": {"slots": ["reference"]}},
    ],
    "completion": {"events": ["core.payment.confirmed"]},
    "claims": [{"kind": "amount", "mustMatch": ["action:cotizar_r.total"]}],
}


@asynccontextmanager
async def _scope(tid: uuid.UUID | None) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _banderas(tid: uuid.UUID, procedimientos: bool) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_procedures = false where id = :t"), {"t": tid})
            await conn.execute(
                text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
                {"t": tid, "r": "v3" if procedimientos else "legacy", "p": procedimientos},
            )
    finally:
        await engine.dispose()


def _herramienta(nombre: str, resultado: dict[str, Any]) -> Any:
    from core.mcp.types import MCPTool

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        LLAMADAS.append((nombre, dict(args)))
        return dict(resultado)

    return MCPTool(
        name=nombre, description=f"prueba {nombre}",
        parameters={"type": "object", "properties": {"periodicidad": {"type": "string"}, "cantidad": {"type": "integer"}}},
        handler=handler,
    )


@pytest.fixture(scope="module")
def pack_de_prueba() -> Any:
    from agentcore_contracts import Procedure

    from core.mcp.host import mcp_host
    from core.mcp.types import BaseSkill

    class _Skill(BaseSkill):
        name = "prueba_aos09"

        def get_tools(self) -> list[Any]:
            return [
                _herramienta("cotizar_r", {"total": 600000}),
                _herramienta("aceptar_r", {"ok": True}),
                _herramienta("crear_pedido_r", {"reference": "REF-9"}),
            ]

    mcp_host.begin_activation(PACK)
    mcp_host.register_skill(_Skill())
    mcp_host.register_procedures([Procedure.model_validate(PROCEDIMIENTO)])
    mcp_host.end_activation()
    yield
    mcp_host.remove_plugin(PACK)


@pytest.fixture(autouse=True)
def _habilitar(pack_de_prueba: Any, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel.procedures import registro

    monkeypatch.setattr(registro, "_valido", lambda proc: True)
    monkeypatch.setattr("core.orchestrator.loop.enabled_packs", lambda spec: {"core", PACK})

    async def permitidos(tenant_id: uuid.UUID) -> set[str]:
        return {"core", PACK}

    monkeypatch.setattr(registro, "packs_permitidos", permitidos)
    LLAMADAS.clear()


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"aos09-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS09 {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    yield tid
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _conversacion(tid: uuid.UUID, mensaje: str) -> Any:
    from core.db.models import Conversation, Customer, Message

    async with _scope(tid) as s:
        cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:+57300{uuid.uuid4().int % 10**7:07d}")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        s.add(Message(tenant_id=tid, conversation_id=conv.id, direction="inbound", sender_type="customer", content=mensaje))
        return types.SimpleNamespace(id=conv.id, tenant_id=tid, customer_id=cliente.id, channel="whatsapp")


async def _tarea(conv: Any, stage: str) -> uuid.UUID:
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        fila = await state.iniciar(
            s, tenant_id=conv.tenant_id, conversation_id=conv.id, procedure_id=PROC_ID, procedure_version="1.0.0",
            stage=stage, actor="kernel",
        )
        return fila.id


async def _tipos(conv: Any, task: uuid.UUID) -> list[str]:
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        return [e.type for e in await state.eventos_de_tarea(s, tenant_id=conv.tenant_id, conversation_id=conv.id, task_id=task)]


async def _politica_en_ledger(conv: Any) -> list[Any]:
    async with _scope(conv.tenant_id) as s:
        filas = (
            await s.execute(
                text("select model, result from usage_ledger where tenant_id = :t and conversation_id = :c order by created_at"),
                {"t": conv.tenant_id, "c": conv.id},
            )
        ).all()
    return [(f.model, (f.result or {}).get("politica_modelo")) for f in filas]


class _Guion:
    """Modelo guionado: devuelve las respuestas en orden y guarda lo que vio."""

    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any], *, model: str = "deepseek-flash", provider_name: str = "deepseek") -> None:
        self.pasos, self.model, self.provider_name = list(pasos), model, provider_name
        self.llamadas = 0

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMToolCall, LLMUsageInfo

        self.llamadas += 1
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = LLMUsageInfo(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return LLMResponse(content="", stop_reason="tool_use", model=self.model, usage=uso,
                               tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=args)])
        return LLMResponse(content=paso, stop_reason="end_turn", model=self.model, usage=uso)


class _Proveedores:
    """`resolve_provider` guionado: el de riesgo alto (thinking=True), el respaldo (por modelo) y
    el base; guarda cómo se pidió cada uno."""

    def __init__(self, base: _Guion, *, alto: _Guion | None = None, respaldo: _Guion | None = None) -> None:
        self.base, self.alto, self.respaldo = base, alto, respaldo
        self.pedidos: list[dict[str, Any]] = []

    def __call__(self, *a: Any, **k: Any) -> _Guion:
        self.pedidos.append(dict(k))
        if k.get("thinking") is True and self.alto is not None:
            return self.alto
        if self.respaldo is not None and k.get("model") == self.respaldo.model:
            return self.respaldo
        return self.base


async def _turno(conv: Any, proveedores: _Proveedores, monkeypatch, texto: str) -> Any:  # type: ignore[no-untyped-def]
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", proveedores)
    async with tenant_scope(conv.tenant_id):
        return await process_conversation(conv.id, texto)


# ---------------------------------------------------------------- Gherkin


async def test_etapa_de_riesgo_alto_usa_thinking_y_queda_en_el_ledger(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "listo, págalo")
        await _tarea(conv, "pagar")
        base, alto = _Guion(["no debería responder"]), _Guion([("crear_pedido_r", {}), "Tu pedido quedó creado."])
        proveedores = _Proveedores(base, alto=alto)
        resultado = await _turno(conv, proveedores, monkeypatch, "listo, págalo")
    finally:
        await _banderas(negocio, False)
    assert resultado is not None and alto.llamadas == 2 and base.llamadas == 0
    assert any(p.get("thinking") is True and p.get("model") == "deepseek-flash" for p in proveedores.pedidos)
    assert ("crear_pedido_r", {}) in LLAMADAS
    politica = (await _politica_en_ledger(conv))[-1][1]
    assert politica == {"riesgo_alto": 2, "modelo": "deepseek-flash", "thinking": True}


async def test_etapa_normal_no_cambia_el_modelo(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "¿cuánto es?")
        await _tarea(conv, "confirmar")
        base, alto = _Guion(["Te cuento el total enseguida."]), _Guion([])
        proveedores = _Proveedores(base, alto=alto)
        await _turno(conv, proveedores, monkeypatch, "¿cuánto es?")
    finally:
        await _banderas(negocio, False)
    assert base.llamadas == 1 and alto.llamadas == 0
    assert all(p.get("thinking") is not True for p in proveedores.pedidos)


async def test_reintento_del_verificador_usa_el_respaldo(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.orchestrator import loop
    from core.orchestrator.verificador.verificador import Veredicto

    original = loop.load_active_spec

    async def spec_con_respaldo() -> Any:
        spec = await original()
        return spec.model_copy(update={"model": spec.model.model_copy(update={"fallback": "deepseek-v4-pro"})})

    veredictos = iter([Veredicto(texto="x", passed=False, motivo="monto_sin_evidencia")])

    def verificar(borrador: str, *a: Any, **k: Any) -> Veredicto:
        return next(veredictos, Veredicto(texto=borrador))

    monkeypatch.setattr(loop, "load_active_spec", spec_con_respaldo)
    monkeypatch.setattr(loop, "verificar_seguro", verificar)
    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "hola")
        base = _Guion(["El total es $999.999."])
        respaldo = _Guion(["Te confirmo el total enseguida."], model="deepseek-v4-pro")
        resultado = await _turno(conv, _Proveedores(base, respaldo=respaldo), monkeypatch, "hola")
    finally:
        await _banderas(negocio, False)
    assert resultado is not None and resultado.text == "Te confirmo el total enseguida."
    assert base.llamadas == 1 and respaldo.llamadas == 1
    modelo, politica = (await _politica_en_ledger(conv))[-1]
    assert modelo == "deepseek-v4-pro" and politica == {"respaldo_verificador": 1}


async def test_boton_con_resumen_del_servidor_y_confirmacion_por_payload(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.db.models import Message
    from core.kernel.egress import entregar
    from core.kernel.procedures.ejecucion import entrada_sin_llm
    from core.shared.tenant import tenant_scope

    encolados: list[str] = []
    monkeypatch.setattr("core.tasks.outbound_send.outbound_send_task.delay", lambda mid, tid: encolados.append(mid))
    args = {"periodicidad": "anual", "cantidad": 2}
    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "lo quiero anual")
        task = await _tarea(conv, "confirmar")
        guion = _Guion([("cotizar_r", {}), ("aceptar_r", args), "¡Perfecto! Son 3 unidades por $1, ¿confirmas?"])
        resultado = await _turno(conv, _Proveedores(guion), monkeypatch, "lo quiero anual")
        assert resultado is not None and resultado.confirmacion is not None
        # El cliente ve lo que el servidor va a ejecutar, no lo que redactó el modelo.
        assert "3 unidades" not in resultado.text and "$1," not in resultado.text
        assert "Periodicidad: anual" in resultado.text and "Cantidad: 2" in resultado.text
        assert "Total: $600.000" in resultado.text and "confirmo" in resultado.text
        assert ("aceptar_r", args) not in LLAMADAS  # todavía no se ejecuta
        async with tenant_scope(negocio):
            await entregar(conv.id, resultado)
        async with _scope(negocio) as s:
            fila = (await s.execute(
                text("select id, content, interactive from messages where tenant_id = :t and conversation_id = :c "
                     "and direction = 'outbound' order by created_at desc limit 1"),
                {"t": negocio, "c": conv.id},
            )).one()
        assert encolados == [str(fila.id)]
        payload = fila.interactive["buttons"][0]["id"]
        assert fila.interactive["type"] == "button" and payload.startswith("confirmar:")
        assert "Cantidad: 2" in fila.content and "«Confirmar»" in fila.content
        assert (await _tipos(conv, task))[-1] == "confirmation_requested"

        # El cliente toca el botón: el payload confirma sin interpretar texto.
        async with tenant_scope(negocio):
            await entrada_sin_llm(conv, {"interactive_id": payload})
        assert (await _tipos(conv, task))[-1] == "confirmed"

        # Turno siguiente: la misma acción con los mismos argumentos se ejecuta.
        async with _scope(negocio) as s:
            s.add(Message(tenant_id=negocio, conversation_id=conv.id, direction="inbound", sender_type="customer",
                          content="Confirmar"))
        guion = _Guion([("aceptar_r", args), "Listo, quedó confirmado."])
        await _turno(conv, _Proveedores(guion), monkeypatch, "Confirmar")
        assert ("aceptar_r", args) in LLAMADAS
        tipos = await _tipos(conv, task)
        assert "action_succeeded" in tipos and "stage_entered" in tipos
    finally:
        await _banderas(negocio, False)


async def test_dos_cotizaciones_distintas_no_ofrecen_boton(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Fail-closed: si en el turno hubo dos totales distintos, el cliente no confirma ninguno."""
    from core.kernel import confirmacion as conf

    llamadas = iter([{"total": 600000}, {"total": 900000}])

    async def cotizar(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return next(llamadas)

    from core.mcp.host import mcp_host

    herramienta = mcp_host.get_tool("cotizar_r")
    assert herramienta is not None
    monkeypatch.setattr(herramienta, "handler", cotizar)
    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "lo quiero")
        await _tarea(conv, "confirmar")
        guion = _Guion([("cotizar_r", {}), ("cotizar_r", {}), ("aceptar_r", {"cantidad": 1}), "Son $600.000, ¿confirmas?"])
        resultado = await _turno(conv, _Proveedores(guion), monkeypatch, "lo quiero")
    finally:
        await _banderas(negocio, False)
    assert resultado is not None and resultado.confirmacion is not None
    assert resultado.confirmacion.modo == "explicit" and resultado.text == conf.sin_resumen().texto
    assert "600.000" not in resultado.text


async def test_si_a_secas_no_confirma_y_confirmo_si(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.db.models import Message
    from core.kernel.procedures.ejecucion import contexto_del_motor
    from core.mcp.host import mcp_host
    from core.shared.tenant import tenant_scope

    await _banderas(negocio, True)
    try:
        conv = await _conversacion(negocio, "lo quiero")
        task = await _tarea(conv, "confirmar")
        guion = _Guion([("aceptar_r", {"cantidad": 1}), "¿Confirmas?"])
        await _turno(conv, _Proveedores(guion), monkeypatch, "lo quiero")
        async with _scope(negocio) as s:
            s.add(Message(tenant_id=negocio, conversation_id=conv.id, direction="outbound", sender_type="agent", content="x"))
        herramientas = mcp_host.get_skill_tools(["prueba_aos09"])
        for texto, esperado in (("Sí", False), ("Sí, confirmo", True)):
            async with tenant_scope(negocio):
                motor = await contexto_del_motor(
                    conv.id, customer_id=conv.customer_id, channel="whatsapp", enabled={"core", PACK}, disabled=set(),
                    herramientas_base=herramientas, candidatas=herramientas, texto_cliente=texto,
                )
                assert motor is not None
                r = await motor.aplicar_comando("confirmar_tarea", {})
            assert r.ok is esperado, texto
        assert (await _tipos(conv, task))[-1] in ("confirmed", "stage_entered")
    finally:
        await _banderas(negocio, False)


async def test_con_la_bandera_apagada_nada_cambia(negocio: uuid.UUID, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await _banderas(negocio, False)
    conv = await _conversacion(negocio, "lo quiero anual")
    await _tarea(conv, "pagar")  # aunque haya una tarea, el motor no la gobierna
    base, alto = _Guion([("aceptar_r", {"cantidad": 2}), "¿Confirmas 2 unidades?"]), _Guion([])
    proveedores = _Proveedores(base, alto=alto)
    resultado = await _turno(conv, proveedores, monkeypatch, "lo quiero anual")
    assert resultado is not None and resultado.confirmacion is None
    assert resultado.text == "¿Confirmas 2 unidades?"
    assert alto.llamadas == 0 and all(p.get("thinking") is not True for p in proveedores.pedidos)
    assert all(politica is None for _, politica in await _politica_en_ledger(conv))
