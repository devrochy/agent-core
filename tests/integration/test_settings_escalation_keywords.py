"""`AppSettings.escalation_config["keywords"]` editable desde
`/admin/configuracion` (ROADMAP, mapa de parametrización, ítem 7): hasta esta
tanda, la ÚNICA superficie editable de "palabras de escalamiento" era
`AgentSpec.policies.escalation.triggers` -- un campo del contrato que el
runtime nunca lee (ver `core/orchestrator/consumer.py::matches_escalation_keywords`,
que lee `escalation_config`). Este test confirma que `settings_save` ahora
escribe donde el runtime REALMENTE lee.

`tests/integration/test_tenant_isolation.py` ya cubre el otro extremo del
mismo bug: el wizard de alta de tenants (`/admin/tenants/nuevo`).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _cookies(app_client, tenant_id) -> tuple[str, str]:
    """Login + switch a un tenant. Devuelve (session_cookie, ac_tenant_cookie)."""
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    session = r.headers["set-cookie"].split(";")[0]
    headers = {"Cookie": session}
    r2 = await app_client.post(
        "/admin/switch-tenant", headers=headers, data={"tenant_id": str(tenant_id)}
    )
    tenant_cookie = [c.split(";")[0] for c in r2.headers.get_list("set-cookie") if "ac_tenant" in c]
    return session, tenant_cookie[0]


async def _snapshot(tenant_id) -> dict:
    """`settings_save` reescribe TODA la fila de `AppSettings` (no solo
    `escalation_config`) -- se guarda la fila entera para restaurarla, no
    ensuciar `tenant-moto` (fila compartida) para otros archivos de test."""
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == tenant_id)
                )
            ).scalar_one()
            return {
                "assistant_name": row.assistant_name,
                "business_description": row.business_description,
                "brand_voice": row.brand_voice,
                "behavior_config": dict(row.behavior_config or {}),
                "escalation_config": dict(row.escalation_config or {}),
            }


async def _restore(tenant_id, snapshot: dict) -> None:
    from sqlalchemy import select

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == tenant_id)
                )
            ).scalar_one()
            row.assistant_name = snapshot["assistant_name"]
            row.business_description = snapshot["business_description"]
            row.brand_voice = snapshot["brand_voice"]
            row.behavior_config = snapshot["behavior_config"]
            row.escalation_config = snapshot["escalation_config"]


async def test_settings_save_escribe_escalation_config_no_el_spec_muerto(
    app_client, moto_setup
) -> None:
    """Opera sobre tenant-moto (mismo criterio que test_spec_editor.py: no
    ensuciar el spec/config de T1)."""
    _t1, t2 = moto_setup
    session, ac_tenant = await _cookies(app_client, t2)
    headers = {"Cookie": f"{session}; {ac_tenant}"}

    snapshot = await _snapshot(t2)
    behavior = snapshot["behavior_config"]
    # `settings_save` reescribe TODO `behavior_config` con lo que venga en el
    # form (cae a sus propios defaults si un campo no llega) -- hay que
    # reenviar los valores actuales, no solo lo que este test quiere cambiar,
    # para no resetear brevemente la config real de `tenant-moto` (fila
    # compartida, posiblemente con tráfico real o tests en paralelo) durante
    # la ventana hasta el `finally`. `expected_behavior` es exactamente lo
    # que `settings_save` va a persistir a partir de estos valores (puede
    # ganar keys que `behavior` no tenía si nunca se había guardado desde el
    # panel para este tenant -- no es un reset, es la primera vez que esas
    # keys quedan explícitas).
    expected_behavior = {
        **behavior,
        "tono": behavior.get("tono", "calido"),
        "debounce_mode": behavior.get("debounce_mode", "natural"),
        "guardrail_max_retries": max(0, behavior.get("guardrail_max_retries", 1)),
        "disabled_guardrails": sorted(behavior.get("disabled_guardrails", [])),
        "difficulty_long_message_threshold": behavior.get("difficulty_long_message_threshold", 400),
        "difficulty_turnos_sin_resolver_threshold": behavior.get(
            "difficulty_turnos_sin_resolver_threshold", 2
        ),
        "difficulty_score_threshold": behavior.get("difficulty_score_threshold", 2),
    }
    try:
        r = await app_client.post(
            "/admin/configuracion/asistente",
            headers=headers,
            data={
                "assistant_name": snapshot["assistant_name"],
                "business_description": snapshot["business_description"],
                "brand_voice": snapshot["brand_voice"],
                "tono": expected_behavior["tono"],
                "debounce_mode": expected_behavior["debounce_mode"],
                "guardrail_max_retries": expected_behavior["guardrail_max_retries"],
                "disabled_guardrails": expected_behavior["disabled_guardrails"],
                "difficulty_long_message_threshold": expected_behavior[
                    "difficulty_long_message_threshold"
                ],
                "difficulty_turnos_sin_resolver_threshold": expected_behavior[
                    "difficulty_turnos_sin_resolver_threshold"
                ],
                "difficulty_score_threshold": expected_behavior["difficulty_score_threshold"],
                "escalation_keywords": "gerente, supervisor",
            },
        )
        assert r.status_code == 303
        assert "Configuraci" in r.headers["location"]

        after = await _snapshot(t2)
        assert after["escalation_config"] == {"keywords": ["gerente", "supervisor"]}
        assert after["behavior_config"] == expected_behavior, "no debe resetear el resto de la config"
    finally:
        await _restore(t2, snapshot)
