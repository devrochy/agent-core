"""La capacidad de pagos no devuelve un link vencido y dice por qué rechaza (adenda 2026-10-10 de ADR-0067).

Al retirar `generar_link_pago` (#423), el vencimiento y el reemplazo quedaron en la tool y la
capacidad (`create_link`, el camino de `confirmar_pago_pedido`) devolvía el link pendiente
aunque ya hubiera vencido. Además, `confirmar_pago_pedido` contestaba `monto_bajo_minimo` a
cualquier rechazo: un pedido ya pagado o sin pasarela salía como "el total es muy bajo".
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pack_commerce.skills import _rechazo_del_link
from pack_payments import payments as capacidad
from pack_payments.models import PaymentTransaction
from pack_payments.providers.base import MontoBajoMinimo, PaymentLinkResult, PaymentRequestRejected

from core.payments import (
    EN_REVISION,
    MONTO_BAJO_MINIMO,
    MONTO_NO_COINCIDE,
    NO_DISPONIBLE,
    SIN_PASARELA,
    YA_PAGADO,
    PaymentCapabilityError,
)

AHORA = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _tx(
    *,
    status: str = "pendiente",
    amount: float = 200_000,
    horas: float = 1,
    url: str | None = "https://checkout.wompi.co/l/viejo",
    borrada: bool = False,
) -> PaymentTransaction:
    """Transacción creada hace `horas` horas (respecto de AHORA)."""
    return PaymentTransaction(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        reference="OC-0001",
        provider="wompi",
        provider_reference="viejo",
        status=status,
        amount=amount,
        currency="COP",
        payment_link_url=url,
        idempotency_key="k",
        deleted_at=AHORA if borrada else None,
        created_at=AHORA - timedelta(hours=horas),
    )


# ---- link_vencido


def test_un_link_reciente_no_esta_vencido() -> None:
    assert capacidad.link_vencido(_tx(horas=1), 24, ahora=AHORA) is False


def test_un_link_que_vence_en_minutos_se_trata_como_vencido() -> None:
    # Vence a las 24 h; a las 23 h 50 min ya está dentro del margen de 15 min.
    assert capacidad.link_vencido(_tx(horas=23 + 50 / 60), 24, ahora=AHORA) is True
    assert capacidad.link_vencido(_tx(horas=23.5), 24, ahora=AHORA) is False


def test_expirado_o_sin_url_esta_vencido() -> None:
    assert capacidad.link_vencido(_tx(status="expirado"), 24, ahora=AHORA) is True
    assert capacidad.link_vencido(_tx(url=None), 24, ahora=AHORA) is True


# ---- reglas de cobro


def test_una_referencia_pagada_no_se_vuelve_a_cobrar_aunque_la_aprobada_este_borrada() -> None:
    with pytest.raises(PaymentCapabilityError) as exc:
        capacidad._rechazar_si_no_cobrable("OC-0001", [_tx(status="aprobado", borrada=True), _tx()])
    assert exc.value.code == YA_PAGADO


def test_un_pago_en_revision_bloquea_otro_link() -> None:
    with pytest.raises(PaymentCapabilityError) as exc:
        capacidad._rechazar_si_no_cobrable("OC-0001", [_tx(status="monto_distinto", borrada=True)])
    assert exc.value.code == EN_REVISION


def test_monto_distinto_solo_bloquea_si_el_link_sigue_vigente(monkeypatch) -> None:
    monkeypatch.setattr(capacidad, "utcnow", lambda: AHORA)

    with pytest.raises(PaymentCapabilityError) as exc:
        capacidad._vigente("OC-0001", 150_000, _tx(horas=1), 24)
    assert exc.value.code == MONTO_NO_COINCIDE
    # Vencido por otro monto (p. ej. el saldo de una cuota tras un abono): se reemplaza.
    assert capacidad._vigente("OC-0001", 150_000, _tx(horas=30), 24) is None
    # Mismo monto con centavos de redondeo: es el mismo cobro.
    viva = _tx(horas=1, amount=200_000.004)
    assert capacidad._vigente("OC-0001", 200_000, viva, 24) is viva


def test_un_link_rechazado_y_vigente_se_reusa(monkeypatch) -> None:
    # Wompi admite otro intento en un link de un solo uso mientras no haya un pago aprobado.
    monkeypatch.setattr(capacidad, "utcnow", lambda: AHORA)
    viva = _tx(status="rechazado", horas=1)
    assert capacidad._vigente("OC-0001", 200_000, viva, 24) is viva


# ---- create_link sin base: qué pide a la pasarela y qué código devuelve


class _Proveedor:
    provider_name = "simulado"

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.llamadas = 0

    async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):  # type: ignore[no-untyped-def]
        self.llamadas += 1
        if self.error is not None:
            raise self.error
        return PaymentLinkResult(provider_reference="nuevo", payment_link_url="https://checkout.wompi.co/l/nuevo")


@pytest.fixture
def sin_base(monkeypatch):
    """Capacidad con transacciones, config y guardado falsos."""
    estado = SimpleNamespace(transacciones=[], proveedor=_Proveedor(), guardadas=[], config=True)
    monkeypatch.setattr(capacidad, "utcnow", lambda: AHORA)

    async def _transacciones(reference):  # type: ignore[no-untyped-def]
        return estado.transacciones

    async def _config():  # type: ignore[no-untyped-def]
        if not estado.config:
            raise RuntimeError("sin config")
        return SimpleNamespace(currency="COP", link_expira_horas=24)

    async def _guardar(reference, amount, currency, provider_name, nuevo, *, reemplaza):  # type: ignore[no-untyped-def]
        estado.guardadas.append(reemplaza)
        return PaymentTransaction(
            reference=reference, payment_link_url=nuevo.payment_link_url, status="pendiente", amount=amount
        )

    monkeypatch.setattr(capacidad, "_transacciones", _transacciones)
    monkeypatch.setattr(capacidad, "tenant_config", _config)
    monkeypatch.setattr(capacidad, "build_provider", lambda _c: estado.proveedor)
    monkeypatch.setattr(capacidad, "_guardar", _guardar)
    return estado


async def test_un_link_vencido_se_reemplaza_por_uno_nuevo(sin_base) -> None:
    vieja = _tx(horas=30)
    sin_base.transacciones = [vieja]

    link = await capacidad.WompiPaymentCapability().create_link(reference="OC-0001", amount=200_000)

    assert link.url == "https://checkout.wompi.co/l/nuevo"
    assert sin_base.proveedor.llamadas == 1
    assert sin_base.guardadas == [vieja]  # se reemplaza la que se leyó


async def test_un_link_vigente_se_reusa_sin_llamar_a_la_pasarela(sin_base) -> None:
    sin_base.transacciones = [_tx(horas=1)]

    link = await capacidad.WompiPaymentCapability().create_link(reference="OC-0001", amount=200_000)

    assert link.url == "https://checkout.wompi.co/l/viejo"
    assert sin_base.proveedor.llamadas == 0


@pytest.mark.parametrize(
    ("error", "codigo"),
    [
        (MontoBajoMinimo("El monto es menor al mínimo"), MONTO_BAJO_MINIMO),
        (PaymentRequestRejected("Wompi rechazó la creación del link: 401"), NO_DISPONIBLE),
    ],
)
async def test_los_rechazos_de_la_pasarela_llevan_su_codigo(sin_base, error, codigo) -> None:
    sin_base.proveedor = _Proveedor(error)

    with pytest.raises(PaymentCapabilityError) as exc:
        await capacidad.WompiPaymentCapability().create_link(reference="OC-0001", amount=200_000)

    assert exc.value.code == codigo
    assert sin_base.guardadas == []


async def test_sin_config_es_sin_pasarela(sin_base) -> None:
    sin_base.config = False

    with pytest.raises(PaymentCapabilityError) as exc:
        await capacidad.WompiPaymentCapability().create_link(reference="OC-0001", amount=200_000)

    assert exc.value.code == SIN_PASARELA


# ---- confirmar_pago_pedido: cada rechazo con su resultado


def test_commerce_solo_dice_monto_bajo_minimo_cuando_lo_es() -> None:
    assert _rechazo_del_link(MONTO_BAJO_MINIMO, "OC-0001")["error"] == "monto_bajo_minimo"
    for codigo in (SIN_PASARELA, YA_PAGADO, EN_REVISION, MONTO_NO_COINCIDE, NO_DISPONIBLE, None):
        assert _rechazo_del_link(codigo, "OC-0001")["error"] != "monto_bajo_minimo", codigo


def test_commerce_no_ofrece_otro_pago_si_ya_esta_pagado() -> None:
    resultado = _rechazo_del_link(YA_PAGADO, "OC-0001")

    assert resultado["error"] == "ya_pagado"
    assert "transferencia" not in resultado["hint"]
    assert "escalate" not in resultado


def test_commerce_escala_lo_que_necesita_una_persona() -> None:
    # El core escala con el flag (loop.py); no se le pide al modelo en un texto.
    assert _rechazo_del_link(EN_REVISION, "OC-0001")["escalate"] is True
    assert _rechazo_del_link(MONTO_NO_COINCIDE, "OC-0001")["escalate"] is True
    for codigo in (MONTO_BAJO_MINIMO, SIN_PASARELA, YA_PAGADO, NO_DISPONIBLE, None):
        assert "escalate" not in _rechazo_del_link(codigo, "OC-0001"), codigo


def test_commerce_no_importa_el_pack_de_pagos() -> None:
    """Invariante nº5: los códigos vienen del core."""
    raiz = Path(__file__).resolve().parents[1] / "packages/pack-commerce/pack_commerce"
    for archivo in raiz.rglob("*.py"):
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.ImportFrom) and nodo.module:
                assert not nodo.module.startswith("pack_payments"), archivo
            if isinstance(nodo, ast.Import):
                assert not any(a.name.startswith("pack_payments") for a in nodo.names), archivo
