"""Tanda 3A — operación y gobierno (ADR-0051).

Rollback de AgentSpec, horarios/autoescalado, guardrails declarativos y evals
persistidos. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"


async def _tenant_id() -> uuid.UUID:
    from core.db.models import Admin
    from core.db.session import get_session
    from core.shared.tenant import admin_tenants

    async with get_session() as session:
        admin = (
            await session.execute(select(Admin).where(Admin.email == "admin@local"))
        ).scalar_one()
    tenants = await admin_tenants(admin.id)
    assert tenants
    return tenants[0][0]


async def _login(app_client) -> str:
    r = await app_client.post("/admin/login", data={"email": "admin@local", "password": "admin123"})
    assert r.status_code == 303
    return r.headers["set-cookie"].split(";")[0]


async def _switch(app_client, session: str, tenant_id: uuid.UUID) -> str:
    r = await app_client.post(
        "/admin/switch-tenant", headers={"Cookie": session}, data={"tenant_id": str(tenant_id)}
    )
    cookies = [c.split(";")[0] for c in r.headers.get_list("set-cookie") if "ac_tenant" in c]
    assert cookies, "switch-tenant no fijó ac_tenant"
    return f"{session}; {cookies[0]}"


# ---------------------------------------------------------------- 3A.1 rollback
async def test_rollback_activa_version_anterior(app_client) -> None:
    from core.admin.views import _save_spec_version  # reusado: crea versión nueva
    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    async with tenant_scope(tid):
        current = await load_active_spec()
        v1 = current.version
        changed = current.model_dump(mode="json")
        changed["model"]["temperature"] = round((changed["model"]["temperature"] or 0.3) + 0.1, 2)
        from agentcore_contracts import AgentSpec

        new_version = await _save_spec_version(AgentSpec.model_validate(changed))
        assert new_version is not None

    # Encontrar el record_id de la versión vieja vía el panel.
    r = await app_client.get("/admin/configuracion?tab=avanzado", headers=headers)
    assert r.status_code == 200

    from core.db.models import AgentSpecRecord
    from core.db.session import get_session

    async with tenant_scope(tid):
        async with get_session() as s:
            old = (
                await s.execute(
                    select(AgentSpecRecord).where(
                        AgentSpecRecord.tenant_id == tid, AgentSpecRecord.version == v1
                    )
                )
            ).scalar_one()
            old_id = str(old.id)

    r = await app_client.post(
        "/admin/configuracion/spec/activar", headers=headers, data={"record_id": old_id}
    )
    assert r.status_code == 303

    async with tenant_scope(tid):
        assert (await load_active_spec()).version == v1


# ---------------------------------------------------------------- 3A.2 horarios
def test_within_business_hours() -> None:
    from core.orchestrator.escalation import within_business_hours

    # Sin config → siempre abierto.
    assert within_business_hours(None) is True

    # Lunes 10:00 en Bogotá dentro de 08:00-18:00.
    bh = {"tz": "America/Bogota", "ranges": [{"day": 0, "from": "08:00", "to": "18:00"}]}
    monday_10 = datetime(2026, 9, 28, 15, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 Bogotá (UTC-5)
    assert within_business_hours(bh, monday_10) is True
    monday_20 = datetime(2026, 9, 28, 3, 0, tzinfo=ZoneInfo("UTC"))  # 22:00 Bogotá domingo -> no
    assert within_business_hours(bh, monday_20) is False

    # Padding: "8:00" se normaliza (hallazgo de auditoría).
    bh_nopad = {"tz": "America/Bogota", "ranges": [{"day": 0, "from": "8:00", "to": "18:00"}]}
    assert within_business_hours(bh_nopad, monday_10) is True

    # JSONB malformado → no rompe (fail-safe por elemento), no lanza nunca.
    assert within_business_hours({"ranges": "no-es-lista"}) is True  # ranges no-lista → abierto
    # Elementos basura se ignoran; sin franja válida que matchee → cerrado (pero sin excepción).
    assert within_business_hours({"tz": "X", "ranges": ["basura", {"day": 0}]}) is False
    assert within_business_hours({"tz": "America/Bogota", "ranges": [{"day": "x"}]}) is False


async def test_guardar_horario_y_autoescalado(app_client) -> None:
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    r = await app_client.post(
        "/admin/configuracion/asistente",
        headers=headers,
        data={
            "assistant_name": "Bot",
            "business_tz": "America/Bogota",
            "business_ranges": "0 08:00-18:00\n1 08:00-18:00",
            "out_of_hours_message": "Estamos cerrados, escribinos mañana.",
            "auto_escalate_after_turns": "3",
        },
    )
    assert r.status_code == 303

    async with tenant_scope(tid):
        async with get_session() as s:
            cfg = (
                await s.execute(
                    select(AppSettings.escalation_config).where(AppSettings.tenant_id == tid)
                )
            ).scalar_one()
    assert cfg["business_hours"]["tz"] == "America/Bogota"
    assert len(cfg["business_hours"]["ranges"]) == 2
    assert cfg["out_of_hours_message"].startswith("Estamos cerrados")
    assert cfg["auto_escalate_after_turns"] == 3


# ---------------------------------------------------------------- 3A.3 guardrails declarativos
def test_validate_custom_rules_descarta_basura() -> None:
    from core.orchestrator.guardrails import validate_custom_rules

    clean = validate_custom_rules(
        [
            {"kind": "forbidden_phrase", "phrases": ["garantizado"], "action": "retry"},
            {"kind": "nope", "phrases": ["x"]},
            {"kind": "max_length", "value": 200},
            {"kind": "required_tool", "phrases": ["ya lo hice"], "tool": "crear_pedido"},
            {"kind": "required_tool", "phrases": ["x"]},  # sin tool -> fuera
        ]
    )
    kinds = [r["kind"] for r in clean]
    assert kinds == ["forbidden_phrase", "max_length", "required_tool"]


def test_eval_custom_rules() -> None:
    from core.orchestrator.guardrails import _eval_custom_rules
    from core.orchestrator.safe_renderer import TOOL_NAME_KEY

    rules = [{"kind": "forbidden_phrase", "phrases": ["garantizado"], "action": "retry"}]
    assert _eval_custom_rules("Está garantizado", [], rules) is not None
    assert _eval_custom_rules("Todo bien", [], rules) is None

    req = [{"kind": "required_tool", "phrases": ["ya confirmé"], "tool": "crear_pedido"}]
    assert _eval_custom_rules("Ya confirmé tu pedido", [], req) is not None
    assert _eval_custom_rules("Ya confirmé tu pedido", [{TOOL_NAME_KEY: "crear_pedido"}], req) is None


async def test_guardrails_propios_persisten_en_spec(app_client) -> None:
    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    r = await app_client.post(
        "/admin/configuracion/habilidades",
        headers=headers,
        data={"guardrails_text": "forbidden_phrase | garantizado, 100% seguro | retry\nmax_length | 300"},
    )
    assert r.status_code == 303

    async with tenant_scope(tid):
        spec = await load_active_spec()
    rules = (spec.extensions or {}).get("guardrails") or []
    assert {r["kind"] for r in rules} == {"forbidden_phrase", "max_length"}


async def test_tools_declarativas_rechazan_dominios_internos(app_client) -> None:
    """Anti-SSRF (ADR-0052): `allowed_domains` solo acepta FQDN públicos."""
    import json
    from urllib.parse import unquote

    from core.agents.spec import load_active_spec
    from core.shared.tenant import tenant_scope

    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    def _tools(domains: list[str]) -> str:
        return json.dumps(
            [{"name": "consulta", "url": "https://api.negocio.com/x", "allowed_domains": domains}]
        )

    r = await app_client.post(
        "/admin/configuracion/habilidades",
        headers=headers,
        data={"declarative_tools_json": _tools(["api.negocio.com", "redis", "127.1"])},
    )
    assert r.status_code == 303
    location = unquote(r.headers["location"])
    assert "flash_type=danger" in location and "redis, 127.1" in location
    async with tenant_scope(tid):
        spec = await load_active_spec()
    assert not any(t["name"] == "consulta" for t in (spec.extensions or {}).get("tools") or [])

    r = await app_client.post(
        "/admin/configuracion/habilidades",
        headers=headers,
        data={"declarative_tools_json": _tools(["API.negocio.com."])},
    )
    assert r.status_code == 303 and "flash_type=success" in r.headers["location"]
    async with tenant_scope(tid):
        spec = await load_active_spec()
    [tool] = [t for t in (spec.extensions or {}).get("tools") or [] if t["name"] == "consulta"]
    assert tool["allowed_domains"] == ["api.negocio.com"]


# ---------------------------------------------------------------- 3A.4 evals
async def test_eval_runs_persisten_y_se_ven(app_client) -> None:
    from core.db.models import EvalResult, EvalRun
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _tenant_id()
    session = await _login(app_client)
    headers = {"Cookie": await _switch(app_client, session, tid)}

    async with tenant_scope(tid):
        async with get_session() as s:
            run = EvalRun(tenant_id=tid, spec_version="1.0.9", git_ref="test", passed=1, total=2)
            s.add(run)
            await s.flush()
            s.add(EvalResult(tenant_id=tid, run_id=run.id, scenario="s1", ok=True))
            s.add(EvalResult(tenant_id=tid, run_id=run.id, scenario="s2", ok=False, detail="falló"))

    r = await app_client.get("/admin/configuracion?tab=avanzado", headers=headers)
    assert r.status_code == 200
    assert "Calidad (evals)" in r.text
    assert "1/2" in r.text
