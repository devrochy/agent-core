"""HU-BILL-16 · verificar el crédito antes de una acción de IA cotizada (ADR-0194, ADR-0144).

- `alcanza` deja pasar con saldo y levanta `QuotaExceeded("creditos")` (409 `plan_limit_creditos`)
  cuando el gasto no cabe en el crédito del mes; con el interruptor apagado solo mide.
- `imagen_ya_cobrada` reconoce una foto ya registrada por `result->>'image_key'`, solo del negocio.
- `medir_creditos` desglosa por `kind`: las filas `vision` del ledger son «Imágenes», no «IA», y
  la suma por agente sigue cuadrando con el total.

Sin LLM ni red: las filas del ledger se insertan directo.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

pytestmark = [
    pytest.mark.aislamiento,  # crédito por negocio (ADR-0144)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

LIMITE_FREE = 2_500


async def _negocio(plan: str = "free") -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = f"cotiza-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"Cotiza {slug}", slug=slug, sembrar_ejemplos=False, plan=plan)
    return tid


async def _filas(tid: uuid.UUID, *filas: Any) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            session.add_all(filas)


def _fila(tid: uuid.UUID, *, credits: str, kind: str = "turn", **campos: Any) -> Any:
    from core.db.models import UsageLedger

    base: dict[str, Any] = {
        "model": "claude-haiku-4-5",
        "input_tokens": 1500,
        "output_tokens": 300,
        "cost_usd": Decimal("0.003"),
        "key_mode": "platform",
        "tarifario_version": "v1",
        "trm_cop": Decimal("4000"),
    }
    base.update(campos)
    return UsageLedger(tenant_id=tid, kind=kind, credits=Decimal(credits), **base)


async def _alcanza(tid: uuid.UUID, creditos: int) -> Any:
    from core.billing.creditos import alcanza
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await alcanza(session, tid, creditos)


async def _ya_cobrada(tid: uuid.UUID, image_key: str) -> bool:
    from core.billing.creditos import imagen_ya_cobrada
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await imagen_ya_cobrada(session, tid, image_key)


async def test_bloquea_cuando_el_gasto_no_cabe_en_el_credito_del_mes() -> None:
    from core.api.plan_limits import plan_limit_error
    from core.billing.enforcement import QuotaExceeded

    tid = await _negocio()
    await _filas(tid, _fila(tid, credits="2495.0"))

    saldo = await _alcanza(tid, 5)  # 2495 + 5 = 2500: justo cabe
    assert saldo.limite == LIMITE_FREE
    assert saldo.usado == Decimal("2495.0")
    assert saldo.disponible == Decimal("5.0")

    with pytest.raises(QuotaExceeded) as exc:
        await _alcanza(tid, 6)
    assert (exc.value.metric, exc.value.used, exc.value.limit) == ("creditos", 2495, LIMITE_FREE)
    error = plan_limit_error(exc.value)
    assert (error.status_code, error.code) == (409, "plan_limit_creditos")
    assert error.details == {"metric": "creditos", "used": 2495, "limit": LIMITE_FREE}


async def test_con_el_interruptor_apagado_no_bloquea(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    tid = await _negocio()
    await _filas(tid, _fila(tid, credits="2500.0"))
    monkeypatch.setattr(get_settings(), "billing_bloquear_por_creditos", False)

    saldo = await _alcanza(tid, 50)
    assert saldo.usado == Decimal("2500.0")
    assert saldo.disponible == Decimal(0)


async def test_lo_de_otro_negocio_no_cuenta_ni_bloquea() -> None:
    otro = await _negocio()
    await _filas(otro, _fila(otro, credits="2500.0"))
    tid = await _negocio()

    saldo = await _alcanza(tid, 10)
    assert saldo.usado == Decimal(0)


async def test_una_foto_ya_registrada_no_se_cobra_dos_veces() -> None:
    tid = await _negocio()
    otro = await _negocio()
    await _filas(
        tid,
        _fila(tid, credits="4.6", kind="vision", result={"image_key": "t/foto-1.jpg", "proposito": "catalogo"}),
        # La ingesta de WhatsApp guarda `media_key` (en sombra): no se confunde con una foto de catálogo.
        _fila(tid, credits="0.0", kind="vision", result={"media_key": "t/foto-2.jpg"}),
    )
    await _filas(otro, _fila(otro, credits="4.6", kind="vision", result={"image_key": "t/foto-3.jpg"}))

    assert await _ya_cobrada(tid, "t/foto-1.jpg") is True
    assert await _ya_cobrada(tid, "t/foto-2.jpg") is False
    assert await _ya_cobrada(tid, "t/foto-3.jpg") is False  # de otro negocio


async def test_la_vision_del_ledger_se_desglosa_aparte_de_la_ia() -> None:
    from core.billing.creditos import medir_creditos
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    await _filas(
        tid,
        _fila(tid, credits="3.0"),  # turno
        _fila(tid, credits="2.0", kind="generacion"),  # datos de prueba: sigue en «IA»
        _fila(tid, credits="4.6", kind="vision", result={"image_key": "t/a.jpg"}),
        _fila(tid, credits="4.6", kind="vision", result={"image_key": "t/b.jpg"}),
        _fila(tid, credits="0.0", kind="vision", result={"media_key": "t/c.jpg"}),  # sombra de WhatsApp
    )
    async with tenant_scope(tid):
        async with get_session() as session:
            consumo = await medir_creditos(
                session, tid, desde=datetime.now(UTC) - timedelta(hours=1), por_agente=True
            )

    assert consumo.por_concepto == {
        "llm_turn": Decimal("5.0"),
        "stt": Decimal(0),
        "vision": Decimal("9.2"),
        "template_sent": Decimal(0),
    }
    assert consumo.total == Decimal("14.2")
    # Las claves no cambian (el panel las nombra) y la suma por agente sigue cuadrando.
    assert sum((a.used for a in consumo.por_agente), Decimal(0)) == consumo.total


async def test_fuera_del_scope_del_negocio_no_mide_en_silencio() -> None:
    from core.billing.creditos import alcanza, imagen_ya_cobrada
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    otro = await _negocio()
    async with tenant_scope(otro):
        async with get_session() as session:
            with pytest.raises(RuntimeError):
                await alcanza(session, tid, 1)
            with pytest.raises(RuntimeError):
                await imagen_ya_cobrada(session, tid, "t/x.jpg")
