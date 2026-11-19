"""La capacidad de pagos reemplaza el link vencido y no deja dos transacciones vivas (adenda 2026-10-10 de ADR-0067).

Contra Postgres: el reemplazo borra la transacción vieja con un `update` condicionado al estado
leído, el índice único parcial `uq_pack_payments_tenant_reference_viva` sigue valiendo con dos
llamadas a la vez, y las referencias de otro negocio no cuentan (RLS + filtro por tenant).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from _api_helpers import uniq
from sqlalchemy import select, update

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
]


class _Proveedor:
    """Pasarela simulada; `durante` corre mientras se "crea" el link (otra llamada, un webhook)."""

    provider_name = "simulado"

    def __init__(self) -> None:
        self.creados = 0
        self.durante: Callable[[], Awaitable[None]] | None = None

    async def create_payment_link(self, *, reference, amount, currency, expires_in_hours):  # type: ignore[no-untyped-def]
        from pack_payments.providers.base import PaymentLinkResult

        self.creados += 1
        if self.durante is not None:
            await self.durante()
        n = f"{reference}-{self.creados}"
        return PaymentLinkResult(provider_reference=f"sim-{n}", payment_link_url=f"https://pagos.simulados/{n}")


@pytest.fixture()
def proveedor(monkeypatch):
    import pack_payments.payments as capacidad
    from pack_payments.config import PaymentsConfig

    falso = _Proveedor()
    config = PaymentsConfig.model_validate(
        {"public_key": "pub_test_x", "private_key": "prv_test_x", "events_secret": "test_x", "sandbox": True}
    )

    async def _config():  # type: ignore[no-untyped-def]
        return config

    monkeypatch.setattr(capacidad, "tenant_config", _config)
    monkeypatch.setattr(capacidad, "build_provider", lambda _config: falso)
    return falso


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    tid, _ = await onboard_tenant(name="Negocio", slug=uniq("lvenc"), packs=[], sembrar_ejemplos=False)
    return tid


async def _filas(reference: str):  # type: ignore[no-untyped-def]
    from pack_payments.models import PaymentTransaction

    from core.db.session import get_session

    async with get_session() as s:
        return (
            (
                await s.execute(
                    select(PaymentTransaction)
                    .where(PaymentTransaction.reference == reference)
                    .order_by(PaymentTransaction.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _envejecer(reference: str, horas: int = 30, **valores) -> None:  # type: ignore[no-untyped-def]
    """La transacción viva de la referencia se creó hace `horas` horas."""
    from pack_payments.models import PaymentTransaction, utcnow

    from core.db.session import get_session

    async with get_session() as s:
        await s.execute(
            update(PaymentTransaction)
            .where(PaymentTransaction.reference == reference, PaymentTransaction.deleted_at.is_(None))
            .values(created_at=utcnow() - timedelta(hours=horas), **valores)
        )


async def test_un_link_vencido_se_reemplaza_y_queda_una_sola_viva(proveedor) -> None:
    from pack_payments.payments import WompiPaymentCapability

    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    async with tenant_scope(await _negocio()):
        primero = await cap.create_link(reference="OC-0001", amount=200000)
        assert (await cap.create_link(reference="OC-0001", amount=200000)).url == primero.url
        await _envejecer("OC-0001")
        segundo = await cap.create_link(reference="OC-0001", amount=200000)
        filas = await _filas("OC-0001")

    assert segundo.url != primero.url
    assert proveedor.creados == 2
    vieja, nueva = filas
    assert vieja.deleted_at is not None and vieja.status == "expirado"
    assert nueva.deleted_at is None and nueva.status == "pendiente"
    assert nueva.payment_link_url == segundo.url


async def test_un_link_vencido_por_otro_monto_se_reemplaza(proveedor) -> None:
    """Saldo de una cuota después de un abono: la referencia no queda bloqueada."""
    from pack_payments.payments import WompiPaymentCapability

    from core.payments import MONTO_NO_COINCIDE, PaymentCapabilityError
    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    async with tenant_scope(await _negocio()):
        await cap.create_link(reference="cob-1", amount=200000)
        with pytest.raises(PaymentCapabilityError) as exc:
            await cap.create_link(reference="cob-1", amount=160000)
        assert exc.value.code == MONTO_NO_COINCIDE
        await _envejecer("cob-1")
        link = await cap.create_link(reference="cob-1", amount=160000)
        filas = await _filas("cob-1")

    assert [float(f.amount) for f in filas if f.deleted_at is None] == [160000.0]
    assert link.url == filas[-1].payment_link_url


async def test_ya_pagado_y_en_revision_bloquean_tambien_en_una_reemplazada(proveedor) -> None:
    from pack_payments.payments import WompiPaymentCapability

    from core.payments import EN_REVISION, YA_PAGADO, PaymentCapabilityError
    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    async with tenant_scope(await _negocio()):
        await cap.create_link(reference="OC-0002", amount=200000)
        await _envejecer("OC-0002")
        await cap.create_link(reference="OC-0002", amount=200000)  # reemplaza
        # Webhook tardío del link viejo: pagó por otro monto.
        from pack_payments.models import PaymentTransaction

        from core.db.session import get_session

        async with get_session() as s:
            await s.execute(
                update(PaymentTransaction)
                .where(PaymentTransaction.reference == "OC-0002", PaymentTransaction.deleted_at.is_not(None))
                .values(status="monto_distinto")
            )
        with pytest.raises(PaymentCapabilityError) as exc:
            await cap.create_link(reference="OC-0002", amount=200000)
        assert exc.value.code == EN_REVISION

        await cap.create_link(reference="OC-0003", amount=200000)
        await _envejecer("OC-0003", status="aprobado")
        with pytest.raises(PaymentCapabilityError) as exc:
            await cap.create_link(reference="OC-0003", amount=200000)
        assert exc.value.code == YA_PAGADO

    assert proveedor.creados == 3  # ninguno de los rechazos llamó a la pasarela


async def test_si_otra_llamada_ya_reemplazo_el_link_se_devuelve_el_suyo(proveedor) -> None:
    """El `update` condicionado no toca la viva que cambió desde que se leyó."""
    from pack_payments.payments import WompiPaymentCapability

    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    async with tenant_scope(await _negocio()):
        await cap.create_link(reference="OC-0004", amount=200000)
        await _envejecer("OC-0004")
        del_otro: list[str] = []

        async def _otra_llamada() -> None:
            proveedor.durante = None
            del_otro.append((await cap.create_link(reference="OC-0004", amount=200000)).url or "")

        proveedor.durante = _otra_llamada
        link = await cap.create_link(reference="OC-0004", amount=200000)
        filas = await _filas("OC-0004")

    assert link.url == del_otro[0]
    vivas = [f for f in filas if f.deleted_at is None]
    assert len(vivas) == 1 and vivas[0].payment_link_url == link.url
    assert proveedor.creados == 3  # el primero, el de esta llamada (quedó sin usar) y el de la otra


async def test_dos_primeras_llamadas_a_la_vez_no_revientan_contra_el_indice(proveedor) -> None:
    from pack_payments.payments import WompiPaymentCapability

    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    async with tenant_scope(await _negocio()):
        del_otro: list[str] = []

        async def _otra_llamada() -> None:
            proveedor.durante = None
            del_otro.append((await cap.create_link(reference="OC-0005", amount=200000)).url or "")

        proveedor.durante = _otra_llamada
        link = await cap.create_link(reference="OC-0005", amount=200000)
        filas = await _filas("OC-0005")

    assert link.url == del_otro[0]
    assert len(filas) == 1


async def test_las_transacciones_de_otro_negocio_no_cuentan(proveedor) -> None:
    from pack_payments.payments import WompiPaymentCapability

    from core.shared.tenant import tenant_scope

    cap = WompiPaymentCapability()
    otro, mio = await _negocio(), await _negocio()
    async with tenant_scope(otro):
        await cap.create_link(reference="OC-0001", amount=999000)
        await _envejecer("OC-0001", horas=1, status="aprobado")
    async with tenant_scope(mio):
        link = await cap.create_link(reference="OC-0001", amount=200000)
        filas = await _filas("OC-0001")

    assert link.url
    assert len(filas) == 1 and float(filas[0].amount) == 200000.0
    assert filas[0].tenant_id == mio
