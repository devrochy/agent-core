"""Umbrales del difficulty router parametrizables por tenant (ROADMAP,
mapa de parametrización, ítem 1). Mismo patrón exacto que
`guardrail_max_retries`/`disabled_guardrails`: `AppSettings.behavior_config`
(JSONB, sin tocar el contrato `AgentSpec` congelado).

`tests/test_difficulty_router.py` ya cubre `route_model` en sí (puro, sin
BD). Este archivo verifica la parte que ese test no puede: que
`core/orchestrator/loop.py` REALMENTE resuelve y clampea los 3 umbrales
desde la config real de un tenant y se los pasa a `route_model` en cada
turno -- monkeypatcheando puntualmente `route_model` (no `resolve_provider`/
`FakeProvider`, que no exponen qué se le pidió al provider, ver el plan de
esta tanda) para capturar con qué valores lo llamó.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from conftest import PHONE, TEST_ALPHA_ID, _meta_payload
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

T1 = TEST_ALPHA_ID


async def _set_behavior(tenant_id: uuid.UUID, **overrides) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                row = AppSettings(tenant_id=tenant_id, assistant_name="it")
                session.add(row)
            behavior = dict(row.behavior_config or {})
            behavior.update(overrides)
            row.behavior_config = behavior


async def _clear_behavior_keys(tenant_id: uuid.UUID, *keys: str) -> None:
    """Saca `keys` de `behavior_config` (no las deja como `None` -- eso
    seguiría siendo una key PRESENTE, distinta de "sin configurar")."""
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(select(AppSettings).where(AppSettings.tenant_id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                return
            behavior = dict(row.behavior_config or {})
            for key in keys:
                behavior.pop(key, None)
            row.behavior_config = behavior


async def _run_background(seconds: float) -> None:
    from core.orchestrator.consumer import consume_loop

    consumer = asyncio.create_task(consume_loop())
    await asyncio.sleep(seconds)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass


@pytest.fixture
def captured_routing_calls(monkeypatch: pytest.MonkeyPatch):
    import core.orchestrator.loop as loop_mod
    from core.llm.difficulty_router import RoutingDecision

    calls: list[dict] = []
    original = loop_mod.route_model

    def _spy(*args, **kwargs):
        calls.append(kwargs)
        # comportamiento normal: no forzar reasoning, dejar que el turno
        # siga su curso habitual con el FakeProvider/tools disponibles.
        return RoutingDecision(model=kwargs["default_model"], reason="default", allow_tools=True)

    monkeypatch.setattr(loop_mod, "route_model", _spy)
    yield calls
    monkeypatch.setattr(loop_mod, "route_model", original)


_KEYS = (
    "difficulty_long_message_threshold",
    "difficulty_turnos_sin_resolver_threshold",
    "difficulty_score_threshold",
    "debounce_mode",  # también lo setea _set_behavior, limpiarlo también
)


async def test_loop_resuelve_umbrales_custom_desde_behavior_config(
    app_client, tenant_setup, recorded, captured_routing_calls
):
    t1, _ = tenant_setup
    try:
        await _set_behavior(
            t1,
            debounce_mode="inmediato",
            difficulty_long_message_threshold=123,
            difficulty_turnos_sin_resolver_threshold=7,
            difficulty_score_threshold=9,
        )
        recorded.clear()

        resp = await app_client.post(
            "/webhooks/whatsapp",
            json=_meta_payload(
                PHONE, f"wa-diff-{uuid.uuid4().hex[:6]}", f"wamid.DIFF.{uuid.uuid4().hex[:6]}", "hola"
            ),
        )
        assert resp.json()["enqueued"] == 1
        await _run_background(2)

        assert len(captured_routing_calls) == 1
        call = captured_routing_calls[0]
        assert call["long_message_threshold"] == 123
        assert call["turnos_sin_resolver_threshold"] == 7
        assert call["score_threshold"] == 9
    finally:
        await _clear_behavior_keys(t1, *_KEYS)


async def test_loop_clampea_valores_fuera_de_rango(
    app_client, tenant_setup, recorded, captured_routing_calls
):
    """`behavior_config` es JSONB libre -- puede llegar un valor fuera de
    rango por otra vía (script, endpoint futuro) sin pasar por el form del
    admin. `loop.py` debe clampear igual, no solo el form. Incluye el caso
    borde `0` (falsy en Python, pero un valor real a clampear al piso, no a
    reinterpretar como "sin configurar")."""
    t1, _ = tenant_setup
    try:
        await _set_behavior(
            t1,
            debounce_mode="inmediato",
            difficulty_long_message_threshold=999_999,  # tope real: 5000
            difficulty_turnos_sin_resolver_threshold=0,  # piso real: 1
            difficulty_score_threshold=-5,  # piso real: 1
        )
        recorded.clear()

        resp = await app_client.post(
            "/webhooks/whatsapp",
            json=_meta_payload(
                PHONE,
                f"wa-diffclamp-{uuid.uuid4().hex[:6]}",
                f"wamid.DIFFC.{uuid.uuid4().hex[:6]}",
                "hola",
            ),
        )
        assert resp.json()["enqueued"] == 1
        await _run_background(2)

        assert len(captured_routing_calls) == 1
        call = captured_routing_calls[0]
        assert call["long_message_threshold"] == 5000
        assert call["turnos_sin_resolver_threshold"] == 1
        assert call["score_threshold"] == 1
    finally:
        await _clear_behavior_keys(t1, *_KEYS)
