"""Operación del enrutamiento (HU-AOS-17, ADR-0138): la bandera `tenants.aos_routing` y el reporte
del modo sombra. Solo el master (CLI `agent-core db ruteo` y `db reporte-ruteo`). Sin LLM.

**Reporte** (precisión y recall por procedimiento frente a lo real): por cada turno enrutado con
`shadow_route` y sin tarea en foco (una ruta `task` no predice un inicio), la predicción es el
`procedure_id` de una ruta `rule` o `trigger` (si no, "ninguno") y lo real es el primer
procedimiento que se inició en esa conversación entre ese turno y el siguiente
(`conversation_tasks.created_at`). Precondición: solo hay verdad con `aos_procedures` encendida, y
`iniciar_procedimiento` ofrece solo los procedimientos del agente en foco, así que la medida es
justa si el agente por defecto los declara todos (el General del lector tolerante).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import text

from core.kernel.router import MODOS

MAX_DIAS = 31
_PREDICEN = ("rule", "trigger")


@dataclass(frozen=True)
class Metrica:
    procedure_id: str
    verdaderos: int
    predichos: int
    reales: int

    @property
    def precision(self) -> float | None:
        return self.verdaderos / self.predichos if self.predichos else None

    @property
    def recall(self) -> float | None:
        return self.verdaderos / self.reales if self.reales else None


@dataclass
class Reporte:
    turnos: int = 0
    metricas: list[Metrica] = field(default_factory=list)


def metricas(pares: Iterable[tuple[str | None, str | None]]) -> Reporte:
    """`(predicho, real)` por turno (None = ninguno) → métricas por procedimiento. Puro."""
    tp: dict[str, int] = {}
    pred: dict[str, int] = {}
    real: dict[str, int] = {}
    reporte = Reporte()
    for predicho, verdadero in pares:
        reporte.turnos += 1
        if predicho:
            pred[predicho] = pred.get(predicho, 0) + 1
        if verdadero:
            real[verdadero] = real.get(verdadero, 0) + 1
        if predicho and predicho == verdadero:
            tp[predicho] = tp.get(predicho, 0) + 1
    for pid in sorted(set(pred) | set(real)):
        reporte.metricas.append(Metrica(pid, tp.get(pid, 0), pred.get(pid, 0), real.get(pid, 0)))
    return reporte


async def reporte_de_sombra(tenant_id: uuid.UUID, dias: int = 7) -> Reporte:
    """El reporte del negocio (bajo su `tenant_scope`; RLS y filtro por `tenant_id`)."""
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ruteo: fuera del tenant_scope del negocio")
    dias = max(1, min(int(dias), MAX_DIAS))
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "WITH d AS ("
                    "  SELECT conversation_id, created_at, shadow_route, "
                    "         lead(created_at) OVER (PARTITION BY conversation_id ORDER BY created_at) AS siguiente "
                    "    FROM core_decisions "
                    "   WHERE tenant_id = :t AND outcome = 'routed' "
                    "     AND created_at >= now() - make_interval(days => :dias)"
                    ") "
                    "SELECT d.shadow_route->>'route_source', d.shadow_route->>'procedure_id', "
                    "  (SELECT ct.procedure_id FROM conversation_tasks ct "
                    "    WHERE ct.tenant_id = :t AND ct.conversation_id = d.conversation_id "
                    "      AND ct.parent_task_id IS NULL AND ct.created_at >= d.created_at "
                    "      AND (d.siguiente IS NULL OR ct.created_at < d.siguiente) "
                    "    ORDER BY ct.created_at LIMIT 1) "
                    "FROM d WHERE d.shadow_route IS NOT NULL AND d.shadow_route->>'route_source' <> 'task'"
                ),
                {"t": tenant_id, "dias": dias},
            )
        ).all()
    return metricas(
        ((str(f[1]) if f[0] in _PREDICEN and f[1] else None), (str(f[2]) if f[2] else None)) for f in filas
    )


async def escribir_modo(tenant_id: uuid.UUID, modo: str, database_url: str | None = None) -> None:
    """Fija `tenants.aos_routing` con el rol dueño (`tenants` no la escribe `app_runtime`,
    ADR-0112). Exige `aos_runtime = 'v3'` para encenderla (el CHECK de 0094 también)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings

    if modo not in MODOS:
        raise ValueError(f"modo inválido: {modo}")
    url = database_url or get_settings().migrations_database_url
    if not url:
        raise PermissionError("falta MIGRATIONS_DATABASE_URL (rol dueño) para escribir las banderas")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            runtime = (
                await conn.execute(
                    text("SELECT aos_runtime FROM tenants WHERE id = :t AND deleted_at IS NULL FOR UPDATE"),
                    {"t": tenant_id},
                )
            ).scalar_one_or_none()
            if runtime is None:
                raise LookupError("negocio no encontrado")
            if modo != "off" and runtime != "v3":
                raise ValueError("el enrutamiento exige aos_runtime = 'v3' (migrar-a-v3 primero)")
            await conn.execute(text("UPDATE tenants SET aos_routing = :m WHERE id = :t"), {"m": modo, "t": tenant_id})
    finally:
        await engine.dispose()


__all__ = ["MAX_DIAS", "Metrica", "Reporte", "escribir_modo", "metricas", "reporte_de_sombra"]
