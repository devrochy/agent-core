"""Ledger del Coordinador contra el stack real (HU-AOS-10, adenda de ADR-0112).

- Cada turno deja una fila en `core_decisions` y otra en `turn_outcomes` con el mismo
  `turn_id`, y el `trace_id` del turno; lo que el turno escribe (`usage_ledger`, mensajes
  salientes) lleva ese `turn_id`.
- Negocio pausado, sin cuota o con la conexión pausada (ADR-0122): decisión `blocked` con su
  motivo y sin LLM.
- RLS: un negocio no ve las filas de otro; una escritura de otro negocio dentro de un turno no
  hereda su `turn_id` (la FK compuesta la rechazaría).

El LLM es el `FakeProvider` guionado del conftest: nunca hay LLM real.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from conftest import PHONE
from sqlalchemy import text

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,  # RLS de las tablas del Coordinador y camino del consumer
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


def _inbound(
    tid: uuid.UUID, cliente: str, contenido: str = "hola, ¿qué planes tienen?", phone: str = PHONE
) -> dict:
    return {
        "tenant_id": str(tid),
        "provider": "meta",
        "channel": "whatsapp",
        "external_message_id": uniq("wamid"),
        "customer_external_id": cliente,
        "customer_name": "Cliente",
        "content": contenido,
        "connection_external_id": phone,
        "media": "[]",
        "media_context": "",
    }


def _inmediato(monkeypatch) -> None:
    from core.config import DEBOUNCE_DELAY_MS

    for modo in list(DEBOUNCE_DELAY_MS):
        monkeypatch.setitem(DEBOUNCE_DELAY_MS, modo, 0)


def _sin_llm(monkeypatch) -> None:
    class _Prohibido:
        provider_name = "prohibido"

        def estimate_cost_usd(self, *_a: Any) -> float:
            return 0.0

        async def generate(self, **_kwargs: Any):
            raise AssertionError("se llamó al LLM en un turno bloqueado")

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: _Prohibido())


async def _filas(tid: uuid.UUID, sql: str, **params: Any) -> list[dict[str, Any]]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            return [dict(r._mapping) for r in (await session.execute(text(sql), params)).all()]


async def _conversacion_de(tid: uuid.UUID, cliente: str) -> uuid.UUID:
    filas = await _filas(
        tid,
        "SELECT cv.id FROM conversations cv JOIN customers c ON c.id = cv.customer_id "
        "WHERE c.external_id = :e ORDER BY cv.started_at DESC LIMIT 1",
        e=cliente,
    )
    return filas[0]["id"]


async def _decisiones(tid: uuid.UUID, conv: uuid.UUID) -> list[dict[str, Any]]:
    return await _filas(
        tid, "SELECT * FROM core_decisions WHERE conversation_id = :c ORDER BY created_at", c=conv
    )


async def _resultado(tid: uuid.UUID, turn_id: uuid.UUID) -> list[dict[str, Any]]:
    return await _filas(tid, "SELECT * FROM turn_outcomes WHERE turn_id = :t", t=turn_id)


# ---------------------------------------------------------------- decisión y resultado
async def test_un_turno_deja_decision_y_resultado_con_el_mismo_turn_id(
    tenant_setup, monkeypatch
) -> None:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    _inmediato(monkeypatch)
    cliente = uniq("wa-kernel")
    async with tenant_scope(t1):
        await consumer._handle_inbound_scoped(_inbound(t1, cliente))

    conv = await _conversacion_de(t1, cliente)
    decisiones = await _decisiones(t1, conv)
    assert len(decisiones) == 1, decisiones
    d = decisiones[0]
    assert d["outcome"] == "routed" and d["route_source"] == "default" and d["blocked_by"] is None
    # OTel apagado: el trace_id de respaldo es el turn_id (32 hex, formato W3C).
    assert d["trace_id"] == d["id"].hex
    conexion = await _filas(t1, "SELECT connection_id FROM conversations WHERE id = :c", c=conv)
    assert d["connection_id"] == conexion[0]["connection_id"] is not None
    entrante = await _filas(
        t1,
        "SELECT id, turn_id FROM messages WHERE conversation_id = :c AND direction = 'inbound'",
        c=conv,
    )
    assert d["inbound_message_ids"] == [entrante[0]["id"]]
    assert entrante[0]["turn_id"] is None, "el inbound se guarda antes de decidir"

    resultado = await _resultado(t1, d["id"])
    assert len(resultado) == 1
    assert resultado[0]["status"] == "replied"
    assert resultado[0]["tokens_in"] > 0 and resultado[0]["latency_ms"] >= 0

    ledger = await _filas(
        t1, "SELECT turn_id, trace_id FROM usage_ledger WHERE conversation_id = :c", c=conv
    )
    assert ledger == [{"turn_id": d["id"], "trace_id": d["trace_id"]}]
    salientes = await _filas(
        t1,
        "SELECT turn_id FROM messages WHERE conversation_id = :c AND direction = 'outbound'",
        c=conv,
    )
    assert salientes and all(m["turn_id"] == d["id"] for m in salientes)


async def test_cada_turno_es_una_fila_nueva_y_el_debounce_decide_al_disparar(
    tenant_setup, monkeypatch
) -> None:
    from core.orchestrator import consumer, debounce
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    agendados: list[uuid.UUID] = []

    async def _agendar(conversation_id, _delay, _payload):
        agendados.append(conversation_id)

    monkeypatch.setattr(consumer, "schedule_debounce", _agendar)
    from core.config import DEBOUNCE_DELAY_MS

    for modo in list(DEBOUNCE_DELAY_MS):  # otros tests dejan al negocio en `inmediato`
        monkeypatch.setitem(DEBOUNCE_DELAY_MS, modo, 1000)
    cliente = uniq("wa-deb")
    async with tenant_scope(t1):
        await consumer._handle_inbound_scoped(_inbound(t1, cliente))
    conv = await _conversacion_de(t1, cliente)
    assert agendados == [conv]
    assert await _decisiones(t1, conv) == [], "agendar no es un turno: decide el disparo"

    for _ in range(2):
        await debounce._fire_conversation(conv, {"latest_message": "hola", "tenant_id": str(t1)})
    decisiones = await _decisiones(t1, conv)
    assert [d["outcome"] for d in decisiones] == ["routed", "routed"]
    assert decisiones[0]["id"] != decisiones[1]["id"]
    assert all(d["inbound_message_ids"] == [] for d in decisiones), (
        "en debounce los ids quedan pendientes"
    )
    for d in decisiones:
        assert [r["status"] for r in await _resultado(t1, d["id"])] == ["replied"]


# ---------------------------------------------------------------- bloqueos sin LLM
async def test_negocio_pausado_bloquea_sin_llm(monkeypatch) -> None:
    from core.alta_negocio import onboard_tenant
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    slug = uniq("kpausa")
    tid, _ = await onboard_tenant(
        name=f"K {slug}", slug=slug, persona={"tone": "cercano"}, admin_email="admin@local"
    )
    from sqlalchemy import select

    async with tenant_scope(tid):
        async with get_session() as session:
            ajustes = (
                await session.execute(
                    select(AppSettings).where(
                        AppSettings.tenant_id == tid, AppSettings.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if ajustes is None:
                session.add(AppSettings(tenant_id=tid, bot_paused=True))
            else:
                ajustes.bot_paused = True
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    cliente = uniq("wa-kp")
    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, phone=uniq("sin-conexion")))

    conv = await _conversacion_de(tid, cliente)
    [d] = await _decisiones(tid, conv)
    assert (d["outcome"], d["blocked_by"], d["route_source"]) == ("blocked", "kill_switch", None)
    assert [r["status"] for r in await _resultado(tid, d["id"])] == ["silent"]
    assert await _filas(tid, "SELECT 1 FROM usage_ledger WHERE conversation_id = :c", c=conv) == []


async def test_sin_cuota_bloquea_sin_llm_y_responde_el_cierre(tenant_setup, monkeypatch) -> None:
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup

    async def _agotada(_tid):
        return "mensajes"

    monkeypatch.setattr("core.billing.enforcement.turn_quota_metric", _agotada)
    _inmediato(monkeypatch)
    _sin_llm(monkeypatch)
    cliente = uniq("wa-kq")
    async with tenant_scope(t1):
        await consumer._handle_inbound_scoped(_inbound(t1, cliente))

    conv = await _conversacion_de(t1, cliente)
    [d] = await _decisiones(t1, conv)
    assert (d["outcome"], d["blocked_by"]) == ("blocked", "quota")
    [r] = await _resultado(t1, d["id"])
    assert r["status"] == "replied" and r["tokens_in"] == 0
    salientes = await _filas(
        t1,
        "SELECT turn_id FROM messages WHERE conversation_id = :c AND direction = 'outbound'",
        c=conv,
    )
    assert salientes and all(m["turn_id"] == d["id"] for m in salientes), (
        "el cierre lleva el turn_id"
    )


async def test_conexion_pausada_guarda_sin_ia_y_no_ejecuta_el_coordinador(monkeypatch) -> None:
    from test_api_registrar_numero import _negocio_con_byok
    from test_conexion_ciclo_de_vida import _estado, _sin_ia

    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    tid, cid, _waba, phone_id = await _negocio_con_byok()
    await _estado(tid, cid, "revoked")
    _sin_ia(monkeypatch)
    cliente = uniq("wa-kcp")
    async with tenant_scope(tid):
        await consumer._handle_inbound_scoped(_inbound(tid, cliente, phone=phone_id))

    conv = await _conversacion_de(tid, cliente)
    [d] = await _decisiones(tid, conv)
    assert (d["outcome"], d["blocked_by"]) == ("blocked", "connection_paused")
    assert d["connection_id"] is None, "Embedded Signup/BYOK: sin conexión canónica hasta HU-AOS-11"
    assert [r["status"] for r in await _resultado(tid, d["id"])] == ["silent"]
    assert len(await _filas(tid, "SELECT 1 FROM messages WHERE conversation_id = :c", c=conv)) == 1


# ---------------------------------------------------------------- aislamiento
async def test_un_negocio_no_ve_las_decisiones_de_otro(tenant_setup, monkeypatch) -> None:
    from core.db.session import get_session
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    t1, t2 = tenant_setup
    _inmediato(monkeypatch)
    cliente = uniq("wa-krls")
    async with tenant_scope(t1):
        await consumer._handle_inbound_scoped(_inbound(t1, cliente))
    conv = await _conversacion_de(t1, cliente)
    [d] = await _decisiones(t1, conv)

    assert await _decisiones(t2, conv) == []
    assert await _resultado(t2, d["id"]) == []
    async with get_session() as session:
        sin_negocio = (
            await session.execute(text("SELECT count(*) FROM core_decisions"))
        ).scalar_one()
    assert sin_negocio == 0, "sin tenant_scope no se ve ninguna decisión"


async def test_lo_que_se_escribe_para_otro_negocio_dentro_de_un_turno_no_lleva_su_turn_id(
    tenant_setup,
) -> None:
    from core.kernel.decisions import en_turno, enrutar
    from core.shared.audit import record_audit
    from core.shared.tenant import tenant_scope

    t1, t2 = tenant_setup
    cliente = uniq("wa-kx")
    marca = uniq("kernel-x")

    async with tenant_scope(t1):
        from core.orchestrator import consumer

        conversacion, _c, _s = await consumer._resolve_conversation(
            {
                "channel": "whatsapp",
                "customer_external_id": cliente,
                "connection_external_id": PHONE,
            }
        )

        @en_turno
        async def _turno() -> None:
            await enrutar(conversacion)
            await record_audit(actor="test:kernel", action=marca)
            # Escritura de otro negocio en medio del turno (p. ej. cobro de plataforma).
            async with tenant_scope(t2):
                await record_audit(actor="test:kernel", action=marca)

        await _turno()
        # Fuera del turno no queda turn_id vigente.
        await record_audit(actor="test:kernel", action=marca + "-despues")

    propias = await _filas(
        t1, "SELECT action, turn_id FROM audit_log WHERE action LIKE :m", m=marca + "%"
    )
    ajenas = await _filas(t2, "SELECT turn_id FROM audit_log WHERE action = :m", m=marca)
    [d] = await _decisiones(t1, conversacion.id)
    assert {(f["action"], f["turn_id"]) for f in propias} == {
        (marca, d["id"]),
        (marca + "-despues", None),
    }
    assert ajenas == [{"turn_id": None}], "la fila del otro negocio se escribe y sin turn_id"


async def test_con_la_bandera_apagada_no_hay_filas_ni_turn_id(tenant_setup, monkeypatch) -> None:
    from core.config import get_settings
    from core.orchestrator import consumer
    from core.shared.tenant import tenant_scope

    t1, _t2 = tenant_setup
    _inmediato(monkeypatch)
    monkeypatch.setenv("KERNEL_LEDGER_ENABLED", "false")
    get_settings.cache_clear()
    try:
        cliente = uniq("wa-koff")
        async with tenant_scope(t1):
            await consumer._handle_inbound_scoped(_inbound(t1, cliente))
    finally:
        monkeypatch.delenv("KERNEL_LEDGER_ENABLED")
        get_settings.cache_clear()
    conv = await _conversacion_de(t1, cliente)
    assert await _decisiones(t1, conv) == []
    assert await _filas(
        t1, "SELECT turn_id FROM usage_ledger WHERE conversation_id = :c", c=conv
    ) == [{"turn_id": None}]
