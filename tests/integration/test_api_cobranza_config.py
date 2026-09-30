"""Config de cartera en la API (`/api/v1/pack/cobranza/portfolios/{id}/config`).

Regresión: la API solo exponía 25 de los 32 campos del clásico -- los 7 restantes
(avisos, días no laborables, periodo de liquidación, ruta) eran invisibles para el
panel nuevo. Este test fija el roundtrip completo con su coerción de tipos.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

# Los 7 que faltaban, con valores no-default para probar la persistencia real.
CAMPOS_NUEVOS = {
    "permitir_cambio_fecha_prestamo": True,
    "dias_no_laborables": "domingos_y_feriados",
    "periodo_liquidacion": "semanal",
    "dias_anticipacion_notificacion": 3,
    "aviso_dia_cobro": True,
    "umbral_mora_notificacion": 5,
    "ruta_habilitada": True,
}

TOTAL_CAMPOS = 32


async def _tenant_con_cartera() -> tuple[uuid.UUID, uuid.UUID]:
    from pack_cobranza.models import Cartera

    from core.db.session import get_session
    from core.onboarding import onboard_tenant
    from core.shared.tenant import tenant_scope

    slug = uniq("cfg-cartera")
    tid, _ = await onboard_tenant(
        name=f"Tenant config cartera {slug}",
        slug=slug,
        business_description="Test de configuración de cartera.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            cartera = Cartera(tenant_id=tid, nombre=f"Cartera {slug}")
            session.add(cartera)
            await session.flush()
            cartera_id = cartera.id
    return tid, cartera_id


async def test_roundtrip_incluye_los_campos_del_clasico(app_client) -> None:
    tid, cartera_id = await _tenant_con_cartera()
    headers = await api_headers(app_client, tid)

    r = await app_client.put(
        f"/api/v1/pack/cobranza/portfolios/{cartera_id}/config",
        headers=headers,
        json={"values": CAMPOS_NUEVOS},
    )
    assert r.status_code == 200, r.text

    r2 = await app_client.get(
        f"/api/v1/pack/cobranza/portfolios/{cartera_id}/config", headers=headers
    )
    assert r2.status_code == 200, r2.text
    values = r2.json()["values"]

    assert len(values) == TOTAL_CAMPOS, f"la API debe exponer los {TOTAL_CAMPOS} campos"
    for nombre, esperado in CAMPOS_NUEVOS.items():
        assert values[nombre] == esperado, f"{nombre}: {values[nombre]!r} != {esperado!r}"


async def test_coercion_de_tipos_como_el_clasico(app_client) -> None:
    tid, cartera_id = await _tenant_con_cartera()
    headers = await api_headers(app_client, tid)

    r = await app_client.put(
        f"/api/v1/pack/cobranza/portfolios/{cartera_id}/config",
        headers=headers,
        json={
            "values": {
                "cuotas_minimas_prestamo": "4",  # int desde string
                "cupo_default": "150000.50",  # num desde string
                "aviso_dia_cobro": 1,  # bool desde int
                "dias_no_laborables": "solo_domingos",  # text
            }
        },
    )
    assert r.status_code == 200, r.text

    values = (
        await app_client.get(
            f"/api/v1/pack/cobranza/portfolios/{cartera_id}/config", headers=headers
        )
    ).json()["values"]
    assert values["cuotas_minimas_prestamo"] == 4
    assert values["cupo_default"] == 150000.50
    assert values["aviso_dia_cobro"] is True
    assert values["dias_no_laborables"] == "solo_domingos"
    # Lo no enviado queda en el default del modelo (el clásico tampoco lo pisaba).
    assert values["periodo_liquidacion"] == "diario"


async def test_detalle_expone_los_kpis_del_clasico(app_client) -> None:
    """El detalle de cartera trae mora, cobrado de 30 días, gastos y reportes diarios."""
    tid, cartera_id = await _tenant_con_cartera()
    headers = await api_headers(app_client, tid)

    r = await app_client.get(f"/api/v1/pack/cobranza/portfolios/{cartera_id}", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    for clave in ("mora", "cobrado_30d", "gastos_total", "reportes_diarios"):
        assert clave in body, f"falta {clave} en el detalle"
    assert body["mora"] == 0
    assert body["cobrado_30d"] == 0
    assert body["gastos_total"] == 0
    assert body["reportes_diarios"] == []
