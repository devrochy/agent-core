"""Cuentas de Instagram del negocio: lo común a las dos rutas de conexión (HU-CHAN-04, ADR-0152).

Una cuenta puede pasar de Facebook Login a Instagram Login (o al revés). Cada ruta tiene su propia cuenta de Meta, así que
`ig_accounts` puede tener DOS filas del mismo `ig_user_id` en el negocio. Solo una sirve: la que apunta la conexión
canónica. La otra queda marcada `superseded` en `raw` (la tabla no tiene columna de estado; sin borrado físico) y deja de
listarse, de usarse para el diagnóstico y de refrescarse.

La conexión canónica se religa DESPUÉS de guardar el token de la ruta nueva: un alta que falla a la mitad no deja la cuenta
apuntando a una ruta sin token.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from core.db.models import ChannelConnection, IgAccount
from core.db.session import get_session
from core.gateway.conexion_canonica import asegurar_conexion_de_cuenta_ig
from core.gateway.meta_asset_claims import MetaAssetError, claim_messaging_assets
from core.gateway.meta_messenger_alta import MENSAJE_CHOQUE_MANUAL
from core.shared.logger import get_logger

log = get_logger(__name__)

def no_reemplazada() -> Any:
    """Condición SQL: la fila de la cuenta sigue vigente (no la reemplazó la otra ruta)."""
    return IgAccount.raw["superseded"].astext.is_distinct_from("true")


async def hay_conexion_manual(session: Any, tenant_id: uuid.UUID, ig_user_id: str) -> bool:
    """¿El negocio ya tiene una conexión MANUAL (con credenciales propias, sin cuenta de Meta) para este identificador?
    Respondería por ella, no por la Página o la cuenta: el alta se rechaza antes de escribir nada."""
    return (
        await session.execute(
            select(ChannelConnection.id).where(
                ChannelConnection.tenant_id == tenant_id,
                ChannelConnection.provider == "meta",
                ChannelConnection.channel == "instagram",
                ChannelConnection.external_id == ig_user_id,
                ChannelConnection.deleted_at.is_(None),
                ChannelConnection.meta_connection_id.is_(None),
            ).limit(1)
        )
    ).first() is not None


async def activar_ruta(
    tenant_id: uuid.UUID, ig_user_id: str, connection_id: uuid.UUID, *, nombre: str
) -> uuid.UUID:
    """Segundo paso del alta (con el token ya guardado): conexión canónica, instancias de agentes y la fila vigente.
    Toma otra vez el candado de la cuenta para serializar dos altas simultáneas del mismo negocio. Devuelve el id de la
    conexión canónica."""
    from core.agents.instancias import instancias_de_conexion_nueva

    async with get_session() as session:
        await claim_messaging_assets(session, tenant_id, ig_user_id=ig_user_id)  # el mismo candado; ya es del negocio
        canonica_id, resultado = await asegurar_conexion_de_cuenta_ig(
            session, tenant_id, ig_user_id, connection_id, nombre=nombre
        )
        if resultado == "choque_manual":  # alguien la creó entre el chequeo previo y ahora
            raise MetaAssetError(MENSAJE_CHOQUE_MANUAL, code="manual_connection_conflict")
        await instancias_de_conexion_nueva(session, tenant_id, canonica_id)
        filas = (
            await session.execute(
                select(IgAccount).where(IgAccount.tenant_id == tenant_id, IgAccount.ig_user_id == ig_user_id)
            )
        ).scalars().all()
        for fila in filas:
            raw = dict(fila.raw or {})
            if fila.connection_id == connection_id:
                raw.pop("superseded", None)
            else:
                raw["superseded"] = "true"
            fila.raw = raw
    return canonica_id
