"""Servicio de «Ver diferencias» (HU-AOS-27): carga las dos releases de la MISMA conexión, arma los
nombres legibles (agentes del negocio y habilidades del catálogo) y llama al diff puro
(`diff_legible.diferencias`)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from core.db.session import get_session
from core.releases.build import ConexionNoEncontrada
from core.releases.promocion import ReleaseNoEncontrada
from core.shared.tenant import get_current_tenant_id


async def de_la_conexion(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, release_id: uuid.UUID, contra: uuid.UUID | None = None
) -> dict[str, Any]:
    """El diff de `release_id` frente a `contra` (por defecto la activa). Las dos deben ser de la
    conexión: si no, `ReleaseNoEncontrada` (404). Sin activa ni `contra`: todo es alta."""
    from core.releases.cargar import cargar_release
    from core.releases.diff_legible import diferencias

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("diffs: fuera del tenant_scope del negocio")
    async with get_session() as session:
        activa = (
            await session.execute(
                text(
                    "SELECT active_release_id FROM channel_connections "
                    "WHERE tenant_id = :t AND id = :c AND deleted_at IS NULL"
                ),
                {"t": tenant_id, "c": connection_id},
            )
        ).first()
        if activa is None:
            raise ConexionNoEncontrada(str(connection_id))
        base_id = contra or activa[0]
        candidata = await cargar_release(session, tenant_id, release_id)
        if candidata is None or candidata.connection_id != connection_id:
            raise ReleaseNoEncontrada(str(release_id))
        base = None
        if base_id is not None and base_id != release_id:
            base = await cargar_release(session, tenant_id, base_id)
            if base is None or base.connection_id != connection_id:
                raise ReleaseNoEncontrada(str(base_id))
        agentes = (
            await session.execute(
                text("SELECT id, name FROM agents WHERE tenant_id = :t"), {"t": tenant_id}
            )
        ).all()
        packs = {
            str(s.get("pack_id"))
            for r in (candidata, base)
            if r is not None
            for s in r.manifest.get("skills") or []
            if isinstance(s, dict)
        }
        habilidades = (
            (
                await session.execute(
                    text("SELECT id, name FROM plugins WHERE id = ANY(:ids)"), {"ids": sorted(packs)}
                )
            ).all()
            if packs
            else []
        )
    return diferencias(
        candidata,
        base,
        nombres_de_agentes={str(i): str(n) for i, n in agentes if n},
        nombres_de_habilidades={str(i): str(n) for i, n in habilidades if n},
    )


__all__ = ["de_la_conexion"]
