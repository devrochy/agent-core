"""Creación del cargo de un período (SPEC-43, HU-SUB-35).

`crear_cargo_de_periodo` es el ÚNICO lugar que crea un cargo `tipo='periodo'`: lo usan el cron de cobros, el
link de pago, el registro de un pago externo y cualquier otro camino. Así el descuento de N períodos se
consume en un solo sitio y una sola vez por período:

- el cargo y el decremento del descuento van en la MISMA transacción (si algo falla se deshace todo);
- el cargo existente del período se reutiliza (idempotencia), y el único parcial
  `uq_pack_subscriptions_charges_periodo` es la red de seguridad si dos transacciones llegaran a la vez;
- quien llama tiene la suscripción bloqueada (`FOR UPDATE`): orden de locks suscripción → cargo → canje.

No importa `capability` ni `inscripcion` (evita ciclos): solo modelos, períodos y promociones.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core.shared.logger import get_logger
from pack_subscriptions import promociones
from pack_subscriptions.models import Subscription, SubscriptionCharge
from pack_subscriptions.periodos import etiqueta_periodo

log = get_logger(__name__)


async def crear_cargo_de_periodo(
    session,  # type: ignore[no-untyped-def]
    sub: Subscription,
    inicio: datetime,
    *,
    estado: str = "pendiente",
    pago_externo: dict[str, Any] | None = None,
    descontar: bool = True,
    ignorar_anulados: bool = False,
) -> tuple[SubscriptionCharge, bool]:
    """Cargo del período que empieza en `inicio`. Devuelve `(cargo, creado)`.

    - `descontar`: aplica el descuento vigente de la suscripción y consume un período. Un pago por otro canal
      (`pago_externo`) se crea a precio de lista y NO consume: lo cobró otro y el descuento es de este cobro.
    - `ignorar_anulados`: un cargo anulado del período no cuenta como existente (el link de pago puede crear
      otro; el cron, no, para no volver a cobrar un período que se anuló a propósito).
    Si el cargo del período ya existía se devuelve tal cual, sin consumir nada."""
    periodo = etiqueta_periodo(inicio, sub.periodicidad)
    filtros = [
        SubscriptionCharge.tenant_id == sub.tenant_id,
        SubscriptionCharge.subscription_id == sub.id,
        SubscriptionCharge.periodo == periodo,
        SubscriptionCharge.tipo == "periodo",
        SubscriptionCharge.deleted_at.is_(None),
    ]
    if ignorar_anulados:
        filtros.append(SubscriptionCharge.estado != "anulado")
    existente = (await session.execute(select(SubscriptionCharge).where(*filtros))).scalars().first()
    if existente is not None:
        return existente, False
    cargo = SubscriptionCharge(
        tenant_id=sub.tenant_id,
        subscription_id=sub.id,
        periodo=periodo,
        monto=Decimal(sub.valor_periodo or 0),
        estado=estado,
        fecha_vencimiento=inicio,
        pago_externo=pago_externo,
    )
    try:
        # Savepoint: el consumo del descuento y el INSERT se deshacen juntos si el único salta.
        async with session.begin_nested():
            if descontar:
                await promociones.aplicar_descuento_vigente(session, sub, cargo)
            session.add(cargo)
            await session.flush()
    except IntegrityError as exc:
        if "uq_pack_subscriptions_charges_periodo" not in str(exc):
            raise
        await session.refresh(sub)  # el savepoint deshizo el consumo en la base; la memoria se resincroniza
        ganador = (await session.execute(select(SubscriptionCharge).where(*filtros))).scalars().first()
        if ganador is None:
            raise
        log.info("subs_cargo_de_periodo_ya_existia", subscription_id=str(sub.id), periodo=periodo)
        return ganador, False
    return cargo, True
