"""Enrutador de eventos de Wompi para una cuenta compartida (riesgo R-05, HU-SH-04, ADR-0123).

Wompi admite UNA URL de eventos por cuenta y por ambiente (sandbox y producción; verificado en
docs.wompi.co, «Eventos»). Si la plataforma cobra los planes con la misma cuenta que usa el
pack de pagos del negocio que los vende (`solvershub`), los dos tipos de evento llegan a una
sola URL: `/webhooks/billing/wompi`.

El webhook de la plataforma verifica la firma con su secreto de eventos y, si el link no es de
un pago de la plataforma, se lo pasa al negocio configurado en `BILLING_WOMPI_EVENTOS_TENANT`.
El pack de pagos de ese negocio vuelve a verificar la firma con SU secreto y lo aplica igual
que su webhook propio. Sin la variable (cuentas separadas) no se reenvía nada.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.config import get_settings
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

IGNORADO = "ignored"


def negocio_con_cuenta_compartida() -> uuid.UUID | None:
    valor = (get_settings().billing_wompi_eventos_tenant or "").strip()
    if not valor:
        return None
    try:
        return uuid.UUID(valor)
    except ValueError:
        log.warning("billing_wompi_eventos_tenant_invalido", entrada=valor[:64])
        return None


async def reenviar_al_negocio(payload: dict[str, Any]) -> str:
    """Evento ya verificado con la firma de la plataforma cuyo link no es de un pago de la
    plataforma. Devuelve lo que hizo el pack de pagos del negocio, o `ignored`."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry
    from core.tenants import is_tenant_active

    negocio = negocio_con_cuenta_compartida()
    if negocio is None:
        log.info("billing_webhook_sin_pago_de_plataforma")
        return IGNORADO
    if not await is_tenant_active(negocio):
        log.warning("billing_webhook_reenvio_negocio_inactivo", tenant_id=str(negocio))
        return IGNORADO
    async with tenant_scope(negocio):
        capacidad = payment_registry.capability_for(enabled_packs(await load_active_spec(negocio)))
        procesar = getattr(capacidad, "procesar_evento", None) if capacidad is not None else None
        if procesar is None:
            log.warning("billing_webhook_reenvio_sin_soporte", tenant_id=str(negocio))
            return IGNORADO
        resultado = str(await procesar(payload))
    log.info("billing_webhook_reenviado", tenant_id=str(negocio), resultado=resultado)
    return resultado


__all__ = ["IGNORADO", "negocio_con_cuenta_compartida", "reenviar_al_negocio"]
