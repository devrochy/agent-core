"""HU-AOS-08 · procedimientos de agenda, cambio de plan y promesa de pago, contra el stack real.

Gherkin de la HU (issue #369), con el motor activo (`aos_procedures`) y el modelo guionado (sin
LLM real), por el loop de verdad:
- **Agendar:** un horario no ofrecido lo rechaza el resolver y el modelo recibe los disponibles;
  uno libre avanza, y la cita se crea solo después del botón.
- **Cambio de plan:** Basic → Premium con precio y prorrateo del catálogo del servidor (ADR-0111);
  el botón muestra el monto del servidor y el cargo se crea solo al confirmar. `sub_cambiar_plan`
  no se ofrece con el motor activo (no se puede saltar el botón). Un monto distinto no cobra.
- **Promesa de pago:** cliente en mora promete "el viernes": la fecha pasa las reglas del pack y
  la promesa queda por el saldo real. Una fecha fuera de regla y un saldo distinto se rechazan.
- **Bandera apagada:** las acciones nuevas no se ofrecen y `sub_cambiar_plan` sí.
"""

from __future__ import annotations

import os
import types
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

HORAS = '{"mon": "09:00-18:00", "tue": "09:00-18:00", "wed": "09:00-18:00", "thu": "09:00-18:00", "fri": "09:00-18:00", "sat": "09:00-18:00", "sun": "09:00-18:00"}'
PREMIUM = {"plan_code": "PLAN-PREMIUM", "nombre": "Premium", "precio": 600000.0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}
VARIANTE_PREMIUM = str(uuid.uuid4())
SOLO_PROCEDIMIENTO = {"sub_cotizar_cambio_plan", "sub_aplicar_cambio_plan", "cob_registrar_promesa_saldo"}


# ---------------------------------------------------------------- packs registrados en el proceso


class _Catalogo:
    provider_id = "pack_subscriptions"

    async def plan_por_codigo(self, codigo: str) -> tuple[str, dict[str, Any]] | None:
        return (VARIANTE_PREMIUM, dict(PREMIUM)) if codigo.upper() == "PLAN-PREMIUM" else None


