"""HU-AOS-103 · corredor de las pruebas del agente en el sandbox (ADR-0212 §2-§5), con la base, la RLS y el loop REALES.

Sin LLM real ni red: el agente, el cliente simulado y el juez son modelos guionados (`_Guion`), uno por papel.

- Una corrida feliz: el agente conversa con el cliente simulado, el juez la aprueba, el gasto se consolida en UNA fila
  `prueba_agente` sin conversación y la conversación se purga (con su cliente sintético).
- Chequeo determinista: una tool prohibida o una esperada ausente fallan SIN llamar al juez.
- Una tool con efectos sin simulador falla cerrada: «no se pudo probar», sin juez y sin tocar la base.
- Tope 1,5× del estimado → `cortada_por_tope`; cancelar a mitad → `cancelada`; en los dos se cobra solo lo gastado.
- Reclamo atómico (una tarea repetida no corre dos veces) y barrido de corridas colgadas, sin cobrar dos veces.
- Aislamiento: escenarios de otro negocio no se corren, nada se consolida ni se purga con el id de otro negocio.
- Rutas: crear encola la corrida; si el broker falla la corrida queda `fallo` y la ruta 503; cancelar.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # dinero y escenarios por negocio (ADR-0212)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
if "journeys" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
    assert _SPEC and _SPEC.loader
    _modulo = importlib.util.module_from_spec(_SPEC)
    sys.modules["journeys"] = _modulo
    _SPEC.loader.exec_module(_modulo)
journeys = sys.modules["journeys"]

PEDIDO = "pack_commerce.tomar_pedido"
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Comida"]],
    "products": [
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [{"sku": "HAM-1", "price": 25000, "stock": 500, "attributes": {}}]},
    ],
}
PASA = '{"pasa": true, "criterio_incumplido": null, "razon": "Cumple en el turno 2."}'
FALLA = '{"pasa": false, "criterio_incumplido": "Confirma la dirección", "razon": "No la pidió (turno 2)."}'


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`. `usd` es el costo de cada llamada."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any], usd: float = 0.01) -> None:
        self.pasos = list(pasos)
        self.usd = usd
        self.llamadas = 0
        self.sistemas: list[list[str]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int, **_k: Any) -> float:
        return self.usd

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.llamadas += 1
        self.sistemas.append(list(kwargs.get("system_blocks") or []))
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = types.SimpleNamespace(input_tokens=100, output_tokens=50)
        if isinstance(paso, tuple):
            nombre, args = paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=uso,
                tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=dict(args))],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", model="fake", latency_ms=1, usage=uso,
                                     tool_calls=[])


@pytest.fixture
def modelos(monkeypatch):  # type: ignore[no-untyped-def]
    """`(agente, cliente, juez)`: cada papel con su guion. El juez y el cliente son de plataforma, el agente del loop."""

    def poner(agente: list[Any], cliente: list[Any], juez: list[Any]) -> tuple[_Guion, _Guion, _Guion]:
        a, c, j = _Guion(agente), _Guion(cliente), _Guion(juez)
        monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *x, **k: a)
        monkeypatch.setattr("core.pruebas.corredor._proveedor_del_simulador", lambda: c)
        monkeypatch.setattr("core.pruebas.juez._proveedor", lambda: j)
        # El arreglo sugerido (HU-AOS-104) es otro papel: aquí no responde (sin arreglo) para no tocar lo del juez.
        monkeypatch.setattr("core.pruebas.arreglo._proveedor", lambda: _Guion([]))
        return a, c, j

    return poner


@pytest.fixture(autouse=True)
def pruebas_encendidas(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "pruebas_agente_enabled", True)
    monkeypatch.setattr(get_settings(), "pruebas_k_maximo", 4)


async def _alta(pagos: Any, prefijo: str) -> tuple[uuid.UUID, str]:
    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(
        {"nombre": f"Corredor {prefijo}", "packs": ["pack_commerce"], "plan": "premium", "catalogo": CATALOGO},
        pagos,
        slug=slug,
    )
    await journeys.encender_runtime_v3(tid)
    return tid, slug


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.tenants import deactivate_tenant, purge_tenant

    await journeys._preparar_proceso()
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    a = await _alta(pagos, "corredor-a")
    b = await _alta(pagos, "corredor-b")
    yield a[0], b[0]
    for tid, slug in (a, b):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture(scope="module")
async def negocio_rutas() -> AsyncIterator[uuid.UUID]:
    """Aparte: las rutas cuentan 5 corridas por día y las de arriba insertan corridas directo."""
    from core.tenants import deactivate_tenant, purge_tenant

    await journeys._preparar_proceso()
    tid, slug = await _alta(journeys.PagoSimulado(monto_minimo=150000), "corredor-rutas")
    yield tid
    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture(autouse=True)
async def sin_corridas_activas(negocios) -> AsyncIterator[None]:  # type: ignore[no-untyped-def]
    """Una corrida activa por negocio: lo que un test deje activo no puede romper al siguiente."""
    yield
    for tid in negocios:
        await _sql(tid, "update agent_test_runs set estado = 'cancelada', terminada_en = now() "
                        "where tenant_id = :t and estado in ('en_cola', 'corriendo') returning id")


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list((await s.execute(text(sql), {"t": tid, **params})).all())


async def _agente(tid: uuid.UUID) -> uuid.UUID:
    """Un agente nuevo, sin publicar, cuyo borrador es el contenido del especialista por defecto."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    [(contenido,)] = await _sql(
        tid,
        "select v.content from agent_instances i join agent_versions v on v.tenant_id = i.tenant_id "
        "and v.agent_id = i.agent_id where i.tenant_id = :t and i.role = 'specialist' and i.is_default "
        "and i.deleted_at is null order by v.version desc limit 1",
    )
    async with tenant_scope(tid):
        agente = await versiones.crear_agente(tid, key=f"prueba_{uuid.uuid4().hex[:8]}", kind="specialist",
                                              name="Agente de prueba")
        await versiones.guardar_borrador(
            tid, agente.id, contenido, 0, catalogo=catalogo_de_acciones(plugin_manager.list_modules())
        )
    return agente.id


