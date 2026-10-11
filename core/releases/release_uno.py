"""Release 1 por conexión conversacional (ADR-0117 §2 "Migración", HU-AOS-14).

`agent-core db backfill-releases`: por negocio, en su `tenant_scope`:

1. Refresca el Coordinador y el General desde el AgentSpec v2 activo **una vez** (corre el
   backfill B2 de HU-AOS-13, idempotente, y publica la conversión actual si cambió; ver
   `core/releases/build.py`). Un negocio que no pasa el backfill queda con error y no se toca.
2. Por cada conexión conversacional viva **sin ninguna release**: construye la release 1 desde
   esas versiones, los artefactos del disco y los campos de `AppSettings` del prompt, y la
   promueve (`reason = 'release_1'`, actor `system:backfill`). Las que ya tienen una release no
   se tocan: así una re-corrida cubre solo las conexiones nuevas (idempotente).

Sin cambio de conducta: con `tenants.aos_runtime = 'legacy'` el runtime ignora `release_id` y
lee de `agent_specs` y del disco, con el rollback de ADR-0051 §1. La release solo se lee con
`v3`. Con `dry_run` no se escribe nada: se cuentan las conexiones que recibirían su release 1.

Exige `pack_versions` al día (`agent-core db sync-packs`, que corre en el arranque del `api`).
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from core.db.session import get_session
from core.releases.build import ErrorDelBuild, construir, refrescar_desde_v2
from core.releases.promocion import promover
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

ACTOR = "system:backfill"


@dataclass
class Problema:
    tenant_id: uuid.UUID
    connection_id: uuid.UUID | None
    mensaje: str


@dataclass
class ReporteReleases:
    dry_run: bool
    negocios: int = 0
    conexiones: Counter[str] = field(default_factory=Counter)
    errores: list[Problema] = field(default_factory=list)

    @property
    def fallo(self) -> bool:
        return bool(self.errores)


async def _conexiones(tenant_id: uuid.UUID) -> list[tuple[uuid.UUID, bool]]:
    """(conexión conversacional viva, ¿ya tiene alguna release?)."""
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT cc.id, EXISTS (SELECT 1 FROM connection_releases r "
                    "  WHERE r.tenant_id = cc.tenant_id AND r.connection_id = cc.id) "
                    "FROM channel_connections cc JOIN connection_types ct ON ct.id = cc.type_id "
                    "WHERE cc.tenant_id = :t AND cc.deleted_at IS NULL AND ct.produces_conversations "
                    "ORDER BY cc.id"
                ),
                {"t": tenant_id},
            )
        ).all()
    return [(f[0], bool(f[1])) for f in filas]


def _mensaje(exc: Exception) -> str:
    if isinstance(exc, ErrorDelBuild):
        return f"{exc.codigo}: {exc}" + (f" ({'; '.join(exc.detalle[:5])})" if exc.detalle else "")
    if isinstance(exc, DBAPIError):
        # Sin `str(exc)`: SQLAlchemy imprime los parámetros (el manifiesto).
        return f"error de base de datos ({type(exc.orig).__name__})"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


async def _negocio(tenant_id: uuid.UUID, reporte: ReporteReleases, modulos: list[Any]) -> None:
    todas = await _conexiones(tenant_id)
    pendientes = [cid for cid, tiene in todas if not tiene]
    reporte.conexiones["ya_estaba"] += len(todas) - len(pendientes)
    if not pendientes:
        return
    if reporte.dry_run:
        reporte.conexiones["por_construir"] += len(pendientes)
        return
    try:
        fijadas = await refrescar_desde_v2(tenant_id, modulos)
    except (ErrorDelBuild, DBAPIError) as exc:
        reporte.errores.append(Problema(tenant_id, None, _mensaje(exc)[:500]))
        return
    for cid in pendientes:
        try:
            resultado = await construir(
                tenant_id,
                cid,
                actor=ACTOR,
                modules=modulos,
                fijadas=fijadas,
                notas="Release 1 (backfill HU-AOS-14).",
            )
            await promover(tenant_id, cid, resultado.release_id, actor=ACTOR, motivo="release_1")
        except (ErrorDelBuild, DBAPIError, LookupError, RuntimeError) as exc:
            reporte.errores.append(Problema(tenant_id, cid, _mensaje(exc)[:500]))
            log.warning(
                "backfill_releases_error",
                tenant_id=str(tenant_id),
                connection_id=str(cid),
                error=_mensaje(exc)[:300],
            )
            continue
        reporte.conexiones["construida"] += 1


async def backfill_releases(
    *, dry_run: bool, tenant_id: uuid.UUID | None = None, modules: Iterable[Any] | None = None
) -> ReporteReleases:
    from core.db.models import Tenant
    from core.plugin_manager import plugin_manager

    modulos = list(modules if modules is not None else plugin_manager.list_modules())
    reporte = ReporteReleases(dry_run=dry_run)
    if tenant_id is not None:
        negocios = [tenant_id]
    else:
        from sqlalchemy import select

        async with get_session() as session:
            negocios = list(
                (
                    await session.execute(
                        select(Tenant.id).where(Tenant.deleted_at.is_(None)).order_by(Tenant.id)
                    )
                )
                .scalars()
                .all()
            )
    for negocio in negocios:
        reporte.negocios += 1
        async with tenant_scope(negocio):
            await _negocio(negocio, reporte, modulos)
    log.info(
        "backfill_releases",
        dry_run=dry_run,
        negocios=reporte.negocios,
        conexiones=dict(reporte.conexiones),
        errores=len(reporte.errores),
    )
    return reporte


__all__ = ["ACTOR", "Problema", "ReporteReleases", "backfill_releases"]
