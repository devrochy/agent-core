"""Auditoría móvil (`POST /api/v1/audit/mobile-events`, ADR-0097).

Cubre: ack por evento (sincronizado/duplicado/error), idempotencia por
(tenant, eventId), aislamiento RLS y validación de payload. Requiere el stack
local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import uniq
from sqlalchemy import select

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"


async def _tenant(email: str, slug: str) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de auditoría móvil.",
        admin_email=email,
        admin_password=PW,
        packs=[],
    )
    return tid


async def _login(app_client, email: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def test_eventos_idempotentes_con_ack(app_client) -> None:
    from core.db.models import MobileAuditEvent
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("audit")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)
    token = await _login(app_client, email)
    h = _headers(token, tid)

    ev1 = uuid.uuid4()
    ev2 = uuid.uuid4()
    eventos = [
        {"eventId": str(ev1), "accion": "eliminar_cuota", "entidad": "cuota",
         "payload": {"cuotaId": "x"}, "deviceId": "dev-1"},
        {"eventId": str(ev2), "accion": "liquidacion", "entidad": "cartera",
         "payload": {"carteraId": "y"}},
    ]
    r = await app_client.post("/api/v1/audit/mobile-events", json={"events": eventos}, headers=h)
    assert r.status_code == 200, r.text
    estados = {a["eventId"]: a["estado"] for a in r.json()["eventos"]}
    assert estados[str(ev1)] == "sincronizado"
    assert estados[str(ev2)] == "sincronizado"

    # Reenvío (la app reintenta): ev1 duplicado, ev3 nuevo.
    ev3 = uuid.uuid4()
    r = await app_client.post(
        "/api/v1/audit/mobile-events",
        json={"events": [
            {"eventId": str(ev1), "accion": "eliminar_cuota", "entidad": "cuota"},
            {"eventId": str(ev3), "accion": "cambio_plan", "entidad": "plan"},
        ]},
        headers=h,
    )
    estados = {a["eventId"]: a["estado"] for a in r.json()["eventos"]}
    assert estados[str(ev1)] == "duplicado"
    assert estados[str(ev3)] == "sincronizado"

    async with tenant_scope(tid):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(MobileAuditEvent).where(
                        MobileAuditEvent.tenant_id == tid,
                        MobileAuditEvent.deleted_at.is_(None),
                    )
                )
            ).scalars().all()
    assert len(filas) == 3


async def test_aislamiento_y_validacion(app_client) -> None:
    slug_a, slug_b = uniq("audit-a"), uniq("audit-b")
    tid_a = await _tenant(f"{slug_a}@test.local", slug_a)
    tid_b = await _tenant(f"{slug_b}@test.local", slug_b)
    token_a = await _login(app_client, f"{slug_a}@test.local")
    token_b = await _login(app_client, f"{slug_b}@test.local")

    ev = uuid.uuid4()
    await app_client.post(
        "/api/v1/audit/mobile-events",
        json={"events": [{"eventId": str(ev), "accion": "pago", "entidad": "cuota"}]},
        headers=_headers(token_a, tid_a),
    )

    # Mismo eventId en otro negocio NO es duplicado: es un evento nuevo ahí.
    r = await app_client.post(
        "/api/v1/audit/mobile-events",
        json={"events": [{"eventId": str(ev), "accion": "pago", "entidad": "cuota"}]},
        headers=_headers(token_b, tid_b),
    )
    assert r.json()["eventos"][0]["estado"] == "sincronizado"

    # Payload inválido → 422.
    r = await app_client.post(
        "/api/v1/audit/mobile-events",
        json={"events": [{"eventId": str(ev), "accion": "x", "entidad": "cuota"}]},
        headers=_headers(token_a, tid_a),
    )
    assert r.status_code == 422


async def test_sin_sesion_es_401(app_client) -> None:
    r = await app_client.post(
        "/api/v1/audit/mobile-events",
        json={"events": [{"eventId": str(uuid.uuid4()), "accion": "pago", "entidad": "cuota"}]},
    )
    assert r.status_code == 401
