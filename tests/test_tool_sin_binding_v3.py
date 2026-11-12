"""HU-ARCH-34: en v3 solo corre lo que concede la versión del agente (enmienda de ADR-0019). Sin base."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from agentcore_contracts import AgentSpec
from agentcore_contracts.contracts import ToolBinding
from structlog.testing import capture_logs

import core.orchestrator.loop as loop_mod
from core.mcp.types import MCPTool
from core.orchestrator.loop import _execute_tool_safely, _solo_las_concedidas, _tool_denial
from core.shared import release_vigente
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
from core.shared.tenant import tenant_scope

T = uuid.uuid4()
CONEXION = uuid.uuid4()
INSTANCIA = uuid.uuid4()
PROCEDIMIENTO = SimpleNamespace(
    stages=[SimpleNamespace(actions=["crear_pedido"], preconditions=["ver_stock"], confirmActions=["cobrar"])]
)


def make_spec(playbooks: list[dict[str, str]]) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=playbooks,
        tools=[],
        policies={
            "maxToolCallsPerTurn": 10,
        },
        packs=[],
    )


def _agente(procedimientos: tuple[str, ...] = ()) -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=INSTANCIA, agent_id=uuid.uuid4(), key="ventas", role="specialist", version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content={"procedures": [{"id": p, "version": "=0.0.0"} for p in procedimientos]},
    )


def _release(*, foco: bool = True, por_agente: dict[uuid.UUID, Any] | None = None) -> ArtefactosDeRelease:
    return ArtefactosDeRelease(
        tenant_id=T, connection_id=CONEXION, release_id=uuid.uuid4(), lane="stable", idle_minutes=720,
        foco=_agente() if foco else None,
        procedimientos_por_agente=por_agente,
    )


def _tool(nombre: str, plugin_id: str = "core") -> MCPTool:
    return MCPTool(
        name=nombre, description="t", parameters={"type": "object", "properties": {}},
        handler=lambda *a, **k: {"ok": True}, plugin_id=plugin_id,
    )


@pytest.fixture
def registradas(monkeypatch: pytest.MonkeyPatch) -> None:
    herramientas = {n: _tool(n) for n in ("send_email", "crear_pedido", "ver_stock", "cobrar")}
    monkeypatch.setattr(loop_mod.mcp_host, "get_tool", herramientas.get)

    async def _sano(*_a: Any) -> bool:
        return False

    monkeypatch.setattr("core.plugin_manager.plugin_manager.is_disabled_for_tenant", _sano)


@pytest_asyncio.fixture
async def turno() -> AsyncIterator[None]:
    token = release_vigente.abrir()
    try:
        async with tenant_scope(T):
            yield
    finally:
        release_vigente.cerrar(token)


@pytest.mark.usefixtures("registradas", "turno")
async def test_sin_binding_en_v3_se_niega_y_deja_evento() -> None:
    release_vigente.fijar(_release())
    with capture_logs() as eventos:
        assert "no está disponible" in await _tool_denial(make_spec([]), "send_email")
    assert any(e["event"] == "tool_sin_binding" and e["tool"] == "send_email" for e in eventos)


@pytest.mark.usefixtures("registradas", "turno")
async def test_con_binding_corre_y_el_deshabilitado_sigue_negando() -> None:
    release_vigente.fijar(_release())
    spec = make_spec([])
    spec.tools = [ToolBinding(tool="send_email", enabled=True, scopes=[])]
    assert await _tool_denial(spec, "send_email") is None
    spec.tools = [ToolBinding(tool="send_email", enabled=False, scopes=[])]
    assert "deshabilitada" in await _tool_denial(spec, "send_email")


@pytest.mark.usefixtures("registradas", "turno")
async def test_declarativa_de_la_version_concedida_y_la_ajena_negada() -> None:
    release_vigente.fijar(_release())
    spec = make_spec([])
    spec.extensions = {
        "tools": [
            {
                "name": "consulta_propia", "description": "d", "method": "GET", "url": "https://api.ejemplo.com/x",
                "allowed_domains": ["api.ejemplo.com"], "parameters": {"type": "object", "properties": {}},
            }
        ]
    }
    assert await _tool_denial(spec, "consulta_propia") is None
    assert await _tool_denial(spec, "declarativa_ajena") is not None


@pytest.mark.usefixtures("registradas", "turno")
@pytest.mark.parametrize("accion", ["crear_pedido", "ver_stock", "cobrar"])
async def test_declarar_el_procedimiento_concede_sus_acciones(accion: str) -> None:
    release_vigente.fijar(_release(por_agente={INSTANCIA: {"p": PROCEDIMIENTO}}))
    spec = make_spec([])
    assert await _tool_denial(spec, accion) is None
    # La consulta entre agentes exige binding del destino.
    assert await _tool_denial(spec, accion, por_procedimiento=False) is not None
    spec.tools = [ToolBinding(tool=accion, enabled=False, scopes=[])]
    assert "deshabilitada" in await _tool_denial(spec, accion)


@pytest.mark.usefixtures("registradas", "turno")
async def test_release_1_no_concede_por_procedimiento() -> None:
    release_vigente.fijar(_release(por_agente=None))
    assert await _tool_denial(make_spec([]), "crear_pedido") is not None


@pytest.mark.usefixtures("registradas", "turno")
async def test_v3_sin_release_niega_aunque_el_spec_v2_tenga_binding() -> None:
    release_vigente.marcar_v3_sin_release(T)
    spec = make_spec([])
    spec.tools = [ToolBinding(tool="send_email", enabled=True, scopes=[])]
    with capture_logs() as eventos:
        assert await _tool_denial(spec, "send_email") is not None
    assert any(e["event"] == "tool_sin_binding" for e in eventos)


@pytest.mark.usefixtures("registradas", "turno")
async def test_release_sin_foco_niega() -> None:
    release_vigente.fijar(_release(foco=False))
    spec = make_spec([])
    spec.tools = [ToolBinding(tool="send_email", enabled=True, scopes=[])]
    assert await _tool_denial(spec, "send_email") is not None


@pytest.mark.usefixtures("registradas", "turno")
async def test_legacy_corre_y_deja_evento() -> None:
    with capture_logs() as eventos:
        assert await _tool_denial(make_spec([]), "send_email") is None
    assert any(
        e["event"] == "tool_sin_binding_legacy" and e["tool"] == "send_email" and e["tenant_id"] == str(T)
        for e in eventos
    )


@pytest.mark.usefixtures("registradas", "turno")
async def test_la_marca_de_otro_negocio_niega_en_vez_de_abrir() -> None:
    release_vigente.marcar_v3_sin_release(uuid.uuid4())
    assert release_vigente.exige_concesion()
    assert await _tool_denial(make_spec([]), "send_email") is not None


@pytest.mark.usefixtures("registradas", "turno")
async def test_no_se_ofrece_lo_que_se_negaria() -> None:
    release_vigente.fijar(_release(por_agente={INSTANCIA: {"p": PROCEDIMIENTO}}))
    spec = make_spec([])
    spec.tools = [
        ToolBinding(tool="send_email", enabled=True, scopes=[]),
        ToolBinding(tool="cobrar", enabled=False, scopes=[]),
    ]
    ofrecidas = [_tool("send_email"), _tool("crear_pedido"), _tool("cobrar"), _tool("otra")]
    assert [t.name for t in _solo_las_concedidas(spec, ofrecidas)] == ["send_email", "crear_pedido"]


@pytest.mark.usefixtures("turno")
async def test_legacy_ofrece_lo_de_siempre() -> None:
    ofrecidas = [_tool("send_email")]
    assert _solo_las_concedidas(make_spec([]), ofrecidas) == ofrecidas


@pytest.mark.usefixtures("registradas", "turno")
async def test_sin_spec_en_v3_se_niega_y_se_audita(monkeypatch: pytest.MonkeyPatch) -> None:
    auditados: list[dict[str, Any]] = []

    async def _auditar(**kwargs: Any) -> None:
        auditados.append(kwargs)

    monkeypatch.setattr(loop_mod, "record_audit", _auditar)
    release_vigente.fijar(_release())
    llamada = loop_mod.LLMToolCall(id="c1", name="send_email", arguments={})
    conversacion = SimpleNamespace(id=uuid.uuid4(), channel="whatsapp", state={})
    resultado = await _execute_tool_safely(conversacion, SimpleNamespace(id=uuid.uuid4()), llamada, None)
    assert "error" in resultado and auditados[0]["action"] == "denied"


@pytest.mark.usefixtures("turno")
@pytest.mark.parametrize(("runtime", "marcado"), [("v3", True), ("legacy", False)])
async def test_conversacion_sin_conexion_en_v3_queda_marcada(
    monkeypatch: pytest.MonkeyPatch, runtime: str, marcado: bool
) -> None:
    from contextlib import asynccontextmanager

    import core.db.session as session_mod
    import core.kernel.banderas as banderas_mod
    import core.kernel.release_del_turno as rdt

    @asynccontextmanager
    async def _sesion() -> AsyncIterator[None]:
        yield None

    async def _banderas(_s: Any, _t: Any) -> Any:
        return SimpleNamespace(runtime=runtime)

    monkeypatch.setattr(session_mod, "get_session", _sesion)
    monkeypatch.setattr(banderas_mod, "banderas_del_negocio", _banderas)
    conversacion = SimpleNamespace(id=uuid.uuid4(), tenant_id=T, connection_id=None)
    assert await rdt.fijar_release_del_turno(conversacion) is None
    assert release_vigente.exige_concesion() is marcado


@pytest.mark.usefixtures("registradas", "turno")
async def test_procedimiento_abierto_declarado_concede_y_el_no_declarado_no() -> None:
    base = _release(por_agente={INSTANCIA: {}})
    import dataclasses

    release_vigente.fijar(dataclasses.replace(base, procedimientos={"abierto": PROCEDIMIENTO}))
    assert await _tool_denial(make_spec([]), "crear_pedido") is not None
    release_vigente.cerrar(release_vigente.abrir())
    con = dataclasses.replace(base, procedimientos={"abierto": PROCEDIMIENTO}, foco=_agente(("abierto",)))
    release_vigente.fijar(con)
    assert await _tool_denial(make_spec([]), "crear_pedido") is None
