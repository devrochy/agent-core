"""Veredictos del gate de pass^k de una release candidata (HU-AOS-21, adenda de ADR-0117).

- **Cálculo** (puro): el pass^4 de un procedimiento es la fracción de sus escenarios con
  `pass_k = 1` (modelo de la migración 0058); con 2 o 3 escenarios, ≥ 0,85 equivale a 100 %. Se
  aprueba si cada procedimiento medido llega al umbral y no queda por debajo del de la release
  activa medido con los mismos escenarios (`escenarios_sha256`). Sin valor de la activa, solo
  aplica el umbral.
- **Registro**: en UNA transacción del negocio real, dentro de su `tenant_scope`: `eval_runs`
  (`origen`, `release_id`), sus `eval_results` (`procedure_id`), una fila agregada de
  `usage_ledger` (`kind = 'eval'`, a cargo de la plataforma) y el `release_event` del veredicto
  (`approved` o `rejected`) con su `base_release_id`.
- **Lectura**: el gate solo cree el último veredicto de la release cuyo `base_release_id` es la
  release activa de ahora; uno medido contra otra base cuenta como inexistente.
- **Tope**: lo gastado en el mes en `kind = 'eval'` frente a
  `tenants.settings.evals.tope_usd_mensual` (o `EVAL_TOPE_USD_MENSUAL`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from core.billing.tipos_de_uso import EVAL
from core.shared.logger import get_logger

log = get_logger(__name__)

UMBRAL = 0.85
K = 4
APROBADO, RECHAZADO = "approved", "rejected"


@dataclass(frozen=True)
class Escenario:
    nombre: str
    k: int
    pass_1: float
    pass_k: float
    ok: bool
    detalle: str = ""


@dataclass(frozen=True)
class Medicion:
    """Lo que midió el corredor para un procedimiento."""

    procedure_id: str
    escenarios: tuple[Escenario, ...]
    escenarios_sha256: str

    @property
    def pass_k(self) -> float:
        if not self.escenarios:
            return 0.0
        return sum(1 for e in self.escenarios if e.pass_k >= 1.0) / len(self.escenarios)


@dataclass(frozen=True)
class Veredicto:
    aprobado: bool
    por_procedimiento: dict[str, dict[str, Any]] = field(default_factory=dict)
    motivo: str = ""


def calcular(
    mediciones: list[Medicion],
    activa: dict[str, dict[str, Any]] | None = None,
    umbral: float = UMBRAL,
) -> Veredicto:
    """`activa`: `por_procedimiento` del último veredicto aprobado de la release activa, o None."""
    por: dict[str, dict[str, Any]] = {}
    fallas: list[str] = []
    for m in sorted(mediciones, key=lambda x: x.procedure_id):
        previo = (activa or {}).get(m.procedure_id)
        referencia = (
            float(previo["pass_k"])
            if previo and previo.get("escenarios_sha256") == m.escenarios_sha256
            else None
        )
        ok = m.pass_k >= umbral and (referencia is None or m.pass_k >= referencia)
        por[m.procedure_id] = {
            "pass_k": round(m.pass_k, 4),
            "activa": referencia,
            "ok": ok,
            "escenarios": len(m.escenarios),
            "escenarios_sha256": m.escenarios_sha256,
        }
        if not ok:
            fallas.append(m.procedure_id)
    if not mediciones:
        return Veredicto(False, {}, "sin_mediciones")
    motivo = "" if not fallas else "bajo_umbral_o_regresion:" + ",".join(fallas)
    return Veredicto(not fallas, por, motivo)


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise PermissionError("veredicto: fuera del tenant_scope del negocio")


async def registrar(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    release_id: uuid.UUID,
    *,
    base_release_id: uuid.UUID | None,
    tocados: dict[str, str],
    mediciones: list[Medicion],
    veredicto: Veredicto,
    costo_usd: float,
    modelo: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    origen: str = "gate",
    actor: str = "system:gate",
    git_sha: str | None = None,
) -> uuid.UUID:
    """Escribe la corrida, sus resultados, el gasto y el evento del veredicto en una transacción.

    Exige el `tenant_scope` del negocio; filtra por `tenant_id` además de RLS."""
    from core.billing.tarifario import TARIFARIO_VIGENTE, trm_vigente
    from core.db.models import EvalResult, EvalRun, UsageLedger
    from core.db.session import get_session
    from core.releases.build import registrar_evento
    _exigir_scope(tenant_id)
    escenarios = [e for m in mediciones for e in m.escenarios]
    async with get_session() as session:
        intento = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM release_events WHERE tenant_id = :t AND release_id = :r "
                        "AND type IN ('approved', 'rejected')"
                    ),
                    {"t": tenant_id, "r": release_id},
                )
            ).scalar_one()
        ) + 1
        corrida = EvalRun(
            tenant_id=tenant_id,
            release_id=release_id,
            origen=origen,
            git_ref=git_sha,
            passed=sum(1 for e in escenarios if e.ok),
            total=len(escenarios),
            cost_usd=costo_usd,
            k=K,
            pass_1=(sum(e.pass_1 for e in escenarios) / len(escenarios)) if escenarios else None,
            pass_k=(sum(e.pass_k for e in escenarios) / len(escenarios)) if escenarios else None,
        )
        session.add(corrida)
        await session.flush()
        for m in mediciones:
            for e in m.escenarios:
                session.add(
                    EvalResult(
                        tenant_id=tenant_id,
                        run_id=corrida.id,
                        scenario=e.nombre,
                        ok=e.ok,
                        detail=e.detalle[:2000],
                        k=e.k,
                        pass_1=e.pass_1,
                        pass_k=e.pass_k,
                        procedure_id=m.procedure_id,
                    )
                )
        session.add(
            UsageLedger(
                tenant_id=tenant_id,
                conversation_id=None,
                turn_id=None,
                connection_id=connection_id,
                release_id=release_id,
                kind=EVAL,
                model=modelo,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=costo_usd,
                result={"proposito": "gate", "run_id": str(corrida.id)},
                # A cargo de la plataforma (decisión del dueño, 2026-10-25): sin créditos del negocio.
                credits=Decimal("0.0"),
                key_mode="platform",
                tarifario_version=TARIFARIO_VIGENTE.version,
                trm_cop=trm_vigente(),
            )
        )
        await registrar_evento(
            session,
            tenant_id,
            connection_id,
            release_id,
            APROBADO if veredicto.aprobado else RECHAZADO,
            actor,
            {
                "run_id": str(corrida.id),
                "intento": intento,
                "k": K,
                "umbral": UMBRAL,
                "base_release_id": str(base_release_id) if base_release_id else None,
                "git_sha": git_sha,
                "tocados": dict(sorted(tocados.items())[:200]),
                "por_procedimiento": veredicto.por_procedimiento,
                "motivo": veredicto.motivo[:200],
            },
        )
        run_id: uuid.UUID = corrida.id
    log.info(
        "release_veredicto",
        release_id=str(release_id),
        aprobado=veredicto.aprobado,
        intento=intento,
        procedimientos=len(mediciones),
    )
    return run_id


async def ultimo(
    session: Any, tenant_id: uuid.UUID, release_id: uuid.UUID, base_release_id: uuid.UUID | None
) -> dict[str, Any] | None:
    """El último veredicto de la release medido contra `base_release_id`, o None."""
    _exigir_scope(tenant_id)
    fila = (
        await session.execute(
            text(
                "SELECT type, data FROM release_events WHERE tenant_id = :t AND release_id = :r "
                "AND type IN ('approved', 'rejected') "
                "AND (data->>'base_release_id') IS NOT DISTINCT FROM :b "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": release_id, "b": str(base_release_id) if base_release_id else None},
        )
    ).first()
    if fila is None:
        return None
    return {"tipo": fila[0], **dict(fila[1] or {})}


async def de_la_activa(session: Any, tenant_id: uuid.UUID, activa: uuid.UUID) -> dict[str, dict[str, Any]] | None:
    """`por_procedimiento` del último veredicto aprobado de la release activa, o None."""
    _exigir_scope(tenant_id)
    fila = (
        await session.execute(
            text(
                "SELECT data->'por_procedimiento' FROM release_events WHERE tenant_id = :t "
                "AND release_id = :r AND type = 'approved' ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant_id, "r": activa},
        )
    ).scalar_one_or_none()
    return dict(fila) if isinstance(fila, dict) else None


async def tope_del_mes(session: Any, tenant_id: uuid.UUID, ahora: datetime | None = None) -> tuple[float, float]:
    """`(gastado, tope)` en USD del mes en curso en evals del negocio."""
    _exigir_scope(tenant_id)
    from core.config import get_settings

    ahora = ahora or datetime.now(UTC)
    inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    gastado = (
        await session.execute(
            text(
                "SELECT coalesce(sum(cost_usd), 0) FROM usage_ledger "
                "WHERE tenant_id = :t AND kind = :k AND created_at >= :i"
            ),
            {"t": tenant_id, "k": EVAL, "i": inicio},
        )
    ).scalar_one()
    ajuste = (
        await session.execute(
            text("SELECT settings->'evals'->>'tope_usd_mensual' FROM tenants WHERE id = :t"), {"t": tenant_id}
        )
    ).scalar_one_or_none()
    try:
        tope = float(ajuste) if ajuste is not None else float(get_settings().eval_tope_usd_mensual)
    except (TypeError, ValueError):
        tope = float(get_settings().eval_tope_usd_mensual)
    return float(gastado or 0), tope
