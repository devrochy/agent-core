"""Búsqueda global (`GET /api/v1/search`, ADR-0096).

Cubre: agregación de providers del core y de los packs para el dueño, RBAC por
sección (colaborador solo ve lo concedido), aislamiento entre negocios (RLS) y
validación de parámetros. Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from _api_helpers import uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PW = "password123"
SEARCH = "/api/v1/search"


async def _tenant(email: str, slug: str, packs: list[str]) -> uuid.UUID:
    from core.onboarding import onboard_tenant

    tid, _ = await onboard_tenant(
        name=f"Tenant {slug}",
        slug=slug,
        business_description="Negocio de prueba de búsqueda.",
        admin_email=email,
        admin_password=PW,
        packs=packs,
    )
    return tid


async def _login(app_client, email: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str, tenant_id: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tenant_id)}


async def _sembrar_datos(tid: uuid.UUID) -> None:
    """Datos de todas las superficies que matchean "juan" en el negocio."""
    from pack_calendar.models import Appointment
    from pack_cobranza.models import Cliente, Promesa
    from pack_commerce.models import Product

    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            customer = Customer(
                tenant_id=tid, channel="whatsapp", external_id="whatsapp:+59171112233",
                name="Juanita Mercado",
            )
            session.add(customer)
            await session.flush()
            session.add(
                Conversation(
                    tenant_id=tid, customer_id=customer.id, channel="whatsapp", status="open"
                )
            )
            session.add(Product(tenant_id=tid, name="Chaqueta Juan de la Cruz"))
            session.add(
                Appointment(
                    tenant_id=tid,
                    title="Demo con Juan",
                    start_at=datetime.now(UTC) + timedelta(hours=2),
                    end_at=datetime.now(UTC) + timedelta(hours=3),
                    status="scheduled",
                )
            )
            cliente = Cliente(
                tenant_id=tid, cartera_id=uuid.uuid4(), nombre="Juana", apellido="Perez",
                numero_documento="5551234",
            )
            session.add(cliente)
            await session.flush()
            session.add(
                Promesa(
                    tenant_id=tid, prestamo_id=uuid.uuid4(), cliente_id=cliente.id,
                    fecha_prometida=date.today(), valor_prometido=Decimal("100.00"),
                    estado="pendiente",
                )
            )
            await session.commit()


async def test_dueno_ve_todas_las_secciones(app_client) -> None:
    slug = uniq("search")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=["pack_cobranza", "pack_commerce", "pack_calendar"])
    await _sembrar_datos(tid)
    token = await _login(app_client, email)

    r = await app_client.get(SEARCH, params={"q": "juan"}, headers=_headers(token, tid))
    assert r.status_code == 200, r.text
    secciones = {s["seccion"]: s for s in r.json()["resultados"]}
    assert set(secciones) >= {
        "conversations",
        "leads",
        "pack_cobranza:clientes",
        "pack_cobranza:promesas",
        "pack_commerce",
        "pack_calendar:citas",
    }
    assert any(i["tipo"] == "conversacion" for i in secciones["conversations"]["items"])
    assert any(i["tipo"] == "promesa" for i in secciones["pack_cobranza:promesas"]["items"])


async def test_colaborador_ve_las_secciones_en_lectura(app_client) -> None:
    """El RBAC actual da `read` a todo colaborador del negocio (ADR-0047): el
    endpoint exige `effective_access` (read) y no filtra secciones de lectura.
    Cuando la grilla de permisos por sección (HU-UI-10) restrinja lecturas, la
    búsqueda se filtrará sola porque consulta `effective_access` por sección."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    slug = uniq("search-col")
    email_owner = f"{slug}-owner@test.local"
    tid = await _tenant(email_owner, slug, packs=["pack_cobranza"])
    await _sembrar_datos(tid)

    email_col = f"{slug}-col@test.local"
    async with get_session() as session:
        col = Admin(email=email_col, password_hash=hash_password(PW), role="colaborador")
        session.add(col)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=col.id, role="colaborador"))
        await session.commit()

    token = await _login(app_client, email_col)
    r = await app_client.get(SEARCH, params={"q": "juan"}, headers=_headers(token, tid))
    assert r.status_code == 200, r.text
    secciones = {s["seccion"] for s in r.json()["resultados"]}
    assert set(secciones) >= {"conversations", "leads", "pack_cobranza:clientes"}


async def test_aislamiento_entre_negocios(app_client) -> None:
    slug_a, slug_b = uniq("search-a"), uniq("search-b")
    tid_a = await _tenant(f"{slug_a}@test.local", slug_a, packs=["pack_cobranza"])
    tid_b = await _tenant(f"{slug_b}@test.local", slug_b, packs=["pack_cobranza"])
    await _sembrar_datos(tid_a)
    token_b = await _login(app_client, f"{slug_b}@test.local")

    r = await app_client.get(SEARCH, params={"q": "juan"}, headers=_headers(token_b, tid_b))
    assert r.status_code == 200
    assert all(not s["items"] for s in r.json()["resultados"])


async def test_query_corta_es_422(app_client) -> None:
    slug = uniq("search-q")
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug, packs=[])
    token = await _login(app_client, email)

    r = await app_client.get(SEARCH, params={"q": "j"}, headers=_headers(token, tid))
    assert r.status_code == 422


async def test_sin_sesion_es_401(app_client) -> None:
    r = await app_client.get(SEARCH, params={"q": "juan"})
    assert r.status_code == 401
