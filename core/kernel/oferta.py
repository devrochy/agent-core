"""Predicados de oferta de tools según el motor de procedimientos (HU-AOS-08, ADR-0112).

Se componen con el `connection_predicate` de una tool (lo evalúa el loop por turno, antes de armar
las candidatas del motor):

- `sin_motor_activo`: la tool de siempre que el procedimiento reemplaza (`sub_cambiar_plan`,
  `cob_registrar_promesa_pago`) no se ofrece con `tenants.aos_procedures` encendida: el modelo no
  puede saltarse el botón con ella. Si la bandera no se puede leer, se ofrece como siempre (con la
  bandera apagada nada cambia; adenda HU-AOS-08 de ADR-0113).
- `solo_con_motor_activo`: una acción que solo existe para un procedimiento no se ofrece sin la
  bandera, aunque un flujo del negocio la nombre. Si la bandera no se puede leer, no se ofrece.

Leen la bandera del negocio del contexto (`get_current_tenant_id()`) en cada evaluación.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from core.shared.logger import get_logger

log = get_logger(__name__)

Predicado = Callable[[set[str]], Awaitable[bool]]


async def _siempre(_tipos: set[str]) -> bool:
    return True


async def motor_activo() -> bool | None:
    """¿El negocio del contexto tiene `aos_procedures`? None si no se pudo leer o no hay negocio."""
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    try:
        from core.db.session import get_session
        from core.kernel.banderas import banderas_del_negocio

        async with get_session() as session:
            return (await banderas_del_negocio(session, tenant_id)).procedimientos
    except Exception:
        log.warning("oferta_bandera_ilegible", exc_info=True)
        return None


def sin_motor_activo(predicado: Predicado | None = None) -> Predicado:
    base = predicado or _siempre

    async def _predicado(tipos: set[str]) -> bool:
        if not await base(tipos):
            return False
        return await motor_activo() is not True

    return _predicado


def solo_con_motor_activo(predicado: Predicado | None = None) -> Predicado:
    base = predicado or _siempre

    async def _predicado(tipos: set[str]) -> bool:
        if not await base(tipos):
            return False
        return await motor_activo() is True

    return _predicado
