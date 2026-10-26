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
                    # Las filas que solo traen la clave `pila` (HU-AOS-20, ruteo activo) no son una ruta en sombra.
                    "FROM d WHERE d.shadow_route ? 'route_source' AND d.shadow_route->>'route_source' <> 'task'"
                ),
                {"t": tenant_id, "dias": dias},
            )
        ).all()
    return metricas(
        ((str(f[1]) if f[0] in _PREDICEN and f[1] else None), (str(f[2]) if f[2] else None)) for f in filas
    )


# ------------------------------------------------------------------ pila de tareas (HU-AOS-20)
VENTANA_PILA_HORAS = 12


@dataclass
class ReportePila:
    """Lo que la pila habría abierto (`shadow_route.pila`, turnos sin foco) frente a los
    procedimientos que de verdad se iniciaron en las `VENTANA_PILA_HORAS` siguientes."""

    turnos: int = 0
    predichos: int = 0
    reales: int = 0
    aciertos: int = 0
    completos: int = 0  # turnos en que se inició todo lo que habría abierto

    @property
    def precision(self) -> float | None:
        return self.aciertos / self.predichos if self.predichos else None

    @property
    def recall(self) -> float | None:
        return self.aciertos / self.reales if self.reales else None


def metricas_de_pila(filas: Iterable[tuple[Iterable[str], Iterable[str]]]) -> ReportePila:
    """`(lo que habría abierto, lo que se inició)` por turno → métricas. Puro."""
    reporte = ReportePila()
    for predichos, reales in filas:
        p, r = set(predichos), set(reales)
        reporte.turnos += 1
        reporte.predichos += len(p)
        reporte.reales += len(r)
        reporte.aciertos += len(p & r)
        reporte.completos += int(bool(p) and p <= r)
    return reporte


async def reporte_de_pila(tenant_id: uuid.UUID, dias: int = 7) -> ReportePila:
    """El bloque «pila» del reporte de sombra (bajo el `tenant_scope` del negocio). Solo turnos sin
    tarea en foco: con foco, el modelo no puede iniciar otra tarea y no hay con qué comparar."""
    from core.db.session import get_session
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("ruteo: fuera del tenant_scope del negocio")
    dias = max(1, min(int(dias), MAX_DIAS))
    async with get_session() as session:
        filas = (
            await session.execute(
                text(
                    "SELECT d.shadow_route->'pila', "
                    "  coalesce((SELECT array_agg(DISTINCT ct.procedure_id) FROM conversation_tasks ct "
                    "    WHERE ct.tenant_id = :t AND ct.conversation_id = d.conversation_id "
                    "      AND ct.parent_task_id IS NULL AND ct.created_at >= d.created_at "
                    "      AND ct.created_at < d.created_at + make_interval(hours => :horas)), '{}') "
                    "FROM core_decisions d "
                    "WHERE d.tenant_id = :t AND d.outcome = 'routed' AND d.focus_task_id IS NULL "
                    "  AND d.shadow_route ? 'pila' AND d.created_at >= now() - make_interval(days => :dias)"
                ),
                {"t": tenant_id, "dias": dias, "horas": VENTANA_PILA_HORAS},
            )
        ).all()

    def predichos(pila: object) -> list[str]:
        d = pila if isinstance(pila, dict) else {}
        return [str(x) for x in [*(d.get("abre") or []), *(d.get("pausa") or [])]]

    return metricas_de_pila((predichos(f[0]), [str(x) for x in (f[1] or [])]) for f in filas)


async def escribir_modo(tenant_id: uuid.UUID, modo: str, database_url: str | None = None) -> None:
    """Fija `tenants.aos_routing` con el rol dueño y `aos_fijar_banderas` (migración 0097; el
    panel del master usa la misma función con su `admin_id`). Exige `aos_runtime = 'v3'` para
    encenderla (la función y el CHECK de 0094 también)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from core.config import get_settings
    from core.kernel.banderas import BanderasAOS, fijar_banderas

    if modo not in MODOS:
        raise ValueError(f"modo inválido: {modo}")
    url = database_url or get_settings().migrations_database_url
    if not url:
        raise PermissionError("falta MIGRATIONS_DATABASE_URL (rol dueño) para escribir las banderas")
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            fila = (
                await conn.execute(
                    text("SELECT aos_runtime, aos_procedures FROM tenants WHERE id = :t AND deleted_at IS NULL FOR UPDATE"),
                    {"t": tenant_id},
                )
            ).first()
            if fila is None:
                raise LookupError("negocio no encontrado")
            if modo != "off" and fila[0] != "v3":
                raise ValueError("el enrutamiento exige aos_runtime = 'v3' (migrar-a-v3 primero)")
            await fijar_banderas(
                conn,
                tenant_id,
                BanderasAOS(runtime=str(fila[0]), procedimientos=bool(fila[1]), ruteo=modo),
                actor="system:ruteo",
            )
    finally:
        await engine.dispose()


__all__ = [
    "MAX_DIAS",
    "Metrica",
    "Reporte",
    "ReportePila",
    "escribir_modo",
    "metricas",
    "metricas_de_pila",
    "reporte_de_pila",
    "reporte_de_sombra",
]
