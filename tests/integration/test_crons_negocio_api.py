"""Crons por negocio (ADR-0201) contra Postgres real.

`/api/v1/admin/automatizaciones`: lista los crons de ámbito `negocio`, enciende/apaga, guarda
hora y ventana, y rechaza lo que el catálogo no admite. La política se prueba también por el
servicio (`debe_correr`), y RLS impide que un negocio lea la configuración de otro.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

from core.admin import crons_negocio_service as svc
from core.shared.tenant import tenant_scope

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/admin/automatizaciones"
T1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
A = uuid.UUID("10000000-0000-0000-0000-000000000001")
B = uuid.UUID("10000000-0000-0000-0000-000000000002")
CRON = "core.memory_consolidation"  # core, siempre visible para el negocio
CRON_VENTANA = "core.cliente_esperando"  # core, admite ventana


async def _sql(sql: str, **params) -> None:
    from core.db.session import get_session

    async with get_session() as session:
        await session.execute(text(sql), params)


async def _limpiar() -> None:
    # La tabla tiene RLS y `app_runtime` no tiene DELETE: se apaga con borrado lógico.
    for tid in (A, B, T1):
        async with tenant_scope(tid):
            await _sql("update tenant_cron_configs set deleted_at = now() where tenant_id = :t", t=tid)
    # El historial se purga con la función SECURITY DEFINER (también sin DELETE para la app).
    await svc.mantenimiento(dias=0)


@pytest.fixture(autouse=True)
async def _limpio():
    await _limpiar()
    yield
    await _limpiar()


@pytest.fixture
async def h(app_client):
    return await api_headers(app_client, T1)


async def test_lista_los_crons_de_negocio_y_no_los_de_plataforma(app_client, h):
    r = await app_client.get(BASE, headers=h)
    assert r.status_code == 200, r.text
    items = {i["name"]: i for i in r.json()["items"]}
    assert CRON in items
    # Plataforma: salud de canales, cobros de la plataforma, retenciones legales.
    assert "core.billing_renovacion" not in items
    assert "core.worker_heartbeat" not in items
    # La tarjeta trae lo que la pantalla necesita.
    assert items[CRON]["habilitada"] is True
    assert items[CRON]["acciones"] == ["enable", "disable", "hora"]


async def test_apagar_y_volver_a_encender(app_client, h):
    r = await app_client.put(f"{BASE}/{CRON}", headers=h, json={"habilitada": False})
    assert r.status_code == 200, r.text
    assert r.json()["habilitada"] is False

    async with tenant_scope(T1):
        politica = await svc.politica(T1, CRON)
    assert politica.debe_correr is False
    assert politica.motivo == svc.OMITIDA_DESHABILITADA

    r = await app_client.put(f"{BASE}/{CRON}", headers=h, json={"habilitada": True})
    assert r.status_code == 200
    assert r.json()["habilitada"] is True


async def test_hora_local_solo_donde_el_catalogo_la_admite(app_client, h):
    r = await app_client.put(f"{BASE}/{CRON_VENTANA}", headers=h, json={"hora_local": 9})
    assert r.status_code == 400
    assert r.json()["code"] == "not_configurable"


async def test_ventana_se_guarda_y_se_respeta(app_client, h):
    r = await app_client.put(
        f"{BASE}/{CRON_VENTANA}",
        headers=h,
        json={"ventana": {"desde": "08:00", "hasta": "18:00", "dias": [0, 1, 2, 3, 4]}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ventana"] == {"desde": "08:00", "hasta": "18:00", "dias": [0, 1, 2, 3, 4]}

    async with tenant_scope(T1):
        # 2026-11-16 15:00 UTC = 10:00 en Bogotá (lunes): dentro.
        dentro = await svc.politica(T1, CRON_VENTANA, ahora=datetime(2026, 11, 16, 15, 0, tzinfo=UTC))
        # 2026-11-16 03:00 UTC = 22:00 del domingo en Bogotá: fuera.
        fuera = await svc.politica(T1, CRON_VENTANA, ahora=datetime(2026, 11, 16, 3, 0, tzinfo=UTC))
    assert dentro.debe_correr is True
    assert fuera.motivo == svc.OMITIDA_FUERA_DE_VENTANA


async def test_una_ventana_invalida_es_400(app_client, h):
    r = await app_client.put(
        f"{BASE}/{CRON_VENTANA}",
        headers=h,
        json={"ventana": {"desde": "25:00", "hasta": "18:00"}},
    )
    assert r.status_code == 400


async def test_rls_aisla_la_configuracion_entre_negocios():
    async with tenant_scope(A):
        await svc.fijar(A, CRON, habilitada=False)
    async with tenant_scope(B):
        assert await svc.cargar(B, CRON) is None
    async with tenant_scope(A):
        config = await svc.cargar(A, CRON)
    assert config is not None and config.habilitada is False


async def test_un_cron_de_plataforma_no_se_puede_configurar(app_client, h):
    r = await app_client.put(f"{BASE}/core.billing_renovacion", headers=h, json={"habilitada": False})
    assert r.status_code == 404


async def test_el_historial_solo_guarda_lo_que_tuvo_efecto(app_client, h):
    async with tenant_scope(T1):
        # Sin efecto: no deja fila.
        await svc.registrar_ejecucion(T1, CRON, status="success", acciones=0)
        # Con efecto: sí.
        await svc.registrar_ejecucion(T1, CRON, status="success", acciones=3, resumen="3 hechos")
    r = await app_client.get(f"{BASE}/{CRON}/runs", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["acciones"] == 3
    assert items[0]["resumen"] == "3 hechos"


async def test_el_historial_aisla_por_negocio(app_client, h):
    async with tenant_scope(A):
        await svc.registrar_ejecucion(A, CRON, status="success", acciones=1, resumen="de A")
    async with tenant_scope(B):
        await svc.registrar_ejecucion(B, CRON, status="success", acciones=2, resumen="de B")
    async with tenant_scope(A):
        filas, total = await svc.historial(A, CRON)
    assert total == 1
    assert filas[0].resumen == "de A"


async def test_la_retencion_borra_lo_viejo():
    viejo = uuid.uuid4()
    async with tenant_scope(A):
        await _sql(
            "insert into tenant_cron_runs (id, tenant_id, cron_name, status, trigger, acciones, started_at) "
            "values (:id, :t, :c, 'success', 'scheduled', 1, now() - interval '100 days')",
            id=viejo,
            t=A,
            c=CRON,
        )
        await svc.registrar_ejecucion(A, CRON, status="success", acciones=1, resumen="reciente")
    borradas = await svc.mantenimiento()
    assert borradas >= 1
    async with tenant_scope(A):
        filas, _total = await svc.historial(A, CRON)
    assert all(f.id != viejo for f in filas)


async def test_el_master_ve_la_configuracion_por_negocio(app_client, h):
    async with tenant_scope(T1):
        await svc.fijar(T1, CRON, habilitada=False)
        await svc.registrar_ejecucion(T1, CRON, status="failed", acciones=0, error_class="TimeoutError")
    r = await app_client.get(f"/api/v1/admin/platform/crons/{CRON}/negocios", headers=h)
    assert r.status_code == 200, r.text
    fila = next((f for f in r.json() if f["tenant_id"] == str(T1)), None)
    assert fila is not None
    assert fila["habilitada"] is False
    assert fila["fallos_24h"] == 1