async def _caso(tid: uuid.UUID, agente: uuid.UUID, **campos: Any) -> uuid.UUID:
    from core.db.models import AgentTestCase
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    caso = AgentTestCase(
        tenant_id=tid,
        agent_id=agente,
        titulo=f"Escenario {uuid.uuid4().hex[:6]}",
        objetivo_cliente=campos.pop("objetivo_cliente", "Quiere pedir una hamburguesa"),
        criterios=campos.pop("criterios", ["Confirma la dirección"]),
        origen="manual",
        estado="aprobado",
        **campos,
    )
    async with tenant_scope(tid), get_session() as session:
        session.add(caso)
        await session.flush()
        return caso.id


async def _corrida(tid: uuid.UUID, agente: uuid.UUID, casos: list[uuid.UUID], *, k: int = 1,
                   tope: str = "100", estado: str = "en_cola", **extra: Any) -> uuid.UUID:
    from core.db.models import AgentTestRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    corrida = AgentTestRun(
        tenant_id=tid, agent_id=agente, estado=estado, k=k, version_probada={"escenarios": [str(c) for c in casos]},
        creditos_estimados=Decimal("50"), tope_creditos=Decimal(tope), **extra,
    )
    async with tenant_scope(tid), get_session() as session:
        session.add(corrida)
        await session.flush()
        return corrida.id


async def _correr(tid: uuid.UUID, run_id: uuid.UUID) -> str:
    from core.pruebas import corredor

    return await corredor.correr(tid, run_id)


async def _run(tid: uuid.UUID, run_id: uuid.UUID) -> Any:
    [fila] = await _sql(
        tid, "select estado, creditos_reales, version_probada from agent_test_runs where tenant_id = :t and id = :r",
        r=run_id,
    )
    return fila


async def _resultados(tid: uuid.UUID, run_id: uuid.UUID) -> list[Any]:
    return await _sql(
        tid,
        "select estado, fin, tools_llamadas, criterio_incumplido, razon, transcripcion, tenant_id "
        "from agent_test_results where tenant_id = :t and run_id = :r order by created_at",
        r=run_id,
    )


