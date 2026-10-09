"""HU-SH-04 (ADR-0123): piezas del cobro de la plataforma por dos canales, sin BD.

Los escenarios Gherkin contra la base están en `tests/integration/test_cobro_plataforma.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest


@pytest.fixture
def settings_limpios(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    from core.config import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# ------------------------------------------------------------------ aviso de renovación
def test_el_correo_de_la_renovacion_trae_la_referencia_el_monto_y_el_enlace() -> None:
    from core.billing.renovacion import asunto_y_cuerpo

    payload = {
        "referencia": "plat-acme-202611",
        "plan_id": "premium",
        "plan_nombre": "Premium",
        "monto": "600000",
        "periodo_inicio": datetime(2026, 11, 9, 9, tzinfo=UTC).isoformat(),
    }
    asunto, cuerpo = asunto_y_cuerpo(payload, "https://panel.test/plan")
    assert asunto == "Tu plan Premium de SolversHub vence el 09/11/2026"
    assert "plat-acme-202611" in cuerpo and "$600.000" in cuerpo
    assert "https://panel.test/plan" in cuerpo


# ------------------------------------------------------------------ R-05: enrutador
def test_sin_cuenta_compartida_no_se_reenvia_nada(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.billing.enrutador import negocio_con_cuenta_compartida

    settings_limpios.setenv("BILLING_WOMPI_EVENTOS_TENANT", "")
    assert negocio_con_cuenta_compartida() is None
    settings_limpios.setenv("BILLING_WOMPI_EVENTOS_TENANT", "solvershub")  # un slug no vale: solo UUID
    from core.config import get_settings

    get_settings.cache_clear()
    assert negocio_con_cuenta_compartida() is None
    vendedor = uuid.uuid4()
    settings_limpios.setenv("BILLING_WOMPI_EVENTOS_TENANT", f" {vendedor} ")
    get_settings.cache_clear()
    assert negocio_con_cuenta_compartida() == vendedor


async def test_el_reenvio_sin_variable_ignora_el_evento(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.billing.enrutador import IGNORADO, reenviar_al_negocio

    settings_limpios.setenv("BILLING_WOMPI_EVENTOS_TENANT", "")
    assert await reenviar_al_negocio({"data": {"transaction": {"payment_link_id": "x"}}}) == IGNORADO


# ------------------------------------------------------------------ respuestas del panel
def test_ya_pagado_dice_la_referencia_el_canal_y_hasta_cuando() -> None:
    from core.billing.checkout import CambioDePlanEnElAsistente, PeriodoYaPagado

    exc = PeriodoYaPagado("plat-acme-202611", "asistente", hasta=datetime(2026, 12, 9, tzinfo=UTC))
    assert exc.code == "ya_pagado"
    assert "plat-acme-202611" in str(exc) and "por el asistente" in str(exc) and "09/12/2026" in str(exc)
    assert CambioDePlanEnElAsistente().code == "cambio_en_el_asistente"


# ------------------------------------------------------------------ eventos y handlers
def test_los_eventos_de_los_dos_canales_tienen_su_handler_del_core() -> None:
    from core.events.core_handlers import _HANDLERS
    from core.shared.events import (
        ON_PLATFORM_PERIOD_PAID,
        ON_PLATFORM_RENEWAL_DUE,
        ON_SUBSCRIPTION_CHARGE_PAID,
    )

    topics = {topic for topic, _ in _HANDLERS}
    assert {ON_SUBSCRIPTION_CHARGE_PAID, ON_PLATFORM_PERIOD_PAID, ON_PLATFORM_RENEWAL_DUE} <= topics


# ------------------------------------------------------------------ pack: reactivación por pago
def _suscripcion(**campos: object):  # type: ignore[no-untyped-def]
    from pack_subscriptions.models import Subscription

    datos = {
        "estado": "vencida",
        "periodicidad": "mensual",
        "dia_ancla": 31,
        "valor_periodo": Decimal("600000"),
        "periodo_actual_inicio": datetime(2026, 9, 30, 9, tzinfo=UTC),
        "proximo_cargo": datetime(2026, 10, 31, 9, tzinfo=UTC),
    }
    datos.update(campos)
    return Subscription(**datos)


def _cargo(inicio: datetime):  # type: ignore[no-untyped-def]
    from pack_subscriptions.models import SubscriptionCharge

    return SubscriptionCharge(periodo=inicio.strftime("%Y-%m"), estado="pagado", fecha_vencimiento=inicio)


def test_un_pago_reactiva_y_avanza_el_calendario_del_asistente() -> None:
    from pack_subscriptions.capability import reactivar_por_pago

    sub = _suscripcion()
    reactivar_por_pago(sub, _cargo(datetime(2026, 10, 31, 9, tzinfo=UTC)))
    assert sub.estado == "activa"
    assert sub.proximo_cargo == datetime(2026, 11, 30, 9, tzinfo=UTC)  # 31 → último día de noviembre


def test_si_el_otro_canal_corto_distinto_manda_su_corte() -> None:
    """El panel arrancó un período nuevo hoy (vencido hace mucho): el asistente se alinea."""
    from pack_subscriptions.capability import reactivar_por_pago

    sub = _suscripcion()
    inicio, fin = datetime(2027, 1, 20, 9, tzinfo=UTC), datetime(2027, 2, 20, 9, tzinfo=UTC)
    reactivar_por_pago(sub, _cargo(inicio), fin=fin)
    assert (sub.periodo_actual_inicio, sub.proximo_cargo, sub.dia_ancla) == (inicio, fin, 20)


def test_una_pausa_que_pidio_el_cliente_no_se_reactiva_con_un_pago() -> None:
    from pack_subscriptions.capability import reactivar_por_pago

    sub = _suscripcion(estado="pausada", motivo_pausa="cliente")
    reactivar_por_pago(sub, _cargo(datetime(2026, 10, 31, 9, tzinfo=UTC)))
    assert sub.estado == "pausada"
