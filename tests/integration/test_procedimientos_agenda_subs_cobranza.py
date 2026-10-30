"""HU-AOS-08 · procedimientos de agenda, cambio de plan y promesa de pago, contra el stack real.

Gherkin de la HU (issue #369), con el motor activo (`aos_procedures`) y el modelo guionado (sin
LLM real), por el loop de verdad:
- **Agendar:** un horario no ofrecido lo rechaza el resolver y el modelo recibe los disponibles;
  uno libre avanza, y la cita se crea solo después del botón.
- **Cambio de plan:** Basic → Premium con precio y prorrateo del catálogo del servidor (ADR-0111);
  el botón muestra el monto del servidor y el cargo se crea solo al confirmar. `sub_cambiar_plan`
  no se ofrece con el motor activo (no se puede saltar el botón). Un monto distinto no cobra.
- **Promesa de pago:** cliente en mora promete "el viernes": la fecha pasa las reglas del pack y
  la promesa queda por lo vencido o por el saldo total, con el monto del servidor (decisión del
  dueño, 2026-10-11). Una fecha fuera de regla, un alcance que el negocio no permite y un monto
  distinto se rechazan.
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
PLANES = {"PLAN-BASIC": ("Basic", 200000), "PLAN-PREMIUM": ("Premium", 600000)}
SOLO_PROCEDIMIENTO = {
    "sub_cotizar_cambio_plan", "sub_aplicar_cambio_plan", "cob_registrar_promesa_saldo", "cob_opciones_promesa",
}


# ---------------------------------------------------------------- packs registrados en el proceso


@pytest.fixture(scope="module")
def packs_registrados() -> Any:
    """Las skills del core y el proveedor de suscripciones (los planes los crea cada test en el
    catálogo del negocio, ADR-0111). Los packs los activa el alta del negocio (`onboard_tenant`); `_asegurar_pack`
    registra a mano lo que falte si la activación no corrió en este proceso."""
    from pack_subscriptions.capability import SubscriptionCapabilityImpl

    from core.mcp.host import mcp_host
    from core.plugin_manager import plugin_manager
    from core.subscriptions import subscription_registry
    from core.tools.core_skill import register_core_skills

    plugin_manager.scan()
    register_core_skills(mcp_host)
    # El proveedor real (idempotente): no se desregistra, otros tests del proceso lo usan.
    if subscription_registry.get("pack_subscriptions") is None:
        subscription_registry.register("pack_subscriptions", SubscriptionCapabilityImpl())
    yield


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
    from pack_subscriptions import planes
    from pack_subscriptions.capability import crear_suscripcion_para
    from pack_subscriptions.models import Subscription

    from core.shared.tenant import tenant_scope

    ahora = datetime.now(UTC)
    async with tenant_scope(tid):
        ids = {}
        for codigo, (nombre, precio) in PLANES.items():  # el catálogo del negocio: Basic (el actual) y Premium (el destino)
            plan = await planes.por_codigo(codigo, solo_activos=False) or await planes.crear(
                planes.DatosDePlan(codigo=codigo, nombre=nombre, precio=Decimal(precio), periodicidad="mensual")
            )
            ids[codigo] = plan.id
        sub = await crear_suscripcion_para(customer_id=customer_id, plan_id=ids["PLAN-BASIC"])
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
    assert not no_existe.ok and "catálogo" in no_existe.error and "PLAN-ORO" not in no_existe.error
    assert ok.ok and ok.value == "PLAN-PREMIUM"
    assert (cotizacion["cambio"], cotizacion["monto_a_pagar"], cotizacion["precio_nuevo"]) == ("pendiente_pago", 200000.0, 600000.0)


# ---------------------------------------------------------------- Gherkin: promesa de pago


async def _cliente_en_mora(tid: uuid.UUID, customer_id: uuid.UUID, *, con_vencida: bool = True) -> uuid.UUID:
    """Préstamo de 600.000 en dos cuotas de 300.000: la primera vencida hace 10 días (o, sin
    `con_vencida`, por vencer en 5) y la segunda por vencer en 20."""
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
        primera = hoy - timedelta(days=10) if con_vencida else hoy + timedelta(days=5)
        s.add(Cuota(tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=1, valor_esperado=Decimal("300000"),
                    fecha_vencimiento=primera, estatus="atrasada" if con_vencida else "pendiente"))
        s.add(Cuota(tenant_id=tid, prestamo_id=prestamo.id, numero_cuota=2, valor_esperado=Decimal("300000"),
                    fecha_vencimiento=hoy + timedelta(days=20), estatus="pendiente"))
        return prestamo.id


async def _alcance_permitido(tid: uuid.UUID, alcance: str) -> None:
    """La regla del negocio (`pack_cobranza_reglas_negociacion.alcance_promesa`)."""
    from pack_cobranza.models import ReglaNegociacion

    async with _scope(tid) as s:
        s.add(ReglaNegociacion(tenant_id=tid, max_dias_prorroga=15, min_abono_pct=Decimal(10),
                               max_reprogramaciones=2, umbral_saldo_autonomo=Decimal(0), alcance_promesa=alcance))


async def _pagar_cuota_vencida(tid: uuid.UUID, prestamo_id: uuid.UUID) -> None:
    from pack_cobranza.models import Cuota

    async with _scope(tid) as s:
        cuota = (await s.execute(select(Cuota).where(
            Cuota.tenant_id == tid, Cuota.prestamo_id == prestamo_id, Cuota.numero_cuota == 1,
        ))).scalar_one()
        cuota.estatus = "pagada"


async def _promesas(tid: uuid.UUID) -> list[tuple[str, str | None, Decimal]]:
    from pack_cobranza.models import Promesa

    async with _scope(tid) as s:
        filas = (await s.execute(select(Promesa).where(Promesa.tenant_id == tid))).scalars().all()
    return [(p.fecha_prometida.isoformat(), p.alcance, Decimal(p.valor_prometido)) for p in filas]


async def _opciones(tid: uuid.UUID, conv: Any, eleccion: str | None = None) -> dict[str, Any]:
    from pack_cobranza.skills import CobranzaSkill

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        return await CobranzaSkill().opciones_promesa({} if eleccion is None else {"eleccion": eleccion}, _ctx(conv))


async def _registrar(tid: uuid.UUID, conv: Any, alcance: str, monto: Any) -> dict[str, Any]:
    from pack_cobranza.skills import CobranzaSkill

    from core.shared.tenant import tenant_scope

    args = {"fecha": _proximo_viernes().isoformat(), "alcance": alcance, "monto": monto}
    async with tenant_scope(tid):
        return await CobranzaSkill().registrar_promesa_saldo(args, _ctx(conv))


def _proximo_viernes() -> date:
    hoy = date.today()
    return hoy + timedelta(days=((4 - hoy.weekday()) % 7) or 7)


PROC_PROMESA = "pack_cobranza.promesa_de_pago"


async def test_promesa_de_pago_el_viernes_por_el_saldo_total(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Con lo vencido y el saldo total disponibles, el cliente elige el saldo total: el botón muestra
    el alcance y el monto del servidor, y la promesa queda por 600.000 con su alcance."""
    tid = await _negocio(["pack_cobranza"])
    viernes = _proximo_viernes().isoformat()
    lejos = (date.today() + timedelta(days=40)).isoformat()
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        prestamo_id = await _cliente_en_mora(tid, conv.customer_id)
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_PROMESA}),
            ("cob_opciones_promesa", {}),
            ("cob_opciones_promesa", {"eleccion": "saldo_total"}),
            ("actualizar_tarea", {"slots": {"fecha": lejos}}),       # fuera de la regla de la cartera
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            ("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 600000}),
            "Te anoto la promesa por $10, ¿confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "te pago el viernes")
        assert any('"vencido", "monto": 300000.0' in v or "'vencido', 'monto': 300000.0" in v for v in guion.vistos)
        assert any("La cartera acepta promesas hasta" in v for v in guion.vistos)
        assert resultado is not None and resultado.confirmacion is not None
        texto = resultado.text
        assert "$10" not in texto and "600.000" in texto and "el saldo total" in texto
        assert await _promesas(tid) == []

        await _tocar_boton(conv, resultado)
        guion = _Guion([("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 600000}),
                        "Listo, promesa registrada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        assert await _promesas(tid) == [(viernes, "saldo_total", Decimal("600000"))]
        tarea, estado = await _tarea(conv, PROC_PROMESA)
        assert estado == "completed" and (tarea.slots.get("promesa_id") or {}).get("value")
        from pack_cobranza.models import Promesa

        async with _scope(tid) as s:
            assert (await s.execute(select(Promesa.prestamo_id))).scalar_one() == prestamo_id
    finally:
        await _banderas(tid, False)


async def test_promesa_por_lo_vencido_y_cambio_de_opcion_antes_del_si(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """El cliente elige lo vencido (300.000); antes de confirmar cambia al saldo total: se pide otro
    sí con el monto nuevo y el botón viejo ya no registra nada."""
    tid = await _negocio(["pack_cobranza"])
    viernes = _proximo_viernes().isoformat()
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        await _cliente_en_mora(tid, conv.customer_id)
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_PROMESA}),
            ("cob_opciones_promesa", {"eleccion": "vencido"}),
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            # El modelo no pasa el monto: el kernel lo completa con el dato de la tarea.
            ("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "vencido"}),
            "¿Confirmas?",
        ])
        vencido = await _turno(conv, guion, monkeypatch, "el viernes te pago lo atrasado")
        assert vencido is not None and vencido.confirmacion is not None
        assert "300.000" in vencido.text and "lo vencido" in vencido.text

        guion = _Guion([
            ("cob_opciones_promesa", {"eleccion": "saldo_total"}),
            ("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 600000}),
            "¿Confirmas?",
        ])
        total = await _turno(conv, guion, monkeypatch, "mejor todo")
        assert total is not None and total.confirmacion is not None
        assert "600.000" in total.text and "el saldo total" in total.text

        await _tocar_boton(conv, vencido)   # el botón de la petición vieja
        guion = _Guion([("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 600000}), "¿Confirmas?"])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        assert await _promesas(tid) == []

        await _tocar_boton(conv, total)
        guion = _Guion([("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 600000}),
                        "Listo, promesa registrada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        assert await _promesas(tid) == [(viernes, "saldo_total", Decimal("600000"))]
    finally:
        await _banderas(tid, False)


async def test_promesa_por_lo_vencido_registrada(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    tid = await _negocio(["pack_cobranza"])
    viernes = _proximo_viernes().isoformat()
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        await _cliente_en_mora(tid, conv.customer_id)
        args = {"fecha": viernes, "alcance": "vencido", "monto": 300000}
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_PROMESA}),
            ("cob_opciones_promesa", {}),
            ("cob_opciones_promesa", {"eleccion": "vencido"}),
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            ("cob_registrar_promesa_saldo", args),
            "¿Confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "te pago lo atrasado el viernes")
        await _tocar_boton(conv, resultado)
        guion = _Guion([("cob_registrar_promesa_saldo", args), "Listo, promesa registrada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        assert await _promesas(tid) == [(viernes, "vencido", Decimal("300000"))]
        tarea, estado = await _tarea(conv, PROC_PROMESA)
        assert estado == "completed"
        assert {n: (tarea.slots.get(n) or {}).get("value") for n in ("alcance", "monto", "saldo_total", "vencido")} == {
            "alcance": "vencido", "monto": 300000.0, "saldo_total": 600000.0, "vencido": 300000.0,
        }
    finally:
        await _banderas(tid, False)


async def test_un_pago_entre_la_opcion_y_el_si_vuelve_a_las_opciones(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """El cliente eligió lo vencido y paga la cuota antes de confirmar: el resumen no se arma
    (monto esperado) y, al reconsultar, la tarea vuelve a `opciones` con la única opción vigente."""
    tid = await _negocio(["pack_cobranza"])
    viernes = _proximo_viernes().isoformat()
    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        prestamo_id = await _cliente_en_mora(tid, conv.customer_id)
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_PROMESA}),
            ("cob_opciones_promesa", {"eleccion": "vencido"}),
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            "¿Para qué fecha?",
        ])
        await _turno(conv, guion, monkeypatch, "te pago lo atrasado el viernes")
        await _pagar_cuota_vencida(tid, prestamo_id)

        guion = _Guion([("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "vencido"}), "¿Confirmas?"])
        resultado = await _turno(conv, guion, monkeypatch, "sí, anótala")
        assert resultado is not None and resultado.confirmacion is not None
        assert resultado.confirmacion.confirmacion.payload == ""   # sin resumen fiel: sin botón

        guion = _Guion([
            ("cob_opciones_promesa", {}),
            ("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 300000}),
            "¿Confirmas?",
        ])
        resultado = await _turno(conv, guion, monkeypatch, "¿y ahora cuánto?")
        tarea, _estado = await _tarea(conv, PROC_PROMESA)
        assert {n: (tarea.slots.get(n) or {}).get("value") for n in ("alcance", "monto", "vencido")} == {
            "alcance": "saldo_total", "monto": 300000.0, "vencido": 0.0,
        }
        assert resultado is not None and resultado.confirmacion is not None and "300.000" in resultado.text
        await _tocar_boton(conv, resultado)
        guion = _Guion([("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "saldo_total", "monto": 300000}),
                        "Listo, promesa registrada."])
        await _turno(conv, guion, monkeypatch, "Confirmar")
        assert await _promesas(tid) == [(viernes, "saldo_total", Decimal("300000"))]
    finally:
        await _banderas(tid, False)


async def test_negocio_que_solo_permite_un_alcance(packs_registrados) -> None:  # type: ignore[no-untyped-def]
    """La regla del negocio manda: con solo `vencido` no se ofrece ni se registra el saldo total,
    y con solo `saldo_total`, al revés. Con una sola opción, queda elegida."""
    solo_vencido = await _negocio(["pack_cobranza"])
    await _alcance_permitido(solo_vencido, "vencido")
    conv = await _conversacion(solo_vencido)
    await _cliente_en_mora(solo_vencido, conv.customer_id)
    r = await _opciones(solo_vencido, conv)
    assert r["opciones"] == [{"alcance": "vencido", "monto": 300000.0}]
    assert (r["alcance"], r["monto"]) == ("vencido", 300000.0)
    assert "error" in await _opciones(solo_vencido, conv, "saldo_total")
    assert "error" in await _registrar(solo_vencido, conv, "saldo_total", 600000)
    assert (await _registrar(solo_vencido, conv, "vencido", 300000)).get("registrada") is True

    solo_total = await _negocio(["pack_cobranza"])
    await _alcance_permitido(solo_total, "saldo_total")
    conv = await _conversacion(solo_total)
    await _cliente_en_mora(solo_total, conv.customer_id)
    r = await _opciones(solo_total, conv)
    assert r["opciones"] == [{"alcance": "saldo_total", "monto": 600000.0}] and r["alcance"] == "saldo_total"
    assert "error" in await _registrar(solo_total, conv, "vencido", 300000)
    assert await _promesas(solo_total) == []


async def test_sin_nada_vencido_hay_una_sola_opcion(packs_registrados) -> None:  # type: ignore[no-untyped-def]
    """Lo vencido = 0: solo el saldo total, ya elegido. Si el negocio solo acepta lo vencido, no hay
    promesa que registrar."""
    tid = await _negocio(["pack_cobranza"])
    conv = await _conversacion(tid)
    await _cliente_en_mora(tid, conv.customer_id, con_vencida=False)
    r = await _opciones(tid, conv)
    assert (r["vencido"], r["opciones"]) == (0.0, [{"alcance": "saldo_total", "monto": 600000.0}])
    assert (r["alcance"], r["monto"]) == ("saldo_total", 600000.0)
    assert "error" in await _opciones(tid, conv, "vencido")
    assert "error" in await _registrar(tid, conv, "vencido", 0)

    solo_vencido = await _negocio(["pack_cobranza"])
    await _alcance_permitido(solo_vencido, "vencido")
    conv = await _conversacion(solo_vencido)
    await _cliente_en_mora(solo_vencido, conv.customer_id, con_vencida=False)
    assert "cuotas vencidas" in (await _opciones(solo_vencido, conv))["error"]


async def test_promesa_con_monto_distinto_no_se_registra(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Monto esperado: un monto distinto del servidor no se registra (la tool), el resumen no se arma
    y, por el motor, el argumento ligado al dato de la tarea se niega."""
    from pack_cobranza.procedimientos import resumen_de_promesa

    from core.shared.tenant import tenant_scope

    tid = await _negocio(["pack_cobranza"])
    conv = await _conversacion(tid)
    await _cliente_en_mora(tid, conv.customer_id)
    r = await _registrar(tid, conv, "vencido", 100)
    assert "error" in r and r["monto"] == 300000.0
    r = await _registrar(tid, conv, "saldo_total", 300000)     # el monto de la otra opción
    assert "error" in r and r["monto"] == 600000.0
    assert await _promesas(tid) == []
    viernes = _proximo_viernes().isoformat()
    async with tenant_scope(tid):
        assert await resumen_de_promesa({"fecha": viernes, "alcance": "vencido", "monto": 100}, _ctx(conv)) is None
        lineas = await resumen_de_promesa({"fecha": viernes, "alcance": "vencido", "monto": 300000}, _ctx(conv))
    assert lineas is not None and [(d.nombre, d.valor, d.es_monto) for d in lineas] == [
        ("monto", 300000.0, True), ("alcance", "lo vencido", False), ("fecha", viernes, False),
    ]

    await _banderas(tid, True)
    try:
        conv = await _conversacion(tid)
        guion = _Guion([
            ("iniciar_procedimiento", {"procedure_id": PROC_PROMESA}),
            ("cob_opciones_promesa", {"eleccion": "vencido"}),
            ("actualizar_tarea", {"slots": {"fecha": viernes}}),
            ("cob_registrar_promesa_saldo", {"fecha": viernes, "alcance": "vencido", "monto": 100}),
            "Listo.",
        ])
        # Otro cliente del mismo negocio, con su propio préstamo.
        await _cliente_en_mora(tid, conv.customer_id)
        await _turno(conv, guion, monkeypatch, "te pago lo atrasado el viernes")
        assert "El valor de monto no coincide con el dato vigente" in guion.vistos[-1]
        assert await _promesas(tid) == []
    finally:
        await _banderas(tid, False)


async def test_alcances_y_montos_no_cruzan_negocios(packs_registrados) -> None:  # type: ignore[no-untyped-def]
    """La regla de A (solo lo vencido) no cambia las opciones de B, y el cliente de A no existe en B."""
    a = await _negocio(["pack_cobranza"])
    b = await _negocio(["pack_cobranza"])
    await _alcance_permitido(a, "vencido")
    conv_a, conv_b = await _conversacion(a), await _conversacion(b)
    await _cliente_en_mora(a, conv_a.customer_id)
    await _cliente_en_mora(b, conv_b.customer_id, con_vencida=False)
    assert [o["alcance"] for o in (await _opciones(a, conv_a))["opciones"]] == ["vencido"]
    assert [o["alcance"] for o in (await _opciones(b, conv_b))["opciones"]] == ["saldo_total"]
    # El cliente de A visto desde B: no hay préstamo (RLS + filtro por negocio).
    assert "préstamo vigente" in (await _opciones(b, conv_a))["error"]
    assert "préstamo vigente" in (await _registrar(b, conv_a, "vencido", 300000))["error"]
    from pack_cobranza.procedimientos import resumen_de_promesa

    from core.shared.tenant import tenant_scope

    args = {"fecha": _proximo_viernes().isoformat(), "alcance": "vencido", "monto": 300000}
    async with tenant_scope(b):
        assert await resumen_de_promesa(args, _ctx(conv_a)) is None
    async with tenant_scope(a):
        assert await resumen_de_promesa(args, _ctx(conv_a)) is not None
    assert (await _registrar(a, conv_a, "vencido", 300000)).get("registrada") is True
    assert await _promesas(b) == [] and len(await _promesas(a)) == 1


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


# ---------------------------------------------------------------- aislamiento entre negocios


async def test_resolvers_y_bandera_no_cruzan_negocios(packs_registrados, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Un id de otro negocio no se acepta y la bandera de un negocio no cambia la oferta de otro."""
    from pack_calendar.models import AppointmentType
    from pack_calendar.procedimientos import tipo_de_cita

    from core.shared.tenant import tenant_scope

    a = await _negocio(["pack_calendar", "pack_subscriptions"])
    b = await _negocio(["pack_calendar", "pack_subscriptions"])
    async with _scope(a) as s:
        tipo_a = AppointmentType(tenant_id=a, name="Demo", duration_minutes=60)
        s.add(tipo_a)
        await s.flush()
        tipo_a_id = tipo_a.id
    conv_b = await _conversacion(b)
    async with tenant_scope(b):
        ajeno = await tipo_de_cita(str(tipo_a_id), _ctx(conv_b), {})
    async with tenant_scope(a):
        propio = await tipo_de_cita(str(tipo_a_id), _ctx(conv_b), {})
    assert not ajeno.ok and propio.ok

    await _suscripcion_basic(b, conv_b.customer_id)
    await _banderas(a, True)
    try:
        guion = _Guion(["Hola, ¿en qué te ayudo?"])
        await _turno(conv_b, guion, monkeypatch, "hola")
    finally:
        await _banderas(a, False)
    assert "sub_cambiar_plan" in guion.tools[0]
