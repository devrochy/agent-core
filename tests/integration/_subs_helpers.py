"""Ayudas de los tests de `pack_subscriptions` que arman suscripciones a mano (SPEC-43).

Desde la contracción del contrato v1 (HU-COM-17) toda suscripción apunta a un plan del catálogo
(`plan_id`) y lleva su número público: estos helpers lo completan para los tests que insertan filas
directamente en vez de inscribir.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pack_subscriptions import planes
from pack_subscriptions.inscripcion import _siguiente_numero
from pack_subscriptions.models import Subscription

CODIGO_POR_DEFECTO = "PLAN-DE-PRUEBA"


async def plan_por_defecto(tid: uuid.UUID, *, precio: int = 200_000, codigo: str = CODIGO_POR_DEFECTO) -> uuid.UUID:
    """Id de un plan del catálogo del negocio (se crea la primera vez)."""
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        existente = await planes.por_codigo(codigo, solo_activos=False)
        if existente is None:
            existente = await planes.crear(
                planes.DatosDePlan(
                    codigo=codigo, nombre=f"Plan {codigo}", precio=Decimal(precio), periodicidad="mensual"
                )
            )
        return existente.id


async def nueva_sub(session: Any, tid: uuid.UUID, **campos: Any) -> Subscription:
    """`Subscription` agregada a la sesión, con plan del catálogo y número público."""
    plan_id = campos.pop("plan_id", None) or await plan_por_defecto(tid)
    numero = campos.pop("numero_publico", None) or await _siguiente_numero(session, "SUB")
    sub = Subscription(tenant_id=tid, plan_id=plan_id, numero_publico=numero, **campos)
    session.add(sub)
    return sub