@pytest.fixture(scope="module")
def packs_registrados() -> Any:
    """Las skills del core, el proveedor de suscripciones y un catálogo de planes de prueba
    (ADR-0111). Los packs los activa el alta del negocio (`onboard_tenant`); `_asegurar_pack`
    registra a mano lo que falte si la activación no corrió en este proceso."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.subscriptions import plan_catalog_registry, subscription_registry
    from core.tools.core_skill import register_core_skills

    plugin_manager.scan()
    register_core_skills(mcp_host)
    sin_proveedor = subscription_registry.get("pack_subscriptions") is None
    if sin_proveedor:
        subscription_registry.register("pack_subscriptions", SubscriptionCapabilityImpl())
    plan_catalog_registry.register("pack_subscriptions", _Catalogo())  # type: ignore[arg-type]
    yield
    plan_catalog_registry.unregister("pack_subscriptions")
    if sin_proveedor:
        subscription_registry.unregister("pack_subscriptions")


def _asegurar_pack(pack_id: str) -> None:
    from pack_calendar.procedimientos import registrar as reg_calendar
    from pack_calendar.skills import CalendarSkill
    from pack_cobranza.procedimientos import registrar as reg_cobranza
    from pack_cobranza.skills import CobranzaSkill
    from pack_subscriptions.procedimientos import registrar as reg_subs

    from core.mcp.host import mcp_host

    if pack_id in mcp_host.get_procedures():
        return
    skill, registrar = {
        "pack_calendar": (CalendarSkill, reg_calendar),
        "pack_cobranza": (CobranzaSkill, reg_cobranza),
        "pack_subscriptions": (None, reg_subs),
    }[pack_id]
    mcp_host.remove_plugin(pack_id)
    mcp_host.begin_activation(pack_id)
    try:
        if skill is not None:
            mcp_host.register_skill(skill())
        registrar(mcp_host, pack_id)
    finally:
        mcp_host.end_activation()


# ---------------------------------------------------------------- helpers


@asynccontextmanager
async def _scope(tid: uuid.UUID) -> AsyncIterator[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            yield session


async def _banderas(tid: uuid.UUID, procedimientos: bool) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.migrations_database_url or settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("update tenants set aos_procedures = false where id = :t"), {"t": tid})
            await conn.execute(
                text("update tenants set aos_runtime = :r, aos_procedures = :p where id = :t"),
                {"t": tid, "r": "v3" if procedimientos else "legacy", "p": procedimientos},
            )
    finally:
        await engine.dispose()


async def _negocio(packs: list[str]) -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = f"aos08-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(name=f"AOS08 {slug}", slug=slug, packs=packs, sembrar_ejemplos=False, plan="premium")
    for pack_id in packs:
        _asegurar_pack(pack_id)
    return tid


async def _conversacion(tid: uuid.UUID) -> Any:
    from core.db.models import Conversation, Customer

    async with _scope(tid) as s:
        cliente = Customer(tenant_id=tid, channel="whatsapp", external_id=f"whatsapp:+57300{uuid.uuid4().int % 10**7:07d}")
        s.add(cliente)
        await s.flush()
        conv = Conversation(tenant_id=tid, customer_id=cliente.id, channel="whatsapp")
        s.add(conv)
        await s.flush()
        return types.SimpleNamespace(id=conv.id, tenant_id=tid, customer_id=cliente.id, channel="whatsapp")


async def _entrante(conv: Any, contenido: str) -> None:
    from core.db.models import Message

    async with _scope(conv.tenant_id) as s:
        s.add(Message(tenant_id=conv.tenant_id, conversation_id=conv.id, direction="inbound",
                      sender_type="customer", content=contenido))


async def _saliente(conv: Any) -> None:
    from core.db.models import Message

    async with _scope(conv.tenant_id) as s:
        s.add(Message(tenant_id=conv.tenant_id, conversation_id=conv.id, direction="outbound",
                      sender_type="agent", content="(respuesta)"))


def _nombre_de_tool(t: Any) -> str:
    if isinstance(t, dict):
        return str(t.get("name") or (t.get("function") or {}).get("name") or "")
    return str(getattr(t, "name", ""))


class _Guion:
    """Modelo guionado: devuelve los pasos en orden y guarda lo que vio (mensajes y tools)."""

    pricing_per_mtok = (0.0, 0.0)
    model = "deepseek-flash"
    provider_name = "deepseek"

    def __init__(self, pasos: list[Any]) -> None:
        self.pasos = list(pasos)
        self.vistos: list[str] = []
        self.tools: list[set[str]] = []

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return 0.0

    async def generate(self, **kwargs: Any) -> Any:
        from core.llm.types import LLMResponse, LLMToolCall, LLMUsageInfo

        self.vistos.append(repr(kwargs.get("messages")))
        self.tools.append({_nombre_de_tool(t) for t in (kwargs.get("tools") or [])})
        paso = self.pasos.pop(0) if self.pasos else "Listo."
        uso = LLMUsageInfo(input_tokens=5, output_tokens=5)
        if isinstance(paso, tuple):
            nombre, args = paso
            return LLMResponse(content="", stop_reason="tool_use", model=self.model, usage=uso,
                               tool_calls=[LLMToolCall(id=f"call-{uuid.uuid4().hex[:6]}", name=nombre, arguments=args)])
        return LLMResponse(content=paso, stop_reason="end_turn", model=self.model, usage=uso)


async def _turno(conv: Any, guion: _Guion, monkeypatch, texto: str) -> Any:  # type: ignore[no-untyped-def]
    from core.orchestrator.loop import process_conversation
    from core.shared.tenant import tenant_scope

    await _entrante(conv, texto)
    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", lambda *a, **k: guion)
    async with tenant_scope(conv.tenant_id):
        resultado = await process_conversation(conv.id, texto)
    await _saliente(conv)
    return resultado


async def _tocar_boton(conv: Any, resultado: Any) -> None:
    from core.kernel.procedures.ejecucion import entrada_sin_llm
    from core.shared.tenant import tenant_scope

    assert resultado is not None and resultado.confirmacion is not None
    payload = resultado.confirmacion.confirmacion.payload
    assert payload.startswith("confirmar:")
    async with tenant_scope(conv.tenant_id):
        await entrada_sin_llm(conv, {"interactive_id": payload})


async def _tarea(conv: Any, procedure_id: str) -> Any:
    from core.db.models import ConversationTask
    from core.kernel import state

    async with _scope(conv.tenant_id) as s:
        fila = (
            await s.execute(
                select(ConversationTask).where(
                    ConversationTask.tenant_id == conv.tenant_id,
                    ConversationTask.conversation_id == conv.id,
                    ConversationTask.procedure_id == procedure_id,
                )
            )
        ).scalars().first()
        assert fila is not None, f"sin tarea {procedure_id}"
        return state.proyeccion_de(fila), fila.status


def _ctx(conv: Any) -> Any:
    from core.mcp.types import ToolContext

    return ToolContext(conversation_id=conv.id, customer_id=conv.customer_id, idempotency_key=str(uuid.uuid4()), channel="whatsapp")


# ---------------------------------------------------------------- Gherkin: agendar


async def test_agendar_rechaza_un_horario_no_ofrecido_y_ofrece_los_disponibles(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_calendar.models import Appointment

    from core.plugin_manager import plugin_manager

    tid = await _negocio(["pack_calendar"])
    async with _scope(tid):
        await plugin_manager.save_config("pack_calendar", {
            "source": "interno", "business_hours": HORAS, "timezone": "America/Bogota",
            "slot_minutes": 60, "buffer_minutes": 0, "event_duration_minutes": 60,
        })
    dia = (datetime.now(ZoneInfo("America/Bogota")) + timedelta(days=7)).date().isoformat()
    proc = "pack_calendar.agendar_cita"
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": proc}),
            ("actualizar_tarea", {"slots": {"horario": f"{dia}T07:00:00"}}),   # antes de abrir: no ofrecido
            ("actualizar_tarea", {"slots": {"horario": f"{dia}T10:00:00"}}),
            "Perfecto, ¿a qué correo te envío la confirmación?",
        ])
        await _turno(conv, guion, monkeypatch, "quiero una cita a las 7 de la mañana")
        # El error del resolver llegó al modelo con los horarios libres del servidor.
        assert any("Ese horario no está disponible" in v and "09:00, 10:00" in v for v in guion.vistos)
        tarea, estado = await _tarea(conv, proc)
        assert estado == "active" and tarea.stage == "datos"
        assert (tarea.slots.get("horario") or {}).get("value", "").startswith(f"{dia}T10:00:00")

        args = {"title": "Cita con Ana", "start_datetime": f"{dia}T10:00:00", "attendee_email": "ana@example.com"}
        guion = _Guion([
            ("actualizar_tarea", {"slots": {"correo": "ana@example.com"}}),
            ("create_calendar_event", args),
            "Te agendo el martes a las 10, ¿confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "mi correo es ana@example.com")
        async with _scope(tid) as s:
            assert (await s.execute(select(Appointment).where(Appointment.tenant_id == tid))).first() is None
        assert resultado is not None and "ana@example.com" in resultado.text

        await _tocar_boton(conv, resultado)
        guion = _Guion([("create_calendar_event", args), "Listo, quedó agendada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        async with _scope(tid) as s:
            citas = (await s.execute(select(Appointment).where(Appointment.tenant_id == tid))).scalars().all()
        assert len(citas) == 1
        _tarea_final, estado = await _tarea(conv, proc)
        assert estado == "completed"
    finally:
        await _banderas(tid, False)


# ---------------------------------------------------------------- Gherkin: cambio de plan


async def _suscripcion_basic(tid: uuid.UUID, customer_id: uuid.UUID) -> uuid.UUID:
    from pack_subscriptions.capability import crear_suscripcion_para
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    basic = {"plan_code": "PLAN-BASIC", "nombre": "Basic", "precio": 200000.0, "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0}
    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        sub = await crear_suscripcion_para(customer_id=customer_id, variante_id=uuid.uuid4(), plan=basic)
    async with _scope(tid) as s:
        fila = await s.get(Subscription, sub.id)
        fila.periodo_actual_inicio = ahora - timedelta(days=15)
        fila.proximo_cargo = ahora + timedelta(days=15)
    return sub.id


async def _cargos(tid: uuid.UUID, sid: uuid.UUID) -> list[Any]:
    from pack_subscriptions.models import SubscriptionCharge

    async with _scope(tid) as s:
        return list((await s.execute(
            select(SubscriptionCharge).where(SubscriptionCharge.tenant_id == tid, SubscriptionCharge.subscription_id == sid)
        )).scalars().all())


async def test_cambio_de_plan_con_precio_del_catalogo_y_boton_antes_de_cobrar(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio(["pack_subscriptions"])
    proc = "pack_subscriptions.cambiar_plan"
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        sid = await _suscripcion_basic(tid, conv.customer_id)
        args = {"plan_code": "PLAN-PREMIUM", "monto_a_pagar": 200000}
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": proc}),
            ("actualizar_tarea", {"slots": {"plan_code": "plan-premium"}}),
            ("sub_cotizar_cambio_plan", {"plan_code": "PLAN-PREMIUM"}),
            ("sub_aplicar_cambio_plan", args),
            "Pasarte a Premium te cuesta $1, ¿confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "quiero pasarme al premium")
        # Con el motor activo la tool de siempre no se ofrece: no hay cómo saltarse el botón.
        assert all("sub_cambiar_plan" not in tools for tools in guion.tools)
        assert any("sub_aplicar_cambio_plan" in tools for tools in guion.tools)
        # El botón muestra el monto y el precio del servidor (prorrateo: 400.000 × 15/30), no el texto del modelo.
        assert resultado is not None and resultado.confirmacion is not None
        assert "$1," not in resultado.text
        assert "200.000" in resultado.text and "600.000" in resultado.text
        assert await _cargos(tid, sid) == []  # nada se cobra antes de confirmar
        tarea, _ = await _tarea(conv, proc)
        assert tarea.stage == "confirmar" and (tarea.slots.get("plan_code") or {}).get("value") == "PLAN-PREMIUM"

        await _tocar_boton(conv, resultado)
        guion = _Guion([("sub_aplicar_cambio_plan", args), "Listo, te envío el pago de la diferencia."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        cargos = await _cargos(tid, sid)
        assert [(c.tipo, c.estado, Decimal(c.monto)) for c in cargos] == [("cambio_plan", "pendiente", Decimal("200000"))]
        tarea, estado = await _tarea(conv, proc)
        assert estado == "completed" and (tarea.slots.get("cambio") or {}).get("value") == "pendiente_pago"
    finally:
        await _banderas(tid, False)


async def test_cambio_de_plan_con_monto_distinto_no_cobra_y_el_resolver_rechaza_el_mismo_plan(packs_registrados) -> None:  # type: ignore[no-untyped-def]
    from pack_subscriptions.procedimientos import plan_destino

    from core.shared.tenant import tenant_scope
    from core.subscriptions import SubscriptionError
    from core.subscriptions.tools import SubscriptionSkill

    tid = await _negocio(["pack_subscriptions"])
    conv = await _conversacion(tid)
    sid = await _suscripcion_basic(tid, conv.customer_id)
    async with tenant_scope(tid):
        with pytest.raises(SubscriptionError) as rechazo:
            await SubscriptionSkill().aplicar_cambio_plan({"plan_code": "PLAN-PREMIUM", "monto_a_pagar": 150000}, _ctx(conv))
        no_existe = await plan_destino("PLAN-ORO", _ctx(conv), {})
        ok = await plan_destino("plan-premium", _ctx(conv), {})
        cotizacion = await SubscriptionSkill().cotizar_cambio_plan({"plan_code": "PLAN-PREMIUM"}, _ctx(conv))
    assert rechazo.value.code == "monto_distinto"
    assert await _cargos(tid, sid) == []
    assert not no_existe.ok and "PLAN-ORO" in no_existe.error
    assert ok.ok and ok.value == "PLAN-PREMIUM"
    assert (cotizacion["cambio"], cotizacion["monto_a_pagar"], cotizacion["precio_nuevo"]) == ("pendiente_pago", 200000.0, 600000.0)


# ---------------------------------------------------------------- Gherkin: promesa de pago


async def _cliente_en_mora(tid: uuid.UUID, customer_id: uuid.UUID) -> uuid.UUID:
    from pack_cobranza.models import Cartera, Cliente, Cuota, Prestamo

    hoy = date.today()
    async with _scope(tid) as s:
        cartera = Cartera(tenant_id=tid, nombre="Cartera A")
        s.add(cartera)
        await s.flush()
        cliente = Cliente(tenant_id=tid, cartera_id=cartera.id, nombre="Juana", apellido="Pérez", customer_id=customer_id)
        s.add(cliente)
        await s.flush()
        prestamo = Prestamo(
            tenant_id=tid, cliente_id=cliente.id, cartera_id=cartera.id, valor=Decimal("600000"),
            saldo=Decimal("600000"), num_cuotas=2, tipo_interes=Decimal("0"), dias_entre_cuotas=30,
            fecha_otorgado=hoy - timedelta(days=40),
        )
        s.add(prestamo)
        await s.flush()
        s.add(Cuota(tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=1, valor_esperado=Decimal("300000"),
                    fecha_vencimiento=hoy - timedelta(days=10), estatus="atrasada"))
        s.add(Cuota(tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=2, valor_esperado=Decimal("300000"),
                    fecha_vencimiento=hoy + timedelta(days=20), estatus="pendiente"))
        return prestamo.id


def _proximo_viernes() -> date:
    hoy = date.today()
    return hoy + timedelta(days=((4 - hoy.weekday()) % 7) or 7)


async def test_promesa_de_pago_el_viernes_por_el_saldo_real(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from pack_cobranza.models import Promesa

    tid = await _negocio(["pack_cobranza"])
    proc = "pack_cobranza.promesa_de_pago"
    viernes = _proximo_viernes().isoformat()
    lejos = (date.today() + timedelta(days=40)).isoformat()
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        prestamo_id = await _cliente_en_mora(tid, conv.customer_id)
        args = {"fecha": viernes, "saldo": 600000}
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": proc}),
            ("cob_consultar_saldo", {}),
            ("actualizar_tarea", {"slots": {"fecha": lejos}}),       # fuera de la regla de la cartera
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            ("cob_registrar_promesa_saldo", args),
            "Te anoto la promesa por $10, ¿confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "te pago el viernes")
        assert any("La cartera acepta promesas hasta" in v for v in guion.vistos)
        assert resultado is not None and resultado.confirmacion is not None
        assert "$10" not in resultado.text and "600.000" in resultado.text
        async with _scope(tid) as s:
            assert (await s.execute(select(Promesa).where(Promesa.tenant_id == tid))).first() is None

        await _tocar_boton(conv, resultado)
        guion = _Guion([("cob_registrar_promesa_saldo", args), "Listo, promesa registrada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        async with _scope(tid) as s:
            promesas = (await s.execute(select(Promesa).where(Promesa.tenant_id == tid))).scalars().all()
        assert [(p.prestamo_id, p.fecha_prometida.isoformat(), Decimal(p.valor_prometido)) for p in promesas] == [
            (prestamo_id, viernes, Decimal("600000"))
        ]
        tarea, estado = await _tarea(conv, proc)
        assert estado == "completed" and (tarea.slots.get("promesa_id") or {}).get("value") == str(promesas[0].id)
    finally:
        await _banderas(tid, False)


async def test_promesa_con_saldo_distinto_no_se_registra(packs_registrados) -> None:  # type: ignore[no-untyped-def]
    from pack_cobranza.models import Promesa
    from pack_cobranza.skills import CobranzaSkill

    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_cobranza"])
    conv = await _conversacion(tid)
    await _cliente_en_mora(tid, conv.customer_id)
    async with tenant_scope(tid):
        r = await CobranzaSkill().registrar_promesa_saldo({"fecha": _proximo_viernes().isoformat(), "saldo": 100}, _ctx(conv))
    assert "error" in r and r["saldo"] == 600000.0
    async with _scope(tid) as s:
        assert (await s.execute(select(Promesa).where(Promesa.tenant_id == tid))).first() is None


# ---------------------------------------------------------------- bandera apagada


async def test_con_la_bandera_apagada_las_acciones_nuevas_no_se_ofrecen(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio(["pack_subscriptions", "pack_cobranza"])
    conv = await _conversacion(tid)
    await _suscripcion_basic(tid, conv.customer_id)
    guion = _Guion(["Hola, ¿en qué te ayudo?"])
    await _turno(conv, guion, monkeypatch, "hola")
    ofrecidas = guion.tools[0]
    assert "sub_cambiar_plan" in ofrecidas and "cob_registrar_promesa_pago" in ofrecidas
    assert not (SOLO_PROCEDIMIENTO & ofrecidas)
    assert not ({"iniciar_procedimiento", "actualizar_tarea"} & ofrecidas)