async def _credito_de_la_corrida(tid: uuid.UUID, run_id: uuid.UUID) -> Decimal:
    [(total,)] = await _sql(
        tid,
        "select coalesce(sum(credits), 0) from usage_ledger where tenant_id = :t and kind = 'prueba_agente' "
        "and result->>'run_id' = :r",
        r=str(run_id),
    )
    return Decimal(total)


async def _sin_restos(tid: uuid.UUID, run_id: uuid.UUID) -> None:
    """La corrida no deja conversaciones, mensajes, cliente sintético ni filas del ledger de turnos."""
    assert await _sql(tid, "select 1 from conversations where tenant_id = :t and state->>'prueba_corrida' = :r", r=str(run_id)) == []
    assert await _sql(
        tid, "select 1 from customers where tenant_id = :t and external_id = :e", e=f"panel-prueba-{run_id}"
    ) == []
    assert await _sql(
        tid, "select 1 from usage_ledger where tenant_id = :t and conversation_id is not null and kind <> 'eval' "
        "and conversation_id not in (select id from conversations where tenant_id = :t)",
    ) == []


# ---------------------------------------------------------------------------------------------------- corrida feliz
async def test_una_corrida_aprobada_consolida_el_gasto_en_una_fila_y_purga(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    run = await _corrida(tid, agente, [caso])
    a, c, j = modelos(
        ["Hola, ¿qué te gustaría pedir?", "Listo, ¿a qué dirección lo llevamos?"],
        ["Hola, quiero una hamburguesa", "Calle 1 # 2-3", "[OBJETIVO_CUMPLIDO]"],
        [PASA],
    )

    assert await _correr(tid, run) == "terminada"

    estado, reales, version = await _run(tid, run)
    assert estado == "terminada" and version["version"]["tipo"] == "borrador" and version["version"]["hash"]
    [r] = await _resultados(tid, run)
    assert r[0] == "pasa" and r[1] == "objetivo" and r[6] == tid
    assert [t["quien"] for t in r[5]] == ["cliente", "agente", "cliente", "agente"]
    assert (a.llamadas, c.llamadas, j.llamadas) == (2, 3, 1)
    await _sin_restos(tid, run)
    # Una fila consolidada de turnos + simulador (sin conversación) y la del juez, las dos `prueba_agente`.
    filas = await _sql(
        tid, "select result->>'concepto', conversation_id, credits from usage_ledger where tenant_id = :t "
        "and kind = 'prueba_agente' and result->>'run_id' = :r order by 1", r=str(run),
    )
    assert [f[0] for f in filas] == ["juez", "turnos"] and all(f[1] is None for f in filas)
    assert reales > 0 and Decimal(reales) == await _credito_de_la_corrida(tid, run), "se cobra lo medido, una vez"
    # Cuenta en el cubo del playground (cuota gratis compartida) y no como consumo del negocio aparte.
    async with tenant_scope(tid), get_session() as s:
        consumo = await medir_creditos(s, tid, desde=datetime.now(UTC) - timedelta(days=1))
    assert consumo.playground_usado >= Decimal(reales)
    # El objetivo del escenario viaja como dato aparte, no dentro de las instrucciones del cliente simulado.
    instrucciones, dato = c.sistemas[0]
    assert "hamburguesa" not in instrucciones and dato.startswith("DATOS_DEL_CLIENTE:") and "hamburguesa" in dato


# ------------------------------------------------------------------------------------------- chequeo determinista
async def test_una_tool_prohibida_falla_sin_llamar_al_juez(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente, tools_prohibidas=["iniciar_procedimiento"])
    run = await _corrida(tid, agente, [caso])
    _, _, j = modelos(
        [("iniciar_procedimiento", {"procedure_id": PEDIDO}), "Claro, ¿qué te gustaría pedir?"],
        ["Quiero una hamburguesa", "[OBJETIVO_CUMPLIDO]"],
        [PASA],
    )

    assert await _correr(tid, run) == "terminada"

    [r] = await _resultados(tid, run)
    assert r[0] == "falla" and "iniciar_procedimiento" in r[2] and "iniciar_procedimiento" in r[3]
    assert j.llamadas == 0, "lo determinista no gasta juez"
    await _sin_restos(tid, run)


async def test_una_tool_esperada_que_nunca_se_llamo_falla_sin_juez(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente, tools_esperadas=["iniciar_procedimiento"])
    run = await _corrida(tid, agente, [caso])
    _, _, j = modelos(["Hola, ¿qué te gustaría pedir?"], ["Hola", "[ME_RINDO]"], [PASA])

    await _correr(tid, run)

    [r] = await _resultados(tid, run)
    assert r[0] == "falla" and r[1] == "se_rindio" and "iniciar_procedimiento" in r[3]
    assert j.llamadas == 0


async def test_el_juez_que_no_valida_no_es_una_falla_del_agente(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    run = await _corrida(tid, agente, [caso])
    modelos(["Hola"], ["Hola", "[OBJETIVO_CUMPLIDO]"], ["esto no es JSON"])

    await _correr(tid, run)

    [r] = await _resultados(tid, run)
    assert r[0] == "no_se_pudo_probar", "una salida inválida del juez no se le cuenta al agente"
    _, reales, _ = await _run(tid, run)
    assert Decimal(reales) == await _credito_de_la_corrida(tid, run) > 0, "lo que el proveedor cobró se suma igual"


# --------------------------------------------------------------------------------------- tool sin simulador, fail-closed
async def test_una_tool_con_efectos_sin_simulador_queda_no_se_pudo_probar(negocios, modelos, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.mcp.host import ejecutar_tool
    from core.mcp.types import MCPTool, ToolContext
    from core.pruebas import corredor

    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente, tools_esperadas=["crear_pedido_real"])
    run = await _corrida(tid, agente, [caso])
    _, _, j = modelos([], ["Quiero comprar", "[OBJETIVO_CUMPLIDO]"], [PASA])
    ejecutado = {"handler": 0}

    async def handler(_a: dict[str, Any], _c: ToolContext) -> dict[str, Any]:
        ejecutado["handler"] += 1
        return {"creado": True}

    tool = MCPTool(name="crear_pedido_real", description="x", parameters={"type": "object"}, handler=handler,
                   side_effects=True)

    async def turno(ctx: Any, conversation_id: uuid.UUID, texto: str) -> Any:
        """El agente llama una tool con efectos sin simulador, en el sandbox de la conversación de la corrida."""
        from core.mcp.sandbox import SandboxPrueba

        contexto = ToolContext(conversation_id=conversation_id, customer_id=uuid.uuid4(), idempotency_key="k",
                               channel="test", sandbox=SandboxPrueba(ctx.tenant_id, conversation_id))
        resultado = await ejecutar_tool(tool, {}, contexto)
        assert resultado["error"] == "herramienta_sin_simulador"
        return types.SimpleNamespace(text="No pude crear el pedido.", escalated=False)

    monkeypatch.setattr(corredor, "_turno", turno)

    assert await _correr(tid, run) == "terminada"

    [r] = await _resultados(tid, run)
    assert r[0] == "no_se_pudo_probar" and "crear_pedido_real" in r[4]
    assert ejecutado["handler"] == 0, "nunca se ejecuta lo real"
    assert j.llamadas == 0, "no se cobra el juez"
    await _sin_restos(tid, run)


# --------------------------------------------------------------------------------------------- tope y cancelación
async def test_pasar_del_tope_corta_la_corrida_y_cobra_solo_lo_gastado(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    run = await _corrida(tid, agente, [caso], k=3, tope="0.1")  # el primer intento ya lo pasa
    modelos(["Hola"] * 6, ["Hola", "[OBJETIVO_CUMPLIDO]"] * 3, [PASA] * 3)

    assert await _correr(tid, run) == "cortada_por_tope"

    estado, reales, _ = await _run(tid, run)
    assert estado == "cortada_por_tope" and len(await _resultados(tid, run)) == 1, "no siguió con los otros intentos"
    assert Decimal(reales) == await _credito_de_la_corrida(tid, run) > Decimal("0.1")
    await _sin_restos(tid, run)


async def test_cancelar_a_mitad_termina_el_intento_cobra_lo_gastado_y_no_sigue(negocios, modelos, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.pruebas import corredor, corridas

    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    run = await _corrida(tid, agente, [caso, await _caso(tid, agente)])
    _, c, _ = modelos(["Hola"] * 4, ["Hola", "Otra cosa", "[OBJETIVO_CUMPLIDO]"] * 2, [PASA] * 2)
    original = corredor._turno

    async def turno(ctx: Any, conversation_id: uuid.UUID, texto: str) -> Any:
        resultado = await original(ctx, conversation_id, texto)
        await corridas.cancelar(tid, agente, run, actor="admin:test")  # el dueño cancela tras el primer turno
        return resultado

    monkeypatch.setattr(corredor, "_turno", turno)

    assert await _correr(tid, run) == "cancelada"

    estado, reales, _ = await _run(tid, run)
    assert estado == "cancelada" and await _resultados(tid, run) == [], "no hay resultado a medias"
    assert Decimal(reales) == await _credito_de_la_corrida(tid, run) > 0, "lo gastado hasta ahora sí se cobra"
    assert c.llamadas == 1, "no pidió otro mensaje al cliente tras cancelar"
    await _sin_restos(tid, run)
    with pytest.raises(corridas.ErrorPruebas) as e:
        await corridas.cancelar(tid, agente, run, actor="admin:test")
    assert e.value.code == "corrida_no_activa"


async def test_una_corrida_ya_tomada_no_se_corre_dos_veces(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _agente(tid)
    run = await _corrida(tid, agente, [await _caso(tid, agente)], estado="corriendo", iniciada_en=datetime.now(UTC))
    a, c, _ = modelos(["Hola"], ["Hola"], [PASA])

    assert await _correr(tid, run) == "no_tomada"

    assert (a.llamadas, c.llamadas) == (0, 0) and (await _run(tid, run))[0] == "corriendo"


async def test_con_las_pruebas_apagadas_la_corrida_no_gasta(negocios, modelos, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    tid, _ = negocios
    agente = await _agente(tid)
    run = await _corrida(tid, agente, [await _caso(tid, agente)])
    _, c, _ = modelos(["Hola"], ["Hola"], [PASA])
    monkeypatch.setattr(get_settings(), "pruebas_agente_enabled", False)

    assert await _correr(tid, run) == "fallo"

    assert c.llamadas == 0 and (await _run(tid, run))[2]["fallo"] == "apagada"


# ----------------------------------------------------------------------------------------------------- aislamiento
async def test_los_escenarios_de_otro_negocio_no_se_corren_ni_se_tocan(negocios, modelos) -> None:  # type: ignore[no-untyped-def]
    tid_a, tid_b = negocios
    agente_a, agente_b = await _agente(tid_a), await _agente(tid_b)
    ajeno = await _caso(tid_b, agente_b)
    run = await _corrida(tid_a, agente_a, [ajeno])  # una corrida de A que nombra un escenario de B
    _, c, _ = modelos(["Hola"], ["Hola", "[OBJETIVO_CUMPLIDO]"], [PASA])

    assert await _correr(tid_a, run) == "fallo"

    assert (await _run(tid_a, run))[2]["fallo"] == "sin_escenarios" and c.llamadas == 0
    assert await _sql(tid_b, "select 1 from agent_test_results where tenant_id = :t") == []
    assert await _sql(tid_a, "select 1 from agent_test_results where case_id = :c", c=ajeno) == [], "RLS: A no ve nada de B"


async def test_no_se_consolida_ni_purga_una_conversacion_de_otro_negocio_o_de_otra_corrida(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.pruebas import corredor
    from core.shared.tenant import tenant_scope

    tid_a, tid_b = negocios
    agente_b = await _agente(tid_b)
    caso = await _caso(tid_b, agente_b)
    run_b = await _corrida(tid_b, agente_b, [caso])
    async with tenant_scope(tid_b):
        conexion = await _conexion(tid_b)
        conv = await corredor.crear_conversacion_de_corrida(tid_b, run_b, caso, agente_b, conexion)

    async with tenant_scope(tid_a):
        # Con el negocio equivocado, o con la corrida equivocada, no pasa nada.
        with pytest.raises(corredor.ConversacionAusente):
            await corredor.consolidar_y_purgar(tid_a, conv, run_id=run_b)
    async with tenant_scope(tid_b):
        with pytest.raises(corredor.ConversacionAusente):
            await corredor.consolidar_y_purgar(tid_b, conv, run_id=uuid.uuid4())
    assert len(await _sql(tid_b, "select 1 from conversations where tenant_id = :t and id = :c", c=conv)) == 1
    async with tenant_scope(tid_b):
        await corredor.consolidar_y_purgar(tid_b, conv, run_id=run_b)


async def _conexion(tid: uuid.UUID) -> uuid.UUID:
    from core.db.session import get_session
    from core.gateway.conexion_pruebas import asegurar_conexion_de_pruebas

    async with get_session() as s:
        return await asegurar_conexion_de_pruebas(s, tid)


# ------------------------------------------------------------------------------------------------------- barrido
async def test_el_barrido_cierra_la_corrida_colgada_y_cobra_la_conversacion_huerfana_una_vez(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.db.models import Conversation, UsageLedger
    from core.db.session import get_session
    from core.pruebas import barrido, corredor
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    hace3h = datetime.now(UTC) - timedelta(hours=3)
    colgada = await _corrida(tid, agente, [caso], estado="corriendo", iniciada_en=hace3h)
    async with tenant_scope(tid):
        conexion = await _conexion(tid)
        conv = await corredor.crear_conversacion_de_corrida(tid, colgada, caso, agente, conexion)
    async with tenant_scope(tid), get_session() as s:
        s.add(UsageLedger(tenant_id=tid, conversation_id=conv, kind="turn", model="fake", input_tokens=10,
                          output_tokens=5, cost_usd=Decimal("0.01"), credits=Decimal("2.5"), key_mode="platform", tarifario_version="v2", trm_cop=Decimal("4000"),
                          tool_calls=[{"tool": "x"}]))
        c = await s.get(Conversation, conv)
        assert c is not None
        c.started_at = datetime.now(UTC) - timedelta(hours=2)

    async with tenant_scope(tid):
        hecho = await barrido.barrer_negocio(tid)
        otra_vez = await barrido.barrer_negocio(tid)

    assert hecho == {"corridas": 1, "conversaciones": 1} and otra_vez == {"corridas": 0, "conversaciones": 0}
    estado, reales, version = await _run(tid, colgada)
    assert estado == "fallo" and version["fallo"] == "colgada" and Decimal(reales) == Decimal("2.5")
    assert await _credito_de_la_corrida(tid, colgada) == Decimal("2.5"), "cobrado una sola vez"
    await _sin_restos(tid, colgada)


# ------------------------------------------------------------------------------------------------------- rutas
async def _ruta(app_client, tid: uuid.UUID, agente: uuid.UUID, casos: list[uuid.UUID], k: int = 1) -> Any:  # type: ignore[no-untyped-def]
    h = await api_headers(app_client, tid)
    base = f"/api/v1/admin/agents/{agente}/pruebas"
    e = await app_client.post(f"{base}/estimado", json={"case_ids": [str(c) for c in casos]}, headers=h)
    assert e.status_code == 200, e.text
    estimado = next(x["estimado"] for x in e.json()["por_k"] if x["k"] == k)
    return await app_client.post(
        f"{base}/corridas", json={"case_ids": [str(c) for c in casos], "k": k, "confirmo_creditos": estimado}, headers=h
    )


async def test_crear_la_corrida_la_manda_al_worker_ya_confirmada(app_client, negocio_rutas, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid = negocio_rutas
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    vistas: list[tuple[uuid.UUID, uuid.UUID, int]] = []

    async def en_la_base(t: uuid.UUID, r: uuid.UUID) -> int:
        return len(await _sql(t, "select 1 from agent_test_runs where tenant_id = :t and id = :r", r=r))

    def encolar(t: uuid.UUID, r: uuid.UUID) -> None:
        vistas.append((t, r, 0))

    monkeypatch.setattr("core.tasks.pruebas_tasks.encolar", encolar)
    resp = await _ruta(app_client, tid, agente, [caso])

    assert resp.status_code == 202, resp.text
    run = uuid.UUID(resp.json()["id"])
    assert vistas == [(tid, run, 0)] and await en_la_base(tid, run) == 1
    # Una activa por negocio: el cancelar libera el cupo.
    h = await api_headers(app_client, tid)
    c = await app_client.post(f"/api/v1/admin/agents/{agente}/pruebas/corridas/{run}/cancelar", headers=h)
    assert c.status_code == 200 and c.json()["estado"] == "cancelada"
    otra = await app_client.post(f"/api/v1/admin/agents/{agente}/pruebas/corridas/{run}/cancelar", headers=h)
    assert otra.status_code == 409 and otra.json()["error"]["code"] == "corrida_no_activa"


async def test_si_la_cola_falla_la_corrida_queda_en_fallo_y_no_bloquea_al_negocio(app_client, negocio_rutas, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid = negocio_rutas
    agente = await _agente(tid)
    caso = await _caso(tid, agente)

    def roto(_t: uuid.UUID, _r: uuid.UUID) -> None:
        raise ConnectionError("broker caído")

    monkeypatch.setattr("core.tasks.pruebas_tasks.encolar", roto)
    resp = await _ruta(app_client, tid, agente, [caso])

    assert resp.status_code == 503 and resp.json()["error"]["code"] == "cola_no_disponible"
    [(estado, version)] = await _sql(
        tid, "select estado, version_probada from agent_test_runs where tenant_id = :t and agent_id = :a "
        "order by created_at desc limit 1", a=agente,
    )
    assert estado == "fallo" and version["fallo"] == "cola_no_disponible"
    monkeypatch.setattr("core.tasks.pruebas_tasks.encolar", lambda *_a: None)
    assert (await _ruta(app_client, tid, agente, [caso])).status_code == 202, "el cupo quedó libre"


async def test_cancelar_una_corrida_de_otro_negocio_es_404(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    tid_a, tid_b = negocios
    agente_a, agente_b = await _agente(tid_a), await _agente(tid_b)
    run_b = await _corrida(tid_b, agente_b, [await _caso(tid_b, agente_b)])
    h = await api_headers(app_client, tid_a)

    for agente in (agente_a, agente_b):
        r = await app_client.post(f"/api/v1/admin/agents/{agente}/pruebas/corridas/{run_b}/cancelar", headers=h)
        assert r.status_code == 404 and str(run_b) not in r.text

    assert (await _run(tid_b, run_b))[0] == "en_cola"


async def test_el_barrido_no_purga_la_conversacion_de_una_corrida_viva(negocios) -> None:  # type: ignore[no-untyped-def]
    from core.db.models import Conversation
    from core.db.session import get_session
    from core.pruebas import barrido, corredor
    from core.shared.tenant import tenant_scope

    tid, _ = negocios
    agente = await _agente(tid)
    caso = await _caso(tid, agente)
    viva = await _corrida(tid, agente, [caso], estado="corriendo", iniciada_en=datetime.now(UTC))
    async with tenant_scope(tid):
        conv = await corredor.crear_conversacion_de_corrida(tid, viva, caso, agente, await _conexion(tid))
    async with tenant_scope(tid), get_session() as s:
        c = await s.get(Conversation, conv)
        assert c is not None
        c.started_at = datetime.now(UTC) - timedelta(hours=2)  # vieja, pero su corrida arrancó hace segundos

    async with tenant_scope(tid):
        assert await barrido.barrer_negocio(tid) == {"corridas": 0, "conversaciones": 0}
    assert len(await _sql(tid, "select 1 from conversations where tenant_id = :t and id = :c", c=conv)) == 1
