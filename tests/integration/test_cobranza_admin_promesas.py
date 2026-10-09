"""GET /api/v1/pack/cobranza/promises con clientes (regresión del nombre).

Antes leía `Cliente.nombres`/`apellidos` (columnas inexistentes) y daba 500.
Requiere el stack local (Postgres + Redis).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

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
        business_description="Negocio de prueba de promesas admin.",
        admin_email=email,
        admin_password=PW,
        packs=["pack_cobranza"],
    )
    return tid


async def _login(app_client, email: str) -> str:
    r = await app_client.post("/api/v1/auth/login", json={"identifier": email, "password": PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _headers(token: str, tid: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Id": str(tid)}


async def test_promesas_admin_lista_con_nombre_de_cliente(app_client) -> None:
    from pack_cobranza.models import Cartera, Cliente, Prestamo, Promesa

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = f"prom-admin-{uuid.uuid4().hex[:6]}"
    email = f"{slug}@test.local"
    tid = await _tenant(email, slug)

    async with tenant_scope(tid):
        async with get_session() as s:
            cartera = Cartera(tenant_id=tid, nombre="Cartera A")
            s.add(cartera)
            await s.flush()
            cliente = Cliente(tenant_id=tid, cartera_id=cartera.id, nombre="Juana", apellido="Perez")
            s.add(cliente)
            await s.flush()
            prestamo = Prestamo(
                tenant_id=tid, cliente_id=cliente.id, cartera_id=cartera.id,
                valor=Decimal("100"), saldo=Decimal("50"), num_cuotas=2,
                tipo_interes=Decimal("10"), dias_entre_cuotas=1, fecha_otorgado=date.today(),
            )
            s.add(prestamo)
            await s.flush()
            s.add(
                Promesa(
                    tenant_id=tid, prestamo_id=prestamo.id, cliente_id=cliente.id,
                    fecha_prometida=date.today() + timedelta(days=1),
                    valor_prometido=Decimal("50"), estado="pendiente",
                )
            )
            await s.commit()

    token = await _login(app_client, email)
    r = await app_client.get(
        "/api/v1/pack/cobranza/promises", headers=_headers(token, tid)
    )
    assert r.status_code == 200, r.text
    promesas = r.json()
    assert len(promesas) == 1
    assert promesas[0]["cliente"] == "Juana Perez"
