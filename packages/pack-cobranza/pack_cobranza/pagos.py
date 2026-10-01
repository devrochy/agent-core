"""Cobro digital del pack-cobranza (ADR-0067).

- Genera el link/QR de una cuota **usando la capacidad del core** que aporta
  `pack_payments` (sin importar ese pack); si no está, cae a una **cuenta
  informativa** configurable en `CobranzaConfig`.
- Se **suscribe** a `ON_PAYMENT_CONFIRMED`: al confirmarse el pago, registra el
  `Pago` de la cuota (idempotente), marca la cuota pagada y mueve caja/saldo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from core.db.session import get_owned, get_session
from core.payments import PaymentCapabilityError, payment_registry, qr_png_data_uri
from core.shared.events import ON_PAYMENT_CONFIRMED, event_bus
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id, tenant_scope
from pack_cobranza.config import CobranzaConfig
from pack_cobranza.models import AuditoriaCartera, Caja, Cuota, LinkPago, Pago, Prestamo

log = get_logger(__name__)

PLUGIN_ID = "pack_cobranza"


async def capability_del_tenant():
    """Capacidad de pagos de un pack habilitado para el tenant (o `None`)."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.plugin_manager import plugin_manager

    if not plugin_manager.is_active(PLUGIN_ID):
        return None
    try:
        enabled = enabled_packs(await load_active_spec())
    except Exception:
        enabled = None
    return payment_registry.capability_for(enabled)


async def config_del_tenant() -> CobranzaConfig | None:
    from core.plugin_manager import plugin_manager

    config = await plugin_manager.load_config(PLUGIN_ID)
    return config if isinstance(config, CobranzaConfig) else None


def datos_informativos(cfg: CobranzaConfig, monto: float) -> dict[str, Any]:
    """Cuenta informativa (sin pasarela): datos + QR del alias o de la imagen."""
    alias = (cfg.pago_alias or "").strip()
    qr = (cfg.pago_qr_imagen_url or "").strip() or qr_png_data_uri(alias)
    return {
        "modo": "informativo",
        "monto": monto,
        "titular": cfg.pago_titular,
        "banco": cfg.pago_banco,
        "tipoCuenta": cfg.pago_tipo_cuenta,
        "numeroCuenta": cfg.pago_numero_cuenta,
        "alias": alias,
        "qrPng": qr,
        "nota": "Transfiere y registra el pago al confirmarlo.",
    }


async def _caja(session, tenant_id: uuid.UUID, cartera_id: uuid.UUID) -> Caja:
    caja = (await session.execute(select(Caja).where(Caja.tenant_id == tenant_id, Caja.cartera_id == cartera_id))).scalar_one_or_none()
    if caja is None:
        caja = Caja(tenant_id=tenant_id, cartera_id=cartera_id, saldo_inicial=Decimal(0), saldo_actual=Decimal(0))
        session.add(caja)
        await session.flush()
    return caja


async def _on_payment_confirmed(reference: str | None = None, amount: Any = None, **_: Any) -> None:
    """Registra el pago de la cuota cuando la pasarela confirma (idempotente)."""
    tenant_id = get_current_tenant_id()
    if tenant_id is None or not reference:
        return
    from core.agents.spec import enabled_packs, load_active_spec

    async with tenant_scope(tenant_id):
        # Guard de enablement: este handler está suscrito de forma global (event
        # bus), pero SOLO debe procesar pagos de tenants con cobranza activa.
        spec = await load_active_spec(tenant_id)
        if "pack_cobranza" not in enabled_packs(spec):
            return
        async with get_session() as session:
            link = (await session.execute(select(LinkPago).where(LinkPago.tenant_id == tenant_id, LinkPago.referencia == reference))).scalar_one_or_none()
            if link is None or link.cuota_id is None or link.estado == "usado":
                return
            cuota = await get_owned(session, Cuota, link.cuota_id)
            if cuota is None:
                return
            prestamo = await get_owned(session, Prestamo, cuota.prestamo_id)
            valor = Decimal(str(amount)) if amount is not None else Decimal(link.monto or 0)
            session.add(
                Pago(
                    tenant_id=tenant_id,
                    cliente_id=link.cliente_id or (prestamo.cliente_id if prestamo else uuid.uuid4()),
                    prestamo_id=cuota.prestamo_id,
                    cuota_id=cuota.id,
                    valor=valor,
                    metodo_pago="en_linea",
                    registrado_por="pasarela",
                    idempotency_key=f"link-{reference}",
                )
            )
            cuota.estatus = "pagada"
            cartera_id = prestamo.cartera_id if prestamo else None
            if prestamo is not None:
                prestamo.saldo = prestamo.saldo - valor
            if cartera_id is not None:
                caja = await _caja(session, tenant_id, cartera_id)
                caja.saldo_actual = caja.saldo_actual + valor
            link.estado = "usado"
            await session.flush()
            session.add(
                AuditoriaCartera(
                    tenant_id=tenant_id, entidad="pago", entidad_id=cuota.id, operacion="crear",
                    valores_despues={"valor": float(valor), "metodo": "en_linea", "referencia": reference},
                    actor_rol="pasarela", motivo="Pago en línea confirmado",
                )
            )
    log.info("cobranza_pago_en_linea", reference=reference)


def suscribir() -> None:
    """Suscribe el pack a las confirmaciones de pago del core (ADR-0067)."""
    event_bus.subscribe(ON_PAYMENT_CONFIRMED, _on_payment_confirmed)


async def crear_link_para_cuota(
    cuota: Cuota, cfg: CobranzaConfig, *, cliente_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Crea (o reutiliza) el link/QR de una cuota. Devuelve el payload para la app."""
    tid = get_current_tenant_id()
    async with get_session() as session:
        pagado = Decimal((await session.execute(select(func.coalesce(func.sum(Pago.valor), 0)).where(Pago.tenant_id == tid, Pago.cuota_id == cuota.id, Pago.deleted_at.is_(None)))).scalar_one() or 0)
    saldo = max(Decimal(cuota.valor_esperado) - pagado, Decimal(0))
    monto = float(saldo)
    cap = await capability_del_tenant()
    if cap is None:
        return datos_informativos(cfg, monto)

    reference = f"cob-{cuota.id}"
    link = await cap.create_link(reference=reference, amount=monto, description=f"Cuota {cuota.numero_cuota}")
    async with get_session() as session:
        existente = (await session.execute(select(LinkPago).where(LinkPago.tenant_id == tid, LinkPago.referencia == link.reference))).scalar_one_or_none()
        if existente is None:
            session.add(LinkPago(tenant_id=tid, cuota_id=cuota.id, prestamo_id=cuota.prestamo_id, cliente_id=cliente_id, referencia=link.reference, url=link.url or "", monto=saldo))
        elif link.url:
            existente.url = link.url
            if cliente_id is not None and existente.cliente_id is None:
                existente.cliente_id = cliente_id
    return {
        "modo": "online",
        "monto": monto,
        "referencia": link.reference,
        "url": link.url,
        "qrPng": qr_png_data_uri(link.qr_payload or link.url or ""),
        "metodos": [m.id for m in cap.methods()],
    }


__all__ = ["PaymentCapabilityError", "capability_del_tenant", "config_del_tenant", "crear_link_para_cuota", "datos_informativos", "suscribir"]
