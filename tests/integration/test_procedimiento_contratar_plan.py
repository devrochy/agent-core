"""SPEC-43 · HU-SUB-29 · `pack_subscriptions.contratar_plan` por el motor de procedimientos, contra el stack real.

Modelo guionado (sin LLM real). Lo que se fija: el motor liga los datos al plan y a la cotización del servidor, el
botón muestra el total del SERVIDOR (no el del texto del modelo), la inscripción solo se crea después del botón y
el precio no sale del modelo.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from test_procedimientos_agenda_subs_cobranza import (
    _banderas,
    _conversacion,
    _Guion,
    _negocio,
    _scope,
    _tarea,
    _tocar_boton,
    _turno,
    packs_registrados,  # noqa: F401  (fixture del módulo hermano)
)
from test_subscription_promociones import CODIGO, _promo

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PROC = "pack_subscriptions.contratar_plan"


async def _plan(tid: uuid.UUID, codigo: str, precio: str) -> None:
    from pack_subscriptions.planes import DatosDePlan, crear

    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await crear(DatosDePlan(codigo=codigo, nombre=codigo.title(), precio=Decimal(precio), periodicidad="mensual"))


async def _inscripciones(tid: uuid.UUID, cliente: uuid.UUID) -> list:
    from pack_subscriptions.models import Subscription

    async with _scope(tid) as s:
        return list(
            (await s.execute(select(Subscription).where(Subscription.customer_id == cliente))).scalars().all()
        )


async def test_contratar_plan_con_codigo_muestra_el_total_del_servidor_y_no_inscribe_antes_del_boton(
    packs_registrados, monkeypatch  # noqa: F811
) -> None:
    tid = await _negocio(["pack_subscriptions"])
    await _banderas(tid, True)
    try:
        await _plan(tid, "PREMIUM", "100000")
        await _promo(tid, CODIGO, "20")
        conv = await _conversacion(tid)
        guion = _Guion(
            [
                ("iniciar_procedimiento", {"procedure_id": PROC}),
                ("actualizar_tarea", {"slots": {"plan_code": "premium", "codigo_promocion": CODIGO}}),
                ("sub_cotizar_inscripcion", {"plan_code": "PREMIUM", "codigo_promocion": CODIGO}),
                "Te cuesta $1 el primer mes, ¿te inscribo?",
            ]
        )
        resultado = await _turno(conv, guion, monkeypatch, "quiero el plan premium, tengo un código")
        tarea, _ = await _tarea(conv, PROC)
        assert (tarea.slots.get("plan_code") or {}).get("value") == "PREMIUM", tarea.slots
        assert float((tarea.slots.get("monto_a_pagar") or {}).get("value")) == 80000.0, tarea.slots
        assert all("sub_inscribir" not in t for t in guion.tools[:1])  # aún no se ofrece la inscripción
        assert await _inscripciones(tid, conv.customer_id) == []  # nada se crea antes de confirmar
        assert resultado is not None
    finally:
        await _banderas(tid, False)


async def test_recorrido_completo_cotiza_pide_datos_confirma_con_boton_e_inscribe(
    packs_registrados, monkeypatch  # noqa: F811
) -> None:
    tid = await _negocio(["pack_subscriptions"])
    await _banderas(tid, True)
    try:
        await _plan(tid, "PREMIUM", "100000")
        await _promo(tid, CODIGO, "20")
        conv = await _conversacion(tid)
        guion = _Guion(
            [
                ("iniciar_procedimiento", {"procedure_id": PROC}),
                ("actualizar_tarea", {"slots": {"plan_code": "premium", "codigo_promocion": CODIGO}}),
                ("sub_cotizar_inscripcion", {"plan_code": "PREMIUM", "codigo_promocion": CODIGO}),
                "Te cuesta $1, ¿me das tus datos?",
            ]
        )
        await _turno(conv, guion, monkeypatch, "quiero el plan premium, tengo un código")
        tarea, _ = await _tarea(conv, PROC)
        pedidos = {k[5:] for k, v in tarea.slots.items() if k.startswith("pide_") and (v or {}).get("value") is True}
        datos = {
            "nombre": "Ana Gómez", "documento": "10203040", "email": "ana@ejemplo.co", "telefono": "+573001112233",
            "negocio": "Taller Ana", "vertical": "servicios",
        }
        guion = _Guion(
            [
                ("actualizar_tarea", {"slots": {**{k: v for k, v in datos.items() if k in pedidos}, "payment_method": "transferencia"}}),  # sin pasarela en el stack: el resolver solo acepta transferencia
                ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 80000}),
                "Resumen listo.",
            ]
        )
        resultado = await _turno(conv, guion, monkeypatch, "mis datos: " + ", ".join(f"{k} {v}" for k, v in datos.items()))
        assert resultado is not None and resultado.confirmacion is not None
        assert "80.000" in resultado.text and "$1," not in resultado.text, resultado.text  # el total es el del servidor
        assert await _inscripciones(tid, conv.customer_id) == []  # nada se crea antes del botón

        await _tocar_boton(conv, resultado)
        guion = _Guion(
            [
                ("sub_confirmar_inscripcion", {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 80000}),
                ("sub_inscribir", {"plan_code": "PREMIUM", "codigo_promocion": CODIGO, "monto_a_pagar": 80000, "customer_data": {}}),
                "Listo, quedaste inscrito.",
            ]
        )
        await _turno(conv, guion, monkeypatch, "Confirmar")
        [sub] = await _inscripciones(tid, conv.customer_id)
        assert sub.estado == "pendiente_pago" and sub.customer_id == conv.customer_id
        tarea, _ = await _tarea(conv, PROC)
        assert (tarea.slots.get("subscription_id") or {}).get("value") == str(sub.id), tarea.slots
    finally:
        await _banderas(tid, False)


async def test_las_tools_de_inscripcion_solo_se_ofrecen_con_el_motor_activo(packs_registrados, monkeypatch) -> None:  # noqa: F811
    tid = await _negocio(["pack_subscriptions"])
    await _plan(tid, "PREMIUM", "100000")
    conv = await _conversacion(tid)
    guion = _Guion(["Hola"])
    await _turno(conv, guion, monkeypatch, "quiero un plan")
    assert not any(
        {"sub_inscribir", "sub_cotizar_inscripcion", "sub_confirmar_inscripcion", "sub_listar_planes"} & t
        for t in guion.tools
    )
