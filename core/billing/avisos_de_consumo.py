"""Avisos al dueño por el consumo del crédito del ciclo (HU-BILL-20, ADR-0208 §7, SPEC-38).

Al 80 %, al 100 % y al entrar al colchón, por el centro de notificaciones. Se disparan en el turno,
después de decidir (`ingress.politicas_del_turno`), con el saldo que la decisión ya midió: debajo del
80 % no hay ninguna lectura extra. Pasado un umbral, una lectura de `notifications` por su clave
idempotente (negocio, umbral e inicio del ciclo) evita volver a insertar en cada turno; la clave única
de `notifications` cubre la carrera entre dos turnos. El aviso sale en el primer turno que encuentra el
umbral cruzado, no al escribir la fila del ledger.

Mide aunque `BILLING_BLOQUEAR_POR_CREDITOS` esté apagado: los textos solo dicen cuánto se consumió.
Nunca lanza: un aviso que falla se registra y el turno sigue.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from core.shared.logger import get_logger

if TYPE_CHECKING:
    from core.billing.creditos import DecisionDelTurno

log = get_logger(__name__)

UMBRAL_AVISO_PREVIO = Decimal("0.8")


def umbrales_cruzados(decision: DecisionDelTurno) -> list[str]:
    """Umbrales del ciclo que el consumo ya cruzó, en orden (`80`, `100`, `colchon`)."""
    saldo = decision.saldo
    if not saldo.limite or saldo.inicio is None:
        return []
    limite = Decimal(saldo.limite)
    umbrales: list[str] = []
    if saldo.usado >= limite * UMBRAL_AVISO_PREVIO:
        umbrales.append("80")
    if saldo.usado >= limite:
        umbrales.append("100")
    if decision.salida == "colchon":
        umbrales.append("colchon")
    return umbrales


async def avisar_consumo(tenant_id: uuid.UUID, decision: DecisionDelTurno) -> None:
    """Emite los avisos de los umbrales cruzados que el negocio aún no tiene en este ciclo."""
    try:
        umbrales = umbrales_cruzados(decision)
        if not umbrales:
            return
        from sqlalchemy import select

        from core.db.models import Notification
        from core.db.session import get_session
        from core.notifications.catalog import consumo_de_credito, consumo_dedupe_key
        from core.notifications.service import notify_standalone
        from core.shared.tenant import tenant_scope

        ciclo = decision.saldo.inicio
        assert ciclo is not None
        claves = {consumo_dedupe_key(u, ciclo=ciclo): u for u in umbrales}
        async with tenant_scope(tenant_id):
            async with get_session() as session:
                # Sin filtrar `deleted_at`: un aviso que el dueño borró no vuelve a salir en el ciclo.
                ya = set(
                    (
                        await session.execute(
                            select(Notification.dedupe_key).where(
                                Notification.tenant_id == tenant_id,
                                Notification.dedupe_key.in_(list(claves)),
                            )
                        )
                    ).scalars()
                )
        for clave, umbral in claves.items():
            if clave in ya:
                continue
            log.info("billing_aviso_consumo", tenant_id=str(tenant_id), umbral=umbral)
            await notify_standalone(tenant_id, consumo_de_credito(umbral, ciclo=ciclo))
    except Exception as exc:
        log.warning("billing_aviso_consumo_fallo", tenant_id=str(tenant_id), error=type(exc).__name__)
