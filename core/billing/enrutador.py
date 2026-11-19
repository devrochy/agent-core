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
# Lo que devuelve el pack de pagos cuando el negocio no puede verificar el evento: con la
# cuenta compartida eso es una configuración mala (otro secreto, pack sin configurar).
_MAL_CONFIGURADO = frozenset({"sin_config", "firma_invalida"})


class ReenvioFallido(RuntimeError):
    """`BILLING_WOMPI_EVENTOS_TENANT` está puesto pero el reenvío no puede hacerse (UUID
    inválido, negocio inactivo, sin pack de pagos o con otro secreto). El webhook responde 5xx
    para que Wompi reintente mientras se corrige: un 200 perdería el pago."""


def negocio_con_cuenta_compartida() -> uuid.UUID | None:
    """UUID de `BILLING_WOMPI_EVENTOS_TENANT`, o None si está vacía (cuentas separadas).
    Lanza `ReenvioFallido` si no es un UUID."""
    valor = (get_settings().billing_wompi_eventos_tenant or "").strip()
    if not valor:
        return None
    try:
        return uuid.UUID(valor)
    except ValueError as exc:
        raise ReenvioFallido("BILLING_WOMPI_EVENTOS_TENANT no es un UUID") from exc


async def reenviar_al_negocio(payload: dict[str, Any]) -> str:
    """Evento ya verificado con la firma de la plataforma cuyo link no es de un pago de la
    plataforma. Devuelve lo que hizo el pack de pagos del negocio, o `ignored` si no hay cuenta
    compartida. No depende de la allowlist de vendedores: basta la variable y el negocio activo."""
    from core.agents.spec import enabled_packs, load_active_spec
    from core.payments import payment_registry, procesar_evento
    from core.tenants import is_tenant_active

    negocio = negocio_con_cuenta_compartida()
    if negocio is None:
        log.info("billing_webhook_sin_pago_de_plataforma")
        return IGNORADO
    if not await is_tenant_active(negocio):
        raise ReenvioFallido(f"el negocio {negocio} de BILLING_WOMPI_EVENTOS_TENANT no está activo")
    async with tenant_scope(negocio):
        capacidad = payment_registry.capability_for(enabled_packs(await load_active_spec(negocio)))
        resultado = await procesar_evento(capacidad, payload) if capacidad is not None else None
    if resultado is None:
        raise ReenvioFallido(f"el negocio {negocio} no tiene un pack de pagos que procese eventos")
    if resultado in _MAL_CONFIGURADO:
        raise ReenvioFallido(f"el pack de pagos del negocio {negocio} no pudo verificar el evento ({resultado})")
    log.info("billing_webhook_reenviado", tenant_id=str(negocio), resultado=resultado)
    return resultado


__all__ = ["IGNORADO", "ReenvioFallido", "negocio_con_cuenta_compartida", "reenviar_al_negocio"]
