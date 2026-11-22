"""HU-BILL-20 · el turno con el saldo agotado contra el stack real (ADR-0208 §2-7).

El saldo del ciclo se fija con un parche de `creditos._saldo_del_mes` (el cálculo real del saldo ya lo
cubre `test_creditos_cotizados.py`); todo lo demás es real: el episodio que abre la asignación de la
release, las filas de `usage_ledger` que escribe el turno, `core_decisions`, la escalación, Redis y el
centro de notificaciones.

- una conversación con un turno atendido en el episodio entra al colchón y se atiende;
- con el colchón agotado pasa a una persona con `creditos_agotados`, sin LLM, y queda escalada;
- una conversación nueva sin saldo recibe `quota_exhausted` una vez y luego silencio; sus turnos
  bloqueados (0 tokens) no la abren, y con saldo otra vez se atiende sin intervención;
- los avisos del ciclo salen una vez cada uno;
- RLS: otro negocio no ve la conversación ni el consumo ajenos.

El LLM es el `FakeProvider` guionado del conftest: nunca hay LLM real.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text
from test_kernel_turno import (
    _conversacion_de,
    _decisiones,
    _filas,
    _inbound,
    _inmediato,
    _resultado,
    _sin_llm,
)

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # el saldo y la conversación abierta son del negocio
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

LIMITE = 12_000  # Basic: colchón de 600


def _saldo_fijo(monkeypatch, estado: dict[str, Any]) -> None:
    """El saldo del ciclo sale de `estado["usado"]`; el inicio del ciclo es único por test."""
    from core.billing import creditos

    async def _saldo(_session, _tid, *, now=None, con_lock=False):  # type: ignore[no-untyped-def]
        return creditos.SaldoDeCreditos(
            usado=Decimal(estado["usado"]), limite=LIMITE, inicio=estado["ciclo"], fin=estado["ciclo"] + timedelta(days=30)
        )

    monkeypatch.setattr(creditos, "_saldo_del_mes", _saldo)


async def _turno(tid: uuid.UUID, cliente: str, contenido: str = "hola, ¿qué planes tienen?") -> None:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, contenido))


async def _abierta(tid: uuid.UUID, conv: uuid.UUID) -> tuple[bool, Any]:
    from core.billing.creditos import conversacion_abierta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return await conversacion_abierta(session, tid, conv)


async def test_conversacion_abierta_entra_al_colchon_y_luego_pasa_a_una_persona(tenant_setup, monkeypatch) -> None:
    t1, _t2 = tenant_setup
    _inmediato(monkeypatch)
    estado = {"usado": 0, "ciclo": datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)}
    _saldo_fijo(monkeypatch, estado)
    cliente = uniq("wa-colchon")

    # 1. Con saldo: se atiende y el turno deja su fila con tokens en el episodio.
    await _turno(t1, cliente)
    conv = await _conversacion_de(t1, cliente)
    assert (await _abierta(t1, conv))[0] is True

    # 2. Sin saldo, conversación abierta y colchón disponible: se atiende con el LLM.
    estado["usado"] = LIMITE + 100
    await _turno(t1, cliente, "¿y cuánto cuesta el plan básico?")
    d = (await _decisiones(t1, conv))[-1]
    assert d["outcome"] == "routed" and d["blocked_by"] is None
    assert (await _resultado(t1, d["id"]))[0]["tokens_in"] > 0

    # 3. Colchón agotado: escala con `creditos_agotados` y no llama al LLM.
    estado["usado"] = LIMITE + 600
    with pytest.MonkeyPatch.context() as mp:
        _sin_llm(mp)
        await _turno(t1, cliente, "¿me confirmas el pedido?")
    d = (await _decisiones(t1, conv))[-1]
    assert (d["outcome"], d["blocked_by"]) == ("blocked", "quota")
    assert (await _resultado(t1, d["id"]))[0]["status"] == "escalated"
    [esc] = await _filas(t1, "SELECT reason FROM escalations WHERE conversation_id = :c", c=conv)
    assert esc["reason"] == "creditos_agotados"
    [c] = await _filas(t1, "SELECT status, bot_paused FROM conversations WHERE id = :c", c=conv)
    assert (c["status"], c["bot_paused"]) == ("escalated", True)
    salida = await _filas(
        t1,
        "SELECT content FROM messages WHERE conversation_id = :c AND direction = 'outbound' "
        "ORDER BY created_at DESC LIMIT 1",
        c=conv,
    )
    assert "persona" in salida[0]["content"]

    # 4. Aunque vuelva el saldo, sigue escalada hasta que una persona la devuelva.
    estado["usado"] = 0
    with pytest.MonkeyPatch.context() as mp:
        _sin_llm(mp)
        await _turno(t1, cliente, "¿hola?")
    d = (await _decisiones(t1, conv))[-1]
    assert d["blocked_by"] == "escalated_conversation"

    # Avisos del ciclo: 80, 100 y colchón, una vez cada uno.
    avisos = await _filas(
        t1,
        "SELECT dedupe_key FROM notifications WHERE dedupe_key LIKE :k",
        k=f"creditos:%:{int(estado['ciclo'].timestamp())}",
    )
    assert sorted(a["dedupe_key"].split(":")[1] for a in avisos) == ["100", "80", "colchon"]


async def test_conversacion_nueva_sin_saldo_responde_una_vez_y_se_desbloquea(tenant_setup, monkeypatch) -> None:
    t1, _t2 = tenant_setup
    _inmediato(monkeypatch)
    estado = {"usado": LIMITE, "ciclo": datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)}
    _saldo_fijo(monkeypatch, estado)
    cliente = uniq("wa-sin-saldo")

    with pytest.MonkeyPatch.context() as mp:
        _sin_llm(mp)
        await _turno(t1, cliente)
        await _turno(t1, cliente, "¿hay alguien?")
        await _turno(t1, cliente, "hola???")
    conv = await _conversacion_de(t1, cliente)
    decisiones = await _decisiones(t1, conv)
    assert [(d["outcome"], d["blocked_by"]) for d in decisiones] == [("blocked", "quota")] * 3
    estados = [(await _resultado(t1, d["id"]))[0]["status"] for d in decisiones]
    assert estados == ["replied", "silent", "silent"]
    salientes = await _filas(
        t1, "SELECT content FROM messages WHERE conversation_id = :c AND direction = 'outbound'", c=conv
    )
    assert len(salientes) == 1
    # Los turnos bloqueados no abren la conversación (0 tokens, o ni siquiera fila).
    assert (await _abierta(t1, conv))[0] is False
    assert not await _filas(t1, "SELECT 1 FROM escalations WHERE conversation_id = :c", c=conv)

    # Vuelve el saldo (ciclo nuevo): el siguiente mensaje se atiende sin intervención manual.
    estado.update(usado=0, ciclo=estado["ciclo"] + timedelta(days=30))
    await _turno(t1, cliente, "¿ya me pueden atender?")
    d = (await _decisiones(t1, conv))[-1]
    assert d["outcome"] == "routed"
    assert (await _resultado(t1, d["id"]))[0]["status"] == "replied"


async def test_un_turno_sin_tokens_o_anterior_al_episodio_no_abre_la_conversacion(tenant_setup, monkeypatch) -> None:
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    _inmediato(monkeypatch)
    cliente = uniq("wa-cero-tokens")
    await _turno(t1, cliente)
    conv = await _conversacion_de(t1, cliente)
    async with tenant_scope(t1):
        async with get_session() as session:
            # El episodio empieza justo después del turno atendido: esa fila ya no cuenta.
            await session.execute(
                text(
                    "UPDATE conversations SET episode_started_at = ("
                    "  SELECT max(created_at) + interval '1 millisecond' FROM usage_ledger WHERE conversation_id = :c"
                    ") WHERE id = :c"
                ),
                {"c": conv},
            )
    assert (await _abierta(t1, conv))[0] is False, "un turno anterior al episodio no cuenta"

    def _fila(kind: str, entrada: int, salida: int) -> Any:
        return UsageLedger(
            tenant_id=t1, conversation_id=conv, kind=kind, model="claude-haiku-4-5",
            input_tokens=entrada, output_tokens=salida, cost_usd=Decimal("0.001") if entrada else Decimal(0),
        )

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add_all([_fila("turn", 0, 0), _fila("agent_run", 100, 10)])
    assert (await _abierta(t1, conv))[0] is False, "ni un turno bloqueado (0 tokens) ni un `agent_run` la abren"

    async with tenant_scope(t1):
        async with get_session() as session:
            session.add(_fila("turn", 100, 10))
    abierta, episodio = await _abierta(t1, conv)
    assert abierta is True and episodio is not None


async def test_otro_negocio_no_ve_la_conversacion_ni_el_consumo_ajeno(tenant_setup, monkeypatch) -> None:
    from core.billing.creditos import conversacion_abierta
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    t1, t2 = tenant_setup
    _inmediato(monkeypatch)
    cliente = uniq("wa-ajena")
    await _turno(t1, cliente)
    conv = await _conversacion_de(t1, cliente)
    assert (await _abierta(t1, conv))[0] is True

    async with tenant_scope(t2):
        async with get_session() as session:
            # Con el id ajeno y su propio scope: la RLS y el filtro por negocio la esconden.
            assert await conversacion_abierta(session, t2, conv) == (False, None)
            # Con el scope de t2, preguntar por t1 se rechaza (no mide en silencio).
            with pytest.raises(RuntimeError):
                await conversacion_abierta(session, t1, conv)
