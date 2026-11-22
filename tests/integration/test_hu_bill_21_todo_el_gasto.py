"""HU-BILL-21 · todo el gasto en créditos, contra la base real (ADR-0208 §11-16).

- Sin cobro doble: el fee de voz e imagen sale de `messages` solo antes de `vigente_desde` de `v2`;
  después cobra la fila `stt`/`vision` del ledger. Filas `v1` y `v2` mezcladas en el mismo mes, y
  filas con `credits` NULL interpretadas con `v1`.
- Un ciclo cerrado antes de `v2` no cambia su total.
- Playground: gratis hasta 300 créditos por ciclo; lo que pasa suma al negocio. `eval` no cuenta.
- La fila `tts` entra al ledger (CHECK de la migración 0153).
- La ingesta de conocimiento sin saldo responde 409 `plan_limit_creditos` y no guarda nada.
- Aislamiento: el consumo y el playground de un negocio no se ven en otro.

Sin LLM ni red: las filas del ledger se insertan directo. El corte de `v2` se mueve a «hace una
hora» (`creditos.TARIFARIO_V2`) para poder sembrar antes y después.
"""

from __future__ import annotations

import dataclasses
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # consumo por negocio (ADR-0144, ADR-0208)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

V2 = {"key_mode": "platform", "tarifario_version": "v2", "trm_cop": Decimal("4000")}
V1 = {"key_mode": "platform", "tarifario_version": "v1", "trm_cop": Decimal("4000")}


