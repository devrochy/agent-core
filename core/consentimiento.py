"""Consentimiento para el tratamiento de datos del cliente (HU-CRM-16, ADR-0179 decisión 8).

Sobre `customer_consents` (solo inserción: la última fila por cliente y propósito manda). El aviso se
le muestra al cliente en el chat; su respuesta («acepto» / «no») queda como fila `otorgado` /
`revocado` con la versión del aviso y el mensaje como evidencia. La fila `informado` solo dice «ya se
le mostró esta versión»: NO es un consentimiento y `vigente` la ignora.

Estados de una persona frente a la versión vigente del aviso:
- `sin_aviso`: nunca se le mostró (o se le mostró una versión anterior sin respuesta).
- `informado`: se le mostró y no ha respondido.
- `otorgado`: aceptó esta versión.
- `revocado`: dijo que no, o retiró lo aceptado (vale para cualquier versión: no se le insiste).

Este módulo no afirma cumplimiento de ninguna ley: la validez del texto del aviso depende de la
revisión legal del negocio (P-37).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import CustomerConsent
from core.shared.logger import get_logger

log = get_logger(__name__)

PROPOSITO = "tratamiento_datos"
SIN_AVISO, INFORMADO, OTORGADO, REVOCADO = "sin_aviso", "informado", "otorgado", "revocado"
ORIGEN_CHAT = "aviso_en_chat"
ORIGEN_FICHA = "ficha"


async def ultima_fila(session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> CustomerConsent | None:
    return (
        await session.execute(
            select(CustomerConsent)
            .where(
                CustomerConsent.tenant_id == tenant_id,
                CustomerConsent.customer_id == customer_id,
                CustomerConsent.proposito == PROPOSITO,
            )
            .order_by(CustomerConsent.created_at.desc(), CustomerConsent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def estado_de(fila: CustomerConsent | None, version: str) -> str:
    """Estado frente a la versión vigente del aviso (ver el módulo)."""
    if fila is None:
        return SIN_AVISO
    if fila.estado == REVOCADO:
        return REVOCADO
    if fila.version_aviso != version:
        return SIN_AVISO  # una versión nueva del texto se vuelve a mostrar
    return OTORGADO if fila.estado == OTORGADO else INFORMADO


async def estado_aviso(session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, version: str) -> str:
    return estado_de(await ultima_fila(session, tenant_id, customer_id), version)


async def vigente(session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID, version: str) -> bool:
    """¿Aceptó la versión vigente del aviso? `informado` no cuenta."""
    return await estado_aviso(session, tenant_id, customer_id, version) == OTORGADO


async def registrar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    estado: str,
    version: str,
    origen: str,
    evidencia: str = "",
    actor_admin_id: uuid.UUID | None = None,
    actor_label: str = "",
) -> CustomerConsent:
    """Agrega una fila (nunca edita) en la sesión de quien llama; no hace commit."""
    if estado not in (INFORMADO, OTORGADO, REVOCADO):
        raise ValueError(f"estado inválido: {estado!r}")
    fila = CustomerConsent(
        tenant_id=tenant_id,
        customer_id=customer_id,
        proposito=PROPOSITO,
        estado=estado,
        version_aviso=version,
        origen=origen,
        evidencia=evidencia[:500],
        actor_admin_id=actor_admin_id,
        actor_label=actor_label,
    )
    session.add(fila)
    await session.flush()
    log.info("consentimiento_datos_registrado", customer_id=str(customer_id), estado=estado, origen=origen)
    return fila


def como_dict(fila: CustomerConsent) -> dict[str, Any]:
    return {
        "estado": fila.estado,
        "origen": fila.origen,
        "version_aviso": fila.version_aviso,
        "evidencia": fila.evidencia,
        "quien": fila.actor_label or ("el cliente" if fila.origen == ORIGEN_CHAT else "el equipo"),
        "created_at": fila.created_at,
    }
