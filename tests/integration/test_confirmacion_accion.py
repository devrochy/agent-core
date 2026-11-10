"""HU-ARCH-36 · confirmación de una acción fuera del gobierno del motor, con la base real.

Un pendiente solo vale si el cliente contestó la pregunta: ni el modelo se confirma solo, ni un reintento,
ni un pendiente viejo. Las pruebas de la cadena (bucle con LLM falso) fijan además que una acción que espera
el sí no cuenta como evidencia de que ocurrió.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import asyncio
import os
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

TOOL = "accion_sensible_prueba"
ARGS = {"pedido": "A-1"}
PARAMETROS = {"type": "object", "properties": {"pedido": {"type": "string"}}}


class Herramienta:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    async def handler(self, arguments: dict[str, Any], _context: Any) -> dict[str, Any]:
        self.llamadas.append(dict(arguments))
        return {"estado": "hecho"}


@pytest.fixture
def herramienta(monkeypatch: pytest.MonkeyPatch) -> Herramienta:
    from core.mcp.host import mcp_host
    from core.mcp.types import MCPTool

    h = Herramienta()
    monkeypatch.setitem(
        mcp_host._tools,
        TOOL,
        MCPTool(name=TOOL, description="Acción de prueba.", parameters=PARAMETROS, handler=h.handler,
                plugin_id="core", sensible=True),
    )

    async def sin_denegacion(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("core.orchestrator.loop._tool_denial", sin_denegacion)
    return h


@pytest.fixture(scope="module")
async def negocio() -> AsyncIterator[uuid.UUID]:
    from core.alta_negocio import onboard_tenant
    from core.tenants import deactivate_tenant, purge_tenant

    slug = f"confa-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Confirma {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    yield tid
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


class Charla:
    """Una conversación con reloj propio: cada mensaje queda después del anterior."""

    def __init__(self, tid: uuid.UUID, conv: Any, cliente: Any) -> None:
        self.tid, self.conv, self.cliente = tid, conv, cliente
        self.reloj = datetime.now(UTC)

    async def mensaje(self, direccion: str, status: str = "sent") -> uuid.UUID:
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        self.reloj += timedelta(seconds=1)
        mid = uuid.uuid4()
        async with tenant_scope(self.tid):
            async with get_session() as s:
                await s.execute(
                    text(
                        "insert into messages (id, tenant_id, conversation_id, direction, sender_type, content, status, created_at) "
                        "values (:id, :t, :c, :d, :s, 'hola', :st, :w)"
                    ),
                    {"id": mid, "t": self.tid, "c": self.conv.id, "d": direccion, "st": status, "w": self.reloj,
                     "s": "customer" if direccion == "inbound" else "agent"},
                )
        return mid

    async def estado(self) -> dict[str, Any]:
        from core.db.models import Conversation
        from core.db.session import get_session
        from core.shared.tenant import tenant_scope

        async with tenant_scope(self.tid):
            async with get_session() as s:
                return dict((await s.get(Conversation, self.conv.id)).state)

    async def llamar(self, *, args: dict[str, Any] | None = None, motor: Any = None, ya_confirmada: bool = False) -> dict[str, Any]:
        from core.agents.spec import load_active_spec
        from core.llm.types import LLMToolCall
        from core.orchestrator.loop import _execute_tool_safely
        from core.shared.tenant import tenant_scope

        llamada = LLMToolCall(id=uuid.uuid4().hex[:8], name=TOOL, arguments=dict(ARGS if args is None else args))
        async with tenant_scope(self.tid):
            spec = await load_active_spec()
            return await _execute_tool_safely(self.conv, self.cliente, llamada, spec, motor=motor, ya_confirmada=ya_confirmada)


async def _charla(tid: uuid.UUID) -> Charla:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"conf:{uuid.uuid4().hex[:8]}")
            s.add(cliente)
            await s.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
            s.add(conv)
            await s.flush()
            s.expunge_all()
    return Charla(tid, conv, cliente)


def _espera(resultado: dict[str, Any]) -> bool:
    return resultado.get("status") == "confirmation_required"


async def _preguntada(c: Charla) -> None:
    """Turno N: el cliente escribe, la acción queda pendiente y la pregunta sale."""
    await c.mensaje("inbound")
    assert _espera(await c.llamar())
    await c.mensaje("outbound")


class _MotorFalso:
    """Lo único que el bloque del bucle lee del motor: ¿gobierna esta acción?"""

    def __init__(self, gobernada: bool) -> None:
        self.gobernada = gobernada

    def es_comando(self, _nombre: str) -> bool:
        return False

    async def antes_de_accion(self, _accion: str, _args: dict[str, Any]) -> Any:
        from core.kernel.procedures.ejecucion import Previo

        return Previo(task_id=uuid.uuid4(), stage="etapa") if self.gobernada else Previo()

    async def tras_accion(self, _accion: str, _args: dict[str, Any], resultado: dict[str, Any], _previo: Any) -> dict[str, Any]:
        return resultado


# ---------------------------------------------------------------- reglas
async def test_sin_binding_la_tool_sensible_no_corre_a_la_primera(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")

    assert _espera(await c.llamar())

    assert herramienta.llamadas == []
    assert (await c.estado())["pending_confirmation"]["tool"] == TOOL


@pytest.mark.parametrize("modo", ["sombra", "activo sin foco"])
async def test_sin_gobierno_de_etapa_pasa_por_el_mismo_bloque(negocio: uuid.UUID, herramienta: Herramienta, modo: str) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")

    assert _espera(await c.llamar(motor=_MotorFalso(gobernada=False))), modo
    assert herramienta.llamadas == []


async def test_el_cliente_responde_y_corre_una_sola_vez(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")

    resultado = await c.llamar()

    assert resultado.get("estado") == "hecho" and herramienta.llamadas == [ARGS]
    assert "pending_confirmation" not in await c.estado()


async def test_dos_llamadas_en_el_mismo_turno_no_se_confirman(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")

    assert _espera(await c.llamar())
    assert _espera(await c.llamar())

    assert herramienta.llamadas == []


async def test_reintento_sin_mensaje_nuevo_no_confirma(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await _preguntada(c)

    assert _espera(await c.llamar())
    assert herramienta.llamadas == []


async def test_si_la_pregunta_no_salio_el_mensaje_siguiente_no_confirma(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")
    assert _espera(await c.llamar())
    await c.mensaje("outbound", status="failed")
    await c.mensaje("inbound")

    assert _espera(await c.llamar())
    assert herramienta.llamadas == []


async def test_un_pendiente_de_hace_dos_turnos_se_vuelve_a_pedir(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")
    await c.mensaje("outbound")
    await c.mensaje("inbound")

    assert _espera(await c.llamar())
    assert herramienta.llamadas == []


async def test_argumentos_distintos_reemplazan_el_pendiente(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")

    assert _espera(await c.llamar(args={"pedido": "B-2"}))

    assert herramienta.llamadas == []
    assert (await c.estado())["pending_confirmation"]["arguments"] == {"pedido": "B-2"}


async def test_la_accion_gobernada_no_usa_este_bloque(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")

    assert (await c.llamar(motor=_MotorFalso(gobernada=True))).get("estado") == "hecho"
    assert herramienta.llamadas == [ARGS]
    assert "pending_confirmation" not in await c.estado()


async def test_el_boton_firmado_no_vuelve_a_preguntar(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    c = await _charla(negocio)
    await c.mensaje("inbound")

    await c.llamar(ya_confirmada=True)

    assert herramienta.llamadas == [ARGS]
    assert "pending_confirmation" not in await c.estado()


async def test_dos_turnos_a_la_vez_ejecutan_una_sola_vez(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    from core.kernel.confirmacion_pendiente import resolver_llamada
    from core.shared.tenant import tenant_scope

    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")

    async def uno() -> bool:
        async with tenant_scope(negocio):
            return await resolver_llamada(negocio, c.conv.id, TOOL, ARGS)

    assert sorted(await asyncio.gather(uno(), uno())) == [False, True]


async def test_el_pendiente_no_pisa_el_resto_del_estado(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    c = await _charla(negocio)
    await c.mensaje("inbound")
    async with tenant_scope(negocio):
        async with get_session() as s:
            fila = await s.get(Conversation, c.conv.id)
            fila.state = {"notes": ["nota del turno"], "turnos_sin_resolver": 2}

    assert _espera(await c.llamar())

    estado = await c.estado()
    assert estado["notes"] == ["nota del turno"] and estado["turnos_sin_resolver"] == 2


async def test_aislamiento_otro_negocio_no_confirma_con_sus_mensajes(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.kernel.confirmacion_pendiente import leer_entrantes, resolver_llamada
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")
    antes = await c.estado()
    slug = f"confb-{uuid.uuid4().hex[:8]}"
    otro, _ = await onboard_tenant(name=f"Otro {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
    try:
        async with tenant_scope(otro):
            async with get_session() as s:
                entrantes = await leer_entrantes(s, otro, c.conv.id)
            assert await resolver_llamada(otro, c.conv.id, TOOL, ARGS) is False
        # Con la sesión del dueño, el filtro por negocio de la consulta es el que responde.
        async with tenant_scope(negocio):
            async with get_session() as s:
                cruzados = await leer_entrantes(s, otro, c.conv.id)
            assert await resolver_llamada(otro, c.conv.id, TOOL, ARGS) is False
        assert entrantes.ultimo is None and entrantes.respondido is None
        assert cruzados.ultimo is None and cruzados.respondido is None
        assert await c.estado() == antes, "el estado del negocio dueño no cambia"
    finally:
        await deactivate_tenant(str(otro))
        await purge_tenant(str(otro), f"ELIMINAR {slug}")


async def test_el_pendiente_vigente_se_muestra_y_el_viejo_no(negocio: uuid.UUID, herramienta: Herramienta) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.mcp.host import mcp_host
    from core.orchestrator.loop import _accion_por_confirmar
    from core.shared.tenant import tenant_scope

    c = await _charla(negocio)
    await _preguntada(c)
    await c.mensaje("inbound")
    tool = mcp_host._tools[TOOL]
    async with tenant_scope(negocio):
        async with get_session() as s:
            conv = await s.get(Conversation, c.conv.id)
        bloque = await _accion_por_confirmar(conv, [tool])
        no_ofrecida = await _accion_por_confirmar(conv, [])
    assert 'pedido="A-1"' in bloque and no_ofrecida == ""

    await c.mensaje("outbound")
    await c.mensaje("inbound")
    async with tenant_scope(negocio):
        async with get_session() as s:
            conv = await s.get(Conversation, c.conv.id)
        assert await _accion_por_confirmar(conv, [tool]) == ""


# ---------------------------------------------------------------- el bucle, con LLM falso
class _Respuesta:
    def __init__(self, stop_reason: str, content: str = "", tool_calls: list[Any] | None = None) -> None:
        self.stop_reason, self.content, self.tool_calls = stop_reason, content, tool_calls or []
        self.model, self.latency_ms = "fake", 1
        self.usage = types.SimpleNamespace(input_tokens=5, output_tokens=5)


class _Guion:
    provider_name = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, *vueltas: list[int] | str) -> None:
        self.vueltas = list(vueltas)

    async def generate(self, **_kwargs: Any) -> _Respuesta:
        vuelta = self.vueltas.pop(0)
        if isinstance(vuelta, str):
            return _Respuesta("end_turn", content=vuelta)
        llamadas = [types.SimpleNamespace(id=f"c{uuid.uuid4().hex[:6]}", name=TOOL, arguments=dict(ARGS)) for _ in range(vuelta[0])]
        return _Respuesta("tool_use", tool_calls=llamadas)

    def estimate_cost_usd(self, *_a: Any) -> float:
        return 0.0


async def _turno(c: Charla, monkeypatch: pytest.MonkeyPatch, guion: _Guion) -> Any:
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    await c.mensaje("inbound")
    async with tenant_scope(c.tid):
        return await process_conversation(c.conv.id, "quiero seguir")


async def test_dos_llamadas_en_un_lote_del_bucle_no_ejecutan(negocio: uuid.UUID, herramienta: Herramienta, monkeypatch: pytest.MonkeyPatch) -> None:
    c = await _charla(negocio)

    await _turno(c, monkeypatch, _Guion([2], "¿Confirmas?"))

    assert herramienta.llamadas == []


async def test_otra_vuelta_del_mismo_turno_no_ejecuta(negocio: uuid.UUID, herramienta: Herramienta, monkeypatch: pytest.MonkeyPatch) -> None:
    c = await _charla(negocio)

    await _turno(c, monkeypatch, _Guion([1], [1], "¿Confirmas?"))

    assert herramienta.llamadas == []


async def test_la_accion_que_espera_no_es_evidencia_ni_cambia_el_contador(negocio: uuid.UUID, herramienta: Herramienta, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.mcp.types import GuardrailResult
    from core.shared.tenant import tenant_scope

    vistos: list[list[dict[str, Any]]] = []

    async def espia(_texto: str, resultados: list[dict[str, Any]], *_a: Any, **_k: Any) -> GuardrailResult:
        vistos.append(resultados)
        return GuardrailResult(passed=True)

    monkeypatch.setattr("core.orchestrator.loop.run_guardrails", espia)
    c = await _charla(negocio)
    async with tenant_scope(negocio):
        async with get_session() as s:
            (await s.get(Conversation, c.conv.id)).state = {"turnos_sin_resolver": 2}

    await _turno(c, monkeypatch, _Guion([1], "Listo, voy a hacerlo."))

    assert vistos and all(TOOL not in {r.get("_tool_name") for r in lote} for lote in vistos)
    assert (await c.estado())["turnos_sin_resolver"] == 2, "ni suma ni reinicia"