@pytest.fixture
def corte(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """`vigente_desde` de `v2` hace una hora y TRM 4000."""
    from core.billing import creditos
    from core.config import get_settings

    momento = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(creditos, "TARIFARIO_V2", dataclasses.replace(creditos.TARIFARIO_V2, vigente_desde=momento))
    monkeypatch.setattr(get_settings(), "billing_trm_cop", 4000.0)
    return momento


async def _negocio(plan: str = "basic") -> uuid.UUID:
    from _habilidades import onboard_tenant

    slug = uniq("bill21")
    tid, _ = await onboard_tenant(name=f"Gasto {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    return tid


async def _conversacion(tid: uuid.UUID, channel: str = "whatsapp") -> uuid.UUID:
    from core.db.models import Conversation, Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            cliente = Customer(tenant_id=tid, channel=channel, external_id=f"b21:{uuid.uuid4().hex[:8]}")
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


def _ledger(tid: uuid.UUID, conv: uuid.UUID | None, cuando: datetime, **campos: Any) -> Any:
    from core.db.models import UsageLedger

    base: dict[str, Any] = {"model": "m", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0, "kind": "turn"}
    base.update(campos)
    return UsageLedger(tenant_id=tid, conversation_id=conv, created_at=cuando, **base)


def _mensaje(tid: uuid.UUID, conv: uuid.UUID, cuando: datetime, **campos: Any) -> Any:
    from core.db.models import Message

    return Message(tenant_id=tid, conversation_id=conv, created_at=cuando, **{"sender_type": "customer", **campos})


def _plantilla(tid: uuid.UUID, conv: uuid.UUID, cuando: datetime) -> Any:
    return _mensaje(tid, conv, cuando, direction="outbound", sender_type="agent", template_name="aviso", status="sent")


async def _medir(tid: uuid.UUID, desde: datetime, hasta: datetime | None = None, *, por_agente: bool = False) -> Any:
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await medir_creditos(session, tid, desde=desde, hasta=hasta, por_agente=por_agente)


async def test_sin_cobro_doble_con_filas_v1_y_v2_mezcladas(corte: datetime) -> None:
    tid = await _negocio()
    conv = await _conversacion(tid)
    antes, despues = corte - timedelta(minutes=10), corte + timedelta(minutes=10)
    await _filas(
        tid,
        # Antes de v2: la ingesta escribía en sombra y el fee salía de `messages`.
        _mensaje(tid, conv, antes, direction="inbound", media_type="audio/ogg"),
        _mensaje(tid, conv, antes, direction="inbound", media_type="image/jpeg"),
        _ledger(tid, conv, antes, kind="stt", cost_usd=Decimal("0.0002"), credits=Decimal("0.0"), **V1),
        _ledger(tid, conv, antes, kind="vision", cost_usd=Decimal("0.0003"), credits=Decimal("0.0"), **V1),
        # Fila previa a 0096 (credits NULL, sin versión): con v1, ⌈0,0069 × 4000 × 3⌉/10 + 0,2 = 8,5.
        _ledger(tid, conv, antes, cost_usd=Decimal("0.0069")),
        _plantilla(tid, conv, antes),
        # Desde v2: la fila del ledger cobra su costo real + fee y `messages` ya no suma.
        _mensaje(tid, conv, despues, direction="inbound", media_type="audio/ogg"),
        _mensaje(tid, conv, despues, direction="inbound", media_type="image/jpeg"),
        _ledger(tid, conv, despues, kind="stt", cost_usd=Decimal("0.0002"), credits=Decimal("0.8"), **V2),
        _ledger(tid, conv, despues, kind="vision", cost_usd=Decimal("0.0003"), credits=Decimal("1.4"), **V2),
        _ledger(tid, conv, despues, kind="tts", cost_usd=Decimal("0.009"), credits=Decimal("10.8"), **V2),
        _ledger(tid, conv, despues, kind="agent_run", cost_usd=Decimal("0.0042"), credits=Decimal("5.1"), **V2),
        _ledger(tid, None, despues, kind="embedding", cost_usd=Decimal("0.0001"), credits=Decimal("0.2"), **V2),
        _ledger(tid, conv, despues, cost_usd=Decimal("0.0069"), credits=Decimal("8.5"), **V2),
        _ledger(tid, None, despues, kind="eval", cost_usd=Decimal("1"), credits=Decimal("999"), **V2),
        _plantilla(tid, conv, despues),
    )

    consumo = await _medir(tid, corte - timedelta(hours=1), por_agente=True)
    assert consumo.por_concepto == {
        "llm_turn": Decimal("8.5") + Decimal("5.1") + Decimal("0.2") + Decimal("8.5"),
        "stt": Decimal("0.5") + Decimal("0.8"),  # fee de messages antes + fila después
        "vision": Decimal("1.0") + Decimal("1.4"),
        "tts": Decimal("10.8"),
        "template_sent": Decimal("0.6"),
        "playground": Decimal("0"),
    }
    assert consumo.total == Decimal("37.4")
    assert sum((a.used for a in consumo.por_agente), Decimal(0)) == consumo.total


async def test_un_ciclo_cerrado_antes_de_v2_no_cambia(corte: datetime) -> None:
    tid = await _negocio()
    conv = await _conversacion(tid)
    playground = await _conversacion(tid, channel="test")
    antes = corte - timedelta(minutes=30)
    await _filas(
        tid,
        _mensaje(tid, conv, antes, direction="inbound", media_type="audio/ogg"),
        _mensaje(tid, conv, antes, direction="inbound", media_type="image/jpeg"),
        _ledger(tid, conv, antes, kind="stt", cost_usd=Decimal("0.0002"), credits=Decimal("0.0"), **V1),
        _ledger(tid, conv, antes, cost_usd=Decimal("0.0069")),
        _plantilla(tid, conv, antes),
        # El playground antes de v2 no consumía, aunque pase de 300.
        _ledger(tid, playground, antes, cost_usd=Decimal("1"), credits=Decimal("1200.2"), **V1),
    )
    consumo = await _medir(tid, corte - timedelta(hours=1), corte)
    # Lo mismo que medía v1: 0,5 + 1,0 + 8,5 + 0,3.
    assert consumo.total == Decimal("10.3")
    assert consumo.playground_usado == 0 and consumo.por_concepto["playground"] == 0


async def test_playground_gratis_hasta_300_y_eval_fuera(corte: datetime) -> None:
    tid = await _negocio()
    playground = await _conversacion(tid, channel="test")
    despues = corte + timedelta(minutes=5)
    await _filas(
        tid,
        _ledger(tid, playground, despues, credits=Decimal("250.0"), **V2),
        _ledger(tid, playground, despues, kind="eval", credits=Decimal("1000.0"), **V2),
    )
    consumo = await _medir(tid, corte - timedelta(hours=1), por_agente=True)
    assert (consumo.total, consumo.playground_usado, consumo.playground_gratis_restante) == (0, 250, 50)

    await _filas(tid, _ledger(tid, playground, despues, credits=Decimal("100.0"), **V2), _plantilla(tid, playground, despues))
    consumo = await _medir(tid, corte - timedelta(hours=1), por_agente=True)
    assert consumo.playground_usado == Decimal("350.3")
    assert consumo.por_concepto["playground"] == Decimal("50.3") == consumo.total
    assert consumo.playground_gratis_restante == 0
    assert [(a.agent_id, a.used) for a in consumo.por_agente] == [(None, Decimal("50.3"))]


async def test_el_playground_se_ve_en_admin_plan(app_client, corte: datetime) -> None:
    tid = await _negocio()
    playground = await _conversacion(tid, channel="test")
    await _filas(tid, _ledger(tid, playground, datetime.now(UTC), credits=Decimal("320.0"), **V2))
    r = await app_client.get("/api/v1/admin/plan", headers=await api_headers(app_client, tid))
    assert r.status_code == 200, r.text
    credito = r.json()["credit"]
    assert credito["playground"] == {"used": 320.0, "free_limit": 300.0, "free_remaining": 0.0, "charged": 20.0}
    assert credito["by_concept"]["playground"] == 20.0


async def test_aislamiento_del_consumo_y_el_playground(corte: datetime) -> None:
    from core.media import consumo

    uno, otro = await _negocio(), await _negocio()
    p_uno, p_otro = await _conversacion(uno, channel="test"), await _conversacion(otro, channel="test")
    conv_uno = await _conversacion(uno)
    despues = corte + timedelta(minutes=5)
    await _filas(
        uno,
        _ledger(uno, p_uno, despues, credits=Decimal("400.0"), **V2),
        _ledger(uno, conv_uno, despues, kind="tts", credits=Decimal("3.0"), **V2),
    )
    await _filas(otro, _ledger(otro, p_otro, despues, credits=Decimal("700.0"), **V2))
    # La fila de media va al negocio del scope, nunca a otro.
    from core.shared.tenant import tenant_scope as _scope

    async with _scope(otro):
        await consumo.registrar_consumo(kind="tts", concepto="tts", conversation_id=None, model="tts-1",
                                        cost_usd=0.009, key_mode="platform")
    consumo_otro = await _medir(otro, corte - timedelta(hours=1))
    assert (consumo_otro.playground_usado, consumo_otro.por_concepto["tts"]) == (700, Decimal("10.8"))
    assert consumo_otro.total == Decimal("400.0") + Decimal("10.8")
    # Con el scope de otro negocio, medir el primero se rechaza (la RLS daría 0 en silencio).
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(otro):
        async with get_session() as session:
            with pytest.raises(RuntimeError):
                await medir_creditos(session, uno, desde=corte)
    consumo_uno = await _medir(uno, corte - timedelta(hours=1))
    assert consumo_uno.total == Decimal("103.0") and consumo_uno.playground_usado == 400


async def test_la_fila_tts_entra_al_ledger() -> None:
    from core.media import consumo
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    conv = await _conversacion(tid)
    async with tenant_scope(tid):
        await consumo.registrar_consumo(
            kind="tts", concepto="tts", conversation_id=conv, model="tts-1", cost_usd=0.009,
            key_mode="platform", resultado={"caracteres": 600},
        )
        from core.db.session import get_session

        async with get_session() as session:
            fila = (
                await session.execute(
                    text("select kind, credits, tarifario_version from usage_ledger where tenant_id = :t and kind = 'tts'"),
                    {"t": tid},
                )
            ).one()
    assert tuple(fila) == ("tts", Decimal("10.8"), "v2")


async def test_ingesta_de_conocimiento_sin_saldo_da_409(app_client, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.billing.plans import get_plan
    from core.config import get_settings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "billing_bloquear_por_creditos", True)
    try:
        tid = await _negocio(plan="free")
        agente = uuid.uuid4()
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("insert into agents (id, tenant_id, key, kind, name) values (:id, :t, 'ventas', 'specialist', 'V')"),
                    {"id": agente, "t": tid},
                )
        limite = get_plan("free").monthly_credits
        assert limite
        await _filas(tid, _ledger(tid, None, datetime.now(UTC), credits=Decimal(limite), **V2))
        headers = await api_headers(app_client, tid)
        r = await app_client.post(
            f"/api/v1/pack/pack_knowledge/agents/{agente}/documents",
            headers=headers,
            files={"archivo": ("faq.md", b"# Envios\nA todo el pais.\n", "text/markdown")},
        )
        assert r.status_code == 409, r.text
        assert r.json()["error"]["code"] == "plan_limit_creditos"
        async with tenant_scope(tid):
            async with get_session() as session:
                docs = (
                    await session.execute(text("select count(*) from pack_knowledge_documents where tenant_id = :t"), {"t": tid})
                ).scalar_one()
        assert docs == 0
    finally:
        get_settings.cache_clear()
