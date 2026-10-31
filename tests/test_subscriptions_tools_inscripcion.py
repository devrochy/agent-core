"""SPEC-43 · HU-SUB-29/34 · tools de inscripción del core con un proveedor falso (sin BD).

Lo que importa: el modelo no decide el cliente, el precio ni la periodicidad. El cliente sale de `ToolContext`,
el monto que manda se COMPARA con el del servidor (nunca se usa), y las tools solo las ofrece el motor.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import core.subscriptions.tools as tools_mod
from core.subscriptions import SubscriptionError

PLAN = {"plan_code": "PREMIUM", "plan_id": str(uuid.uuid4()), "nombre": "Premium", "precio": 100000.0,
        "moneda": "COP", "periodicidad": "mensual", "trial_dias": 0, "entitlements": {}, "descripcion": "x"}


class _Cap:
    """Proveedor falso: implementa `InscripcionCapability` y guarda lo que recibió."""

    provider_id = "pack_subscriptions"

    def __init__(self) -> None:
        self.cotizaciones: list[tuple[Any, ...]] = []
        self.inscripciones: list[dict[str, Any]] = []

    async def list_for_customer(self, customer_id):
        return []

    async def listar_planes(self, *, solo_activos: bool = True):
        return [PLAN]

    async def plan_por_codigo(self, codigo: str):
        return PLAN if codigo == "PREMIUM" else None

    async def cotizar_inscripcion(self, plan_code, codigo_promocion=None, *, customer_id=None):
        self.cotizaciones.append((plan_code, codigo_promocion, customer_id))
        if plan_code != "PREMIUM":
            raise SubscriptionError("Ese plan no existe o no está disponible.", code="plan_no_encontrado")
        descuento = 20000.0 if codigo_promocion else 0.0
        return {
            "plan_code": "PREMIUM", "plan": "Premium", "precio_lista": 100000.0, "descuento": descuento,
            "total_primer_cargo": 100000.0 - descuento, "precio_periodo_siguiente": 100000.0, "moneda": "COP",
            "periodicidad": "mensual", "trial_dias": 0, "requiere_pago": True,
            "datos_requeridos": ["nombre", "email"],
        }

    async def inscribir(self, **kwargs):
        self.inscripciones.append(kwargs)
        return {
            "subscription_id": str(uuid.uuid4()), "numero_publico": "SUB-1", "plan": "Premium", "estado": "pendiente_pago",
            "moneda": "COP", "inscripcion": "nueva",
            "cargo_inscripcion": {"monto": 80000.0, "link": "https://pago.test/x", "referencia": "r1"},
        }


@pytest.fixture()
def cap(monkeypatch):
    c = _Cap()

    async def _cap_del_negocio(tenant_id):
        return c

    monkeypatch.setattr(tools_mod, "capability_del_negocio", _cap_del_negocio)
    monkeypatch.setattr(tools_mod, "get_current_tenant_id", lambda: uuid.uuid4())

    async def _sin_token(ctx):
        return None

    monkeypatch.setattr(tools_mod, "_token_de_terminos_aceptados", _sin_token)
    return c


def _ctx(customer_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id=uuid.uuid4(), customer_id=customer_id or uuid.uuid4(),
        idempotency_key=str(uuid.uuid4()), channel="whatsapp",
    )


async def test_listar_planes_no_expone_ids_internos(cap) -> None:
    r = await tools_mod.SubscriptionSkill().listar_planes({}, _ctx())
    assert r["encontrado"] and "plan_id" not in r["planes"][0] and r["planes"][0]["plan_code"] == "PREMIUM"
    uno = await tools_mod.SubscriptionSkill().listar_planes({"plan_code": "PREMIUM"}, _ctx())
    assert uno["plan"]["nombre"] == "Premium"
    with pytest.raises(SubscriptionError) as e:
        await tools_mod.SubscriptionSkill().listar_planes({"plan_code": "NOPE"}, _ctx())
    assert e.value.code == "plan_no_encontrado"


async def test_cotizar_marca_lo_que_se_pide_y_pasa_el_cliente_de_la_conversacion(cap) -> None:
    cliente = uuid.uuid4()
    r = await tools_mod.SubscriptionSkill().cotizar_inscripcion(
        {"plan_code": "PREMIUM", "codigo_promocion": "LANZA"}, _ctx(cliente)
    )
    assert r["monto_a_pagar"] == 80000.0 and r["descuento"] == 20000.0 and r["precio_periodo_siguiente"] == 100000.0
    assert (r["pide_nombre"], r["pide_email"], r["pide_telefono"], r["pide_negocio"]) == (True, True, False, False)
    assert cap.cotizaciones == [("PREMIUM", "LANZA", cliente)]


async def test_confirmar_compara_el_monto_con_el_del_servidor(cap) -> None:
    skill = tools_mod.SubscriptionSkill()
    ok = await skill.confirmar_inscripcion({"plan_code": "PREMIUM", "codigo_promocion": "LANZA", "monto_a_pagar": 80000}, _ctx())
    assert ok["confirmada"] and ok["monto_a_pagar"] == 80000.0
    for malo in (1000, 100000, None, True, "mil"):
        with pytest.raises(SubscriptionError) as e:
            await skill.confirmar_inscripcion({"plan_code": "PREMIUM", "codigo_promocion": "LANZA", "monto_a_pagar": malo}, _ctx())
        assert e.value.code == "monto_distinto"


async def test_inscribir_toma_el_cliente_del_contexto_y_no_deja_fijar_precio_ni_periodicidad(cap) -> None:
    cliente = uuid.uuid4()
    r = await tools_mod.SubscriptionSkill().inscribir(
        {
            "plan_code": "PREMIUM", "monto_a_pagar": 80000, "payment_method": "pago_en_linea",
            "customer_id": str(uuid.uuid4()), "periodicidad": "anual", "precio": 1,  # el modelo intenta colarlos
            "customer_data": {"nombre": "Ana", "email": "ana@x.co", "telefono": "", "acepta_terminos": "tok"},
        },
        _ctx(cliente),
    )
    [llamada] = cap.inscripciones
    assert llamada["customer_id"] == cliente and llamada["origen"] == "agente"
    assert set(llamada) == {"customer_id", "plan_code", "datos_alta", "origen", "metodo_pago", "terminos",
                            "codigo_promocion", "monto_esperado"}
    assert llamada["datos_alta"] == {"nombre": "Ana", "email": "ana@x.co"}  # sin vacíos ni el token
    assert llamada["terminos"]["acepta_terminos"] == "tok" and llamada["terminos"]["canal"] == "whatsapp"
    assert llamada["monto_esperado"] == 80000
    assert r["link"] == "https://pago.test/x" and r["monto_a_pagar"] == 80000.0 and "https://pago.test/x" in r["_safe_append"]


async def test_inscribir_sin_monto_responde_monto_distinto(cap) -> None:
    with pytest.raises(SubscriptionError) as e:
        await tools_mod.SubscriptionSkill().inscribir({"plan_code": "PREMIUM", "customer_data": {}}, _ctx())
    assert e.value.code == "monto_distinto" and cap.inscripciones == []


def test_las_tools_de_inscripcion_son_solo_del_procedimiento() -> None:
    tools = {t.name: t for t in tools_mod.SubscriptionSkill().get_tools()}
    nuevas = {"sub_listar_planes", "sub_cotizar_inscripcion", "sub_confirmar_inscripcion", "sub_inscribir"}
    assert nuevas <= set(tools_mod.SOLO_PROCEDIMIENTO)
    assert not any(tools[n].always_available for n in nuevas)
    assert tools["sub_listar_planes"].solo_lectura is True
    # El LLM no puede mandar cliente, precio ni periodicidad: no están en el schema.
    props = tools["sub_inscribir"].parameters["properties"]
    assert not ({"customer_id", "precio", "periodicidad", "price"} & set(props))


async def test_el_cambio_de_plan_pasa_el_codigo_de_promocion_solo_si_el_cliente_lo_dio(monkeypatch) -> None:
    """HU-SUB-34: `codigo_promocion` viaja a `cotizar_cambio` y a `change_plan` únicamente cuando hay uno."""
    llamadas: list[tuple[str, dict[str, Any]]] = []

    class _CapCambio:
        async def cotizar_cambio(self, subscription_id, *, plan_id, plan, **extra):
            llamadas.append(("cotizar", extra))
            return {"cambio": "inmediato", "monto_a_pagar": 40000.0, "monto_bruto": 50000.0}

        async def change_plan(self, subscription_id, *, plan_id, plan, monto_esperado, **extra):
            llamadas.append(("aplicar", extra))
            return {"cambio": "inmediato", "cargo_cambio": {"monto": 40000.0}}

    sub = {"subscription_id": str(uuid.uuid4())}

    async def _cotizable(ctx, plan_code):
        return _CapCambio(), sub, "plan-1", PLAN

    monkeypatch.setattr(tools_mod, "_cambio_cotizable", _cotizable)
    skill = tools_mod.SubscriptionSkill()
    await skill.cotizar_cambio_plan({"plan_code": "PREMIUM"}, _ctx())
    cot = await skill.cotizar_cambio_plan({"plan_code": "PREMIUM", "codigo_promocion": "  LANZA "}, _ctx())
    await skill.aplicar_cambio_plan({"plan_code": "PREMIUM", "monto_a_pagar": 40000}, _ctx())
    await skill.aplicar_cambio_plan({"plan_code": "PREMIUM", "monto_a_pagar": 40000, "codigo_promocion": "LANZA"}, _ctx())
    assert llamadas == [
        ("cotizar", {}),
        ("cotizar", {"codigo_promocion": "LANZA"}),
        ("aplicar", {}),
        ("aplicar", {"codigo_promocion": "LANZA"}),
    ]
    assert cot["monto_a_pagar"] == 40000.0 and cot["monto_bruto"] == 50000.0
