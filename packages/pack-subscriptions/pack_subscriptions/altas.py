"""Fuente de los datos del alta automática desde una inscripción (SPEC-43, HU-SUB-30, ADR-0177).

Cuando un negocio vendedor activa la suscripción de un plan de la plataforma, el core da de alta
el negocio del comprador. Con SPEC-43 la inscripción vive en este pack: la referencia pública es
el `numero_publico` (`SUB-0001`), el plan de la plataforma sale del plan del catálogo y los datos
del comprador son los `datos_alta` que el servidor guardó al inscribir (nunca texto del LLM). Se
registra en `core.provisioning.fuentes` junto a la de commerce: el core pregunta a cada una hasta
que una conoce la referencia.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from core.db.session import get_session
from core.provisioning.fuentes import DatosDeAlta
from core.shared.tenant import get_current_tenant_id
from pack_subscriptions.models import Plan, Subscription

# Estados desde los que la inscripción ya cubre su plan (Free nace así; uno de pago, tras su primer pago).
_PAGADAS = ("trialing", "activa", "pausada", "vencida")


class DatosDeAltaDeLaInscripcion:
    provider_id = "pack_subscriptions"

    async def plan_de_inscripcion(self, plan_id: str) -> str | None:
        """Plan de la plataforma que vende un plan del catálogo (cambio de plan y cargo de período)."""
        tenant_id = get_current_tenant_id()
        try:
            pid = uuid.UUID(str(plan_id))
        except (TypeError, ValueError):
            return None
        if tenant_id is None:
            return None
        async with get_session() as session:
            # Sin filtrar `deleted_at`: borrar un plan no cambia lo que ya se vendió.
            plan = (
                await session.execute(select(Plan.plan_plataforma).where(Plan.tenant_id == tenant_id, Plan.id == pid))
            ).scalar_one_or_none()
        return str(plan or "").strip().lower() or None

    async def datos_de_alta(self, referencia: str, *, variant_id: str | None = None) -> DatosDeAlta | None:
        tenant_id = get_current_tenant_id()
        referencia = (referencia or "").strip()
        if tenant_id is None or not referencia:
            return None
        async with get_session() as session:
            sub = (
                await session.execute(
                    select(Subscription).where(
                        Subscription.tenant_id == tenant_id,
                        Subscription.numero_publico == referencia,
                        Subscription.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if sub is None or sub.plan_id is None:
                return None
            plataforma = (
                await session.execute(
                    select(Plan.plan_plataforma).where(Plan.tenant_id == tenant_id, Plan.id == sub.plan_id)
                )
            ).scalar_one_or_none()
        datos = dict(sub.datos_alta or {})
        return {
            "plan": str(plataforma or "").strip().lower() or None,
            "vertical": datos.get("vertical"),
            "negocio": datos.get("negocio"),
            "email": datos.get("email"),
            "telefono": datos.get("telefono"),
            "nombre": datos.get("nombre"),
            "customer_id": str(sub.customer_id),
            "pagado": sub.estado in _PAGADAS,
            "origen": sub.origen,
        }
