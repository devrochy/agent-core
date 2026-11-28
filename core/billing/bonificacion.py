"""Abono de créditos de regalo: `credit_grants` de tipo `bonificacion` (ADR-0213 §1, HU-FB-02).

Lo inserta la aprobación de un reporte de feedback (`origen = 'feedback:<report_id>'`, HU-FB-04) en el ciclo
actual del negocio. No vence: el cierre lo arrastra como una recarga (`cierre_de_ciclo.cerrar_ciclo`) y se
gasta después de lo acumulado y antes de las recargas (`creditos.orden_de_gasto`).

`acreditar_bonificacion_unica` (HU-REF-02, ADR-0214) suma la defensa que los referidos necesitan: buscar el
`origen` en cualquier ciclo, porque la unicidad de `credit_grants` incluye `ciclo`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.billing.creditos import _exigir_scope
from core.billing.periodos import ventana_mensual
from core.shared.logger import get_logger

log = get_logger(__name__)

ResultadoDeLaBonificacion = Literal["acreditada", "ya_acreditada", "sin_ciclo"]


async def acreditar_bonificacion(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    origen: str,
    creditos: int,
    *,
    now: datetime | None = None,
) -> ResultadoDeLaBonificacion:
    """Inserta la bonificación en el ciclo actual del negocio, una sola vez, con el patrón de
    `checkout._acreditar_recarga`: `ON CONFLICT DO NOTHING` sobre `(tenant_id, ciclo, tipo, origen)`.

    Devuelve `acreditada`, `ya_acreditada` (el abono ya estaba en este ciclo) o `sin_ciclo` (la suscripción
    no tiene `period_start`, o no hay suscripción: no hay ciclo donde abonar; la aprobación responde 409
    `sin_ciclo_de_creditos`, ADR-0213 §1). Corre dentro del `tenant_scope` del negocio y en la transacción
    del llamador (no hace commit). No toma el lock del saldo: lo sostiene quien aprueba
    (`pg_advisory_xact_lock('billing:creditos:<tenant>')`, ADR-0213 §7), que además comprueba los topes.
    `creditos` es un entero mayor que 0; si no, `ValueError`."""
    from core.db.models import CreditGrant, TenantSubscription

    if isinstance(creditos, bool) or not isinstance(creditos, int) or creditos <= 0:
        raise ValueError("creditos debe ser un entero mayor que 0")
    if not origen or len(origen) > 200:
        raise ValueError("origen debe tener entre 1 y 200 caracteres")
    _exigir_scope(tenant_id)
    sub = (
        await session.execute(
            select(TenantSubscription.period_start, TenantSubscription.dia_ancla).where(
                TenantSubscription.tenant_id == tenant_id,
                TenantSubscription.deleted_at.is_(None),
            )
        )
    ).first()
    if sub is None or sub.period_start is None:
        log.warning("bonificacion_sin_ciclo", tenant_id=str(tenant_id), origen=origen)
        return "sin_ciclo"
    ciclo, _ = ventana_mensual(sub.period_start, sub.dia_ancla, now or datetime.now(UTC))
    resultado = await session.execute(
        pg_insert(CreditGrant)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            ciclo=ciclo,
            tipo="bonificacion",
            creditos=Decimal(creditos),
            vence_en=None,
            origen=origen,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "ciclo", "tipo", "origen"])
    )
    if not resultado.rowcount:
        return "ya_acreditada"
    log.info(
        "bonificacion_acreditada", tenant_id=str(tenant_id), origen=origen, creditos=creditos, ciclo=ciclo.isoformat()
    )
    return "acreditada"


async def acreditar_bonificacion_unica(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    origen: str,
    creditos: int,
    *,
    now: datetime | None = None,
) -> ResultadoDeLaBonificacion:
    """Como `acreditar_bonificacion`, pero el `origen` se abona una sola vez en CUALQUIER ciclo.

    La unicidad de `credit_grants` es `(tenant_id, ciclo, tipo, origen)`: un reintento que cae en otro ciclo
    (p. ej. tras `cerrar_ciclo`) insertaría un segundo abono, y uno ya gastado ni siquiera se arrastra. Aquí se
    busca primero cualquier `bonificacion` con ese `origen` y, si existe, se devuelve `ya_acreditada` sin
    insertar. Lo usan los referidos (`referral:<id>:referidor` / `referral:<id>:referido`, ADR-0214 §6).

    La búsqueda y el abono no son atómicos por sí solos: quien llama debe sostener
    `pg_advisory_xact_lock('billing:creditos:<tenant>')` (mismo candado que `creditos.py`, ADR-0213 §7) en la
    misma transacción. `credit_grants` es de solo inserción, así que la fila encontrada no desaparece. Misma
    firma, validaciones y scope que `acreditar_bonificacion`, que es quien inserta y no cambia."""
    from core.db.models import CreditGrant

    # Valida antes de consultar para no devolver `ya_acreditada` con argumentos que el abono rechazaría.
    if isinstance(creditos, bool) or not isinstance(creditos, int) or creditos <= 0:
        raise ValueError("creditos debe ser un entero mayor que 0")
    if not origen or len(origen) > 200:
        raise ValueError("origen debe tener entre 1 y 200 caracteres")
    _exigir_scope(tenant_id)
    existente = (
        await session.execute(
            select(CreditGrant.ciclo)
            .where(
                CreditGrant.tenant_id == tenant_id,
                CreditGrant.tipo == "bonificacion",
                CreditGrant.origen == origen,
            )
            .limit(1)
        )
    ).first()
    if existente is not None:
        log.info("bonificacion_ya_acreditada_en_otro_ciclo", tenant_id=str(tenant_id), origen=origen)
        return "ya_acreditada"
    return await acreditar_bonificacion(session, tenant_id, origen, creditos, now=now)


__all__ = ["ResultadoDeLaBonificacion", "acreditar_bonificacion", "acreditar_bonificacion_unica"]
