"""HU-AOS-72 · probar el BORRADOR de un agente desde el playground, sin publicarlo.

Por HTTP (`/api/v1/admin/playground` con `agent_id`), con la base, la RLS y el loop REALES; el modelo
va guionado (`resolve_provider`). Comprueba que:

- el turno del borrador responde y no crea release, versión ni instancia (adenda HU-AOS-72 de ADR-0191);
- va directo a ese agente: la tarea que abre no tiene dueño persistido (la instancia efímera no existe en
  la base) y el ledger del turno queda con release e instancia NULL; ninguna FK se rompe;
- la conversación es la prueba del panel (sandbox de HU-ARCH-14) y está aislada de la prueba de siempre;
- el tope de agentes del plan no se consulta (`plan_limit_agentes` nunca sale);
- el agente de otro negocio es 404 y su prueba no se toca; reset y close son por agente.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
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

PG = "/api/v1/admin/playground"
PEDIDO = "pack_commerce.tomar_pedido"
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Comida"]],
    "products": [
        {"name": "Hamburguesa", "description": "Doble carne.", "category_path": ["Comida"],
         "variants": [{"sku": "HAM-1", "price": 25000, "stock": 500, "attributes": {}}]},
    ],
}


class _Guion:
    """Modelo guionado: un paso por llamada, texto o `(tool, args)`."""

    provider_name = "fake"
    model = "fake"
    pricing_per_mtok = (0.0, 0.0)

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.tools: list[list[str]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMToolCall

        self.tools.append(sorted(t.name for t in kwargs.get("tools") or []))
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = types.SimpleNamespace(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return types.SimpleNamespace(
                content="", stop_reason="tool_use", model="fake", latency_ms=1, usage=uso,
                tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=dict(args))],
            )
        return types.SimpleNamespace(content=paso, stop_reason="end_turn", model="fake", latency_ms=1, usage=uso,
                                     tool_calls=[])


async def _alta(pagos: Any, prefijo: str) -> tuple[uuid.UUID, str]:
    slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
    tid = await journeys.preparar_negocio(
        {"nombre": f"Borrador {prefijo}", "packs": ["pack_commerce"], "plan": "premium", "catalogo": CATALOGO},
        pagos,
        slug=slug,
    )
    await journeys.encender_runtime_v3(tid)
    return tid, slug


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return list((await s.execute(text(sql), {"t": tid, **params})).all())


async def _borrador(tid: uuid.UUID) -> uuid.UUID:
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


async def _conteos(tid: uuid.UUID) -> tuple[int, ...]:
    [fila] = await _sql(
        tid,
        "select (select count(*) from connection_releases where tenant_id = :t), "
        "(select count(*) from agent_versions where tenant_id = :t), "
        "(select count(*) from agent_instances where tenant_id = :t)",
    )
    return tuple(fila)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.tenants import deactivate_tenant, purge_tenant

    await journeys._preparar_proceso()
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    a = await _alta(pagos, "borrador-a")
    b = await _alta(pagos, "borrador-b")
    yield a[0], b[0]
    for tid, slug in (a, b):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


@pytest.fixture
def guion(monkeypatch):  # type: ignore[no-untyped-def]
    def poner(pasos: list[Any]) -> _Guion:
        g = _Guion(pasos)
        monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: g)
        return g

    return poner


@pytest.fixture
def sin_tope_de_agentes(monkeypatch):  # type: ignore[no-untyped-def]
    """El plan ya está en su tope de agentes: cualquier consulta del tope falla."""
    from core.billing.enforcement import QuotaExceeded

    async def tope(_tenant_id: Any, key: str, **_k: Any) -> None:
        raise QuotaExceeded(key, 1, 1)

    monkeypatch.setattr("core.billing.enforcement.enforce_growth", tope)


async def test_el_borrador_responde_sin_publicar_ni_consumir_cupo(app_client, negocios, guion, sin_tope_de_agentes, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.kernel import decisions
    from core.shared.test_mode import is_panel_test

    avisos: list[str] = []
    monkeypatch.setattr(decisions.log, "warning", lambda evento, **_k: avisos.append(evento))
    tid, _ = negocios
    agente = await _borrador(tid)
    antes = await _conteos(tid)
    h = await api_headers(app_client, tid)
    g = guion([("iniciar_procedimiento", {"procedure_id": PEDIDO}), "Claro, ¿qué te gustaría pedir?"])

    r = await app_client.post(f"{PG}/message", json={"text": "Quiero una hamburguesa", "agent_id": str(agente)}, headers=h)

    assert r.status_code == 200, r.text
    assert r.json()["text"] == "Claro, ¿qué te gustaría pedir?" and not r.json()["escalated"]
    assert "iniciar_procedimiento" in g.tools[0], "el borrador concede sus procedimientos"
    assert await _conteos(tid) == antes, "probar no crea release, versión ni instancia"
    [(conv, estado, canal)] = await _sql(
        tid, "select id, state, channel from conversations where tenant_id = :t and state->>'draft_agent_id' = :a",
        a=str(agente),
    )
    assert canal == "test" and is_panel_test(types.SimpleNamespace(channel=canal, state=estado)), "sandbox de HU-ARCH-14"
    [(proc, dueno)] = await _sql(
        tid, "select procedure_id, owner_instance_id from conversation_tasks where tenant_id = :t and conversation_id = :c",
        c=conv,
    )
    assert proc == PEDIDO and dueno is None, "la instancia efímera no se persiste como dueño"
    filas = await _sql(
        tid, "select release_id, agent_instance_id from usage_ledger where tenant_id = :t and conversation_id = :c", c=conv
    )
    assert filas and all(f == (None, None) for f in filas), "el costo queda, sin ids que no existen"
    decisiones = await _sql(
        tid, "select release_id, active_instance_id from core_decisions where tenant_id = :t and conversation_id = :c",
        c=conv,
    )
    assert "core_decision_write_failed" not in avisos
    assert decisiones == [], "el turno de borrador no escribe en core_decisions (#951)"


async def test_la_prueba_del_borrador_esta_aislada_y_se_cierra_por_agente(app_client, negocios, guion) -> None:  # type: ignore[no-untyped-def]
    tid, _ = negocios
    agente = await _borrador(tid)
    h = await api_headers(app_client, tid)
    guion(["Hola desde el borrador."])
    r = await app_client.post(f"{PG}/message", json={"text": "hola", "agent_id": str(agente)}, headers=h)
    assert r.status_code == 200, r.text

    propio = await app_client.get(PG, params={"agent_id": str(agente)}, headers=h)
    general = await app_client.get(PG, headers=h)

    assert any("borrador" in str(t) for t in propio.json()["turns"])
    assert not any("borrador" in str(t) for t in general.json()["turns"]), "la prueba de siempre no ve la del borrador"

    assert (await app_client.post(f"{PG}/reset", params={"agent_id": str(agente)}, headers=h)).status_code == 204
    assert (await app_client.get(PG, params={"agent_id": str(agente)}, headers=h)).json()["turns"] == []
    assert (await app_client.post(f"{PG}/close", params={"agent_id": str(agente)}, headers=h)).status_code == 204
    vivas = await _sql(
        tid, "select 1 from conversations where tenant_id = :t and state->>'draft_agent_id' = :a", a=str(agente)
    )
    assert vivas == [], "cerrar purga la prueba del borrador"


async def test_agente_de_otro_negocio_es_404_y_no_toca_su_prueba(app_client, negocios, guion) -> None:  # type: ignore[no-untyped-def]
    tid_a, tid_b = negocios
    ajeno = await _borrador(tid_b)
    hb = await api_headers(app_client, tid_b)
    guion(["Hola B."])
    assert (await app_client.post(f"{PG}/message", json={"text": "hola", "agent_id": str(ajeno)}, headers=hb)).status_code == 200
    ha = await api_headers(app_client, tid_a)

    for r in (
        await app_client.post(f"{PG}/message", json={"text": "hola", "agent_id": str(ajeno)}, headers=ha),
        await app_client.get(PG, params={"agent_id": str(ajeno)}, headers=ha),
        await app_client.post(f"{PG}/reset", params={"agent_id": str(ajeno)}, headers=ha),
    ):
        assert r.status_code == 404 and r.json()["error"]["code"] == "agente_no_encontrado"
        assert str(ajeno) not in r.text
    assert (await app_client.post(f"{PG}/close", params={"agent_id": str(ajeno)}, headers=ha)).status_code == 204
    vivas = await _sql(
        tid_b, "select 1 from conversations where tenant_id = :t and state->>'draft_agent_id' = :a", a=str(ajeno)
    )
    assert len(vivas) == 1, "el close de otro negocio no purga la prueba de B"


async def test_sin_escritura_en_agentes_no_prueba_el_borrador(app_client, negocios) -> None:  # type: ignore[no-untyped-def]
    from test_opciones_api import _headers

    tid, _ = negocios
    agente = await _borrador(tid)
    h = await _headers(app_client, tid, rol="colaborador", secciones={"connections": "write", "settings": "read"})

    r = await app_client.post(f"{PG}/message", json={"text": "hola", "agent_id": str(agente)}, headers=h)

    assert r.status_code == 403


async def test_el_chat_de_prueba_normal_si_escribe_su_decision_con_release(app_client, negocios, guion, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Regresión de #951: el chat de prueba de siempre (sin `agent_id`) corre con la release del negocio, así
    que su cabecera en `core_decisions` sigue escribiéndose, con release, y no falla el CHECK."""
    from core.kernel import decisions

    avisos: list[str] = []
    monkeypatch.setattr(decisions.log, "warning", lambda evento, **_k: avisos.append(evento))
    tid, _ = negocios
    h = await api_headers(app_client, tid)
    guion(["Hola, ¿en qué te ayudo?"])

    r = await app_client.post(f"{PG}/message", json={"text": "hola"}, headers=h)

    assert r.status_code == 200, r.text
    assert "core_decision_write_failed" not in avisos
    [(conv,)] = await _sql(
        tid,
        "select id from conversations where tenant_id = :t and channel = 'test' "
        "and coalesce(state->>'draft_agent_id', '') = '' order by started_at desc limit 1",
    )
    decisiones = await _sql(
        tid, "select outcome, release_id from core_decisions where tenant_id = :t and conversation_id = :c", c=conv
    )
    assert decisiones and all(o == "routed" and rel is not None for o, rel in decisiones)
