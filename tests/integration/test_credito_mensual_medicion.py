"""HU-BILL-04/06 · medición del crédito mensual contra la base real (ADR-0144).

- El crédito usado coincide con `usage_ledger` y la tasa: créditos congelados al escribir, filas
  viejas calculadas con la TRM vigente, fees de notas de voz, imágenes y plantillas enviadas; sin
  playground ni filas `eval`.
- El mes de consumo se reinicia con el ciclo de la suscripción.
- Aislamiento entre negocios (filtro y RLS) y `GET /admin/plan` con el uso real.
- La migración 0096 sube y baja.

Sin LLM ni red: las filas del ledger se insertan directo.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # consumo por negocio (ADR-0144)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]


async def _negocio(plan: str = "basic") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = f"credito-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Crédito {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    return tid


async def _conversacion(tid: uuid.UUID, channel: str = "whatsapp") -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel=channel, external_id=f"cred:{uuid.uuid4().hex[:8]}")
            session.add(cliente)
            await session.flush()
            conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel=channel)
            session.add(conv)
            await session.flush()
            return conv.id


async def _filas(tid: uuid.UUID, *filas: Any) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add_all(filas)


def _turno(tid: uuid.UUID, conv: uuid.UUID | None, cuando: datetime, **campos: Any) -> Any:
    from core.db.models import UsageLedger

    base: dict[str, Any] = {"model": "deepseek-flash", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0}
    base.update(campos)
    return UsageLedger(tenant_id=tid, conversation_id=conv, created_at=cuando, **base)


def _mensaje(tid: uuid.UUID, conv: uuid.UUID, cuando: datetime, **campos: Any) -> Any:
    from core.db.models import Message

    return Message(tenant_id=tid, conversation_id=conv, created_at=cuando, **{"sender_type": "customer", **campos})


async def _sembrar_mes(tid: uuid.UUID, cuando: datetime) -> None:
    """Un mes tipo. Esperado (TRM 4000): 8,5 + 1,6 + 0,5 + 1,0 + 0,3 = 11,9 créditos."""
    conv = await _conversacion(tid)
    playground = await _conversacion(tid, channel="test")
    await _filas(
        tid,
        # Fila de antes de 0096 (sin créditos): ⌈0,0069 × 4000 × 3⌉ / 10 + 0,2 = 8,5.
        _turno(tid, conv, cuando, cost_usd=Decimal("0.0069")),
        # Fila con créditos congelados al escribir (BYOK de una conversación cara): cuenta 1,6 tal cual.
        _turno(tid, conv, cuando, cost_usd=Decimal("0.5"), credits=Decimal("1.6"), key_mode="byok",
               tarifario_version="v1", trm_cop=Decimal("4000")),
        _turno(tid, None, cuando, cost_usd=Decimal("1"), kind="eval"),  # a cargo de la plataforma
        _turno(tid, playground, cuando, cost_usd=Decimal("1")),  # prueba del panel: no consume
        _turno(tid, conv, cuando, input_tokens=0, output_tokens=0),  # sin llamada al LLM: 0
        _mensaje(tid, conv, cuando, direction="inbound", media_type="audio/ogg"),
        _mensaje(tid, conv, cuando, direction="inbound", media_type="image/jpeg"),
        _mensaje(tid, conv, cuando, direction="outbound", sender_type="agent", template_name="recordatorio",
                 status="delivered"),
        _mensaje(tid, conv, cuando, direction="outbound", sender_type="agent", template_name="recordatorio",
                 status="failed"),  # no salió: no cobra
        _mensaje(tid, conv, cuando, direction="inbound", content="hola"),  # texto: va en el turno
        _mensaje(tid, playground, cuando, direction="inbound", media_type="audio/ogg"),  # playground
    )


async def _medir(tid: uuid.UUID, desde: datetime, hasta: datetime | None = None, *, por_agente: bool = False) -> Any:
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await medir_creditos(session, tid, desde=desde, hasta=hasta, por_agente=por_agente)


async def test_el_credito_usado_coincide_con_el_ledger_y_la_tasa(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.billing.metrics import count_metric
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4000.0)
    tid = await _negocio()
    ahora = datetime.now(UTC)
    desde = ahora - timedelta(hours=1)
    await _sembrar_mes(tid, ahora)

    consumo = await _medir(tid, desde, por_agente=True)
    assert consumo.por_concepto == {
        "llm_turn": Decimal("10.1"), "stt": Decimal("0.5"), "vision": Decimal("1.0"), "template_sent": Decimal("0.3"),
    }
    assert consumo.total == Decimal("11.9")
    # Sin agentes v3: todo cae en el renglón sin agente y la suma cuadra con el total.
    assert [(a.agent_id, a.used) for a in consumo.por_agente] == [(None, Decimal("11.9"))]
    # El registro de métricas (ADR-0045) mide lo mismo.
    assert await count_metric(tid, "creditos", period_start=desde) == pytest.approx(11.9)

    # Otra TRM: solo cambian las filas sin créditos congelados (8,5 → ⌈91,08⌉/10 + 0,2 = 9,4).
    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4400.0)
    assert (await _medir(tid, desde)).total == Decimal("12.8")


async def test_el_mes_de_consumo_se_reinicia_con_el_ciclo() -> None:
    from core.admin.plan_view import credito_del_mes
    from core.billing.enforcement import subscription_overview

    tid = await _negocio()
    ahora = datetime.now(UTC)
    inicio_ciclo = ahora - timedelta(days=45)  # renovación anticipada: period_start no se movió
    async with _como_dueno_de_tablas() as conn:
        actualizada = await conn.execute(
            text("update tenant_subscriptions set period_start = :ps, period_end = :pe, dia_ancla = :d, "
                 "status = 'active' where tenant_id = :t"),
            {"ps": inicio_ciclo, "pe": ahora + timedelta(days=40), "d": inicio_ciclo.day, "t": tid},
        )
        assert actualizada.rowcount == 1
    conv = await _conversacion(tid)
    await _filas(
        tid,
        _turno(tid, conv, ahora - timedelta(days=40), cost_usd=Decimal("0.0069")),  # mes anterior
        _turno(tid, conv, ahora - timedelta(minutes=5), cost_usd=Decimal("0.0069")),  # mes en curso
    )
    medido = await credito_del_mes(tid, await subscription_overview(tid))
    assert medido is not None
    assert medido["period_start"] <= ahora < medido["period_end"]
    assert medido["period_start"] > inicio_ciclo
    assert medido["used"] == pytest.approx(8.5)  # solo la del mes en curso

    # El mes siguiente empieza en cero.
    siguiente = await credito_del_mes(tid, await subscription_overview(tid), now=medido["period_end"])
    assert siguiente is not None and siguiente["used"] == 0


async def test_aislamiento_entre_negocios(app_client) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    uno, otro = await _negocio(), await _negocio()
    ahora = datetime.now(UTC)
    await _sembrar_mes(uno, ahora)
    desde = ahora - timedelta(hours=1)

    assert (await _medir(uno, desde)).total == Decimal("11.9")
    assert (await _medir(otro, desde)).total == Decimal(0)
    # Medir otro negocio desde un scope ajeno se rechaza; y la RLS no deja ver sus filas.
    from core.billing.creditos import medir_creditos

    async with tenant_scope(otro):
        async with get_session() as session:
            with pytest.raises(RuntimeError):
                await medir_creditos(session, uno, desde=desde)
            ajenas = await session.execute(
                text("select count(*) from usage_ledger where tenant_id = :t"), {"t": uno}
            )
            assert ajenas.scalar() == 0

    # GET /admin/plan: cada negocio ve solo su consumo.
    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, uno))
    assert r.status_code == 200, r.text
    credito = r.json()["credit"]
    assert (credito["limit"], credito["used"], credito["percent"]) == (20000, 11.9, 0.1)
    assert credito["by_concept"] == {"llm_turn": 10.1, "stt": 0.5, "vision": 1.0, "template_sent": 0.3}
    assert credito["by_agent"] == [{"agent_id": None, "name": None, "used": 11.9}]
    assert credito["period_start"] and credito["period_end"]
    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, otro))
    assert r.status_code == 200, r.text
    assert (r.json()["credit"]["used"], r.json()["credit"]["percent"]) == (0.0, 0.0)


class _como_dueno_de_tablas:
    """Conexión como dueño de las tablas (sin RLS) para preparar la suscripción."""

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        from sqlalchemy.ext.asyncio import create_async_engine

        from core.config import get_settings

        settings = get_settings()
        self._engine = create_async_engine(settings.migrations_database_url or settings.database_url)
        self._ctx = self._engine.begin()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        await self._ctx.__aexit__(*exc)
        await self._engine.dispose()


async def _columnas_de_credito() -> set[str]:
    async with _como_dueno_de_tablas() as conn:
        filas = (
            await conn.execute(
                text("select column_name from information_schema.columns where table_name = 'usage_ledger' "
                     "and column_name in ('credits','key_mode','tarifario_version','trm_cop')")
            )
        ).all()
    return {f[0] for f in filas}


def _alembic(*args: str) -> None:
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=RAIZ, capture_output=True, text=True, timeout=300, check=False
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]


async def test_la_migracion_0096_sube_y_baja() -> None:
    columnas = {"credits", "key_mode", "tarifario_version", "trm_cop"}
    assert await _columnas_de_credito() == columnas
    try:
        _alembic("downgrade", "0095")
        assert await _columnas_de_credito() == set()
    finally:
        _alembic("upgrade", "head")
    assert await _columnas_de_credito() == columnas
