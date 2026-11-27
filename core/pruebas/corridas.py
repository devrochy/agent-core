"""Crear una corrida de pruebas del agente: estimado, aprobación y cupo (SPEC-53, ADR-0212 §4, HU-AOS-102).

Dos operaciones, las dos dentro del `tenant_scope` del negocio y con `tenant_id` explícito además de la RLS:

- `estimar`: la tabla `por_k` de `core.billing.pruebas.estimar_corrida` para los escenarios pedidos. No escribe.
- `crear`: valida y deja la corrida `en_cola`. Es la compuerta del dinero, en este orden:
  1. escenarios del agente, del negocio, aprobados y no borrados (de otro negocio o agente: 404 sin eco);
  2. `k` entre 1 y el tope del negocio (422 `k_fuera_de_rango`) y máx. 20 escenarios;
  3. 5 corridas por día (429) y UNA activa por negocio (409 `corrida_en_curso`), bajo un candado consultivo por
     negocio; el índice único parcial `uq_agent_test_runs_activa` es la red de seguridad si dos peticiones
     llegan juntas;
  4. `confirmo_creditos` igual al estimado de ese `k` recalculado AHORA (409 `estimado_cambio`, con el vigente);
  5. `alcanza()` sobre lo que sale del saldo (409 `plan_limit_creditos`).
  Nada se escribe si falla un paso. La corrida guarda estimado, cuota, tope (estimado × 1,5) y quién la pidió.

NO ejecuta nada: el corredor (HU-AOS-103) toma las corridas `en_cola`. `version_probada` queda con los
`escenarios` pedidos (la tabla no tiene otra columna para ellos) y el corredor le suma la versión que prueba.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from core.billing.pruebas import (
    MAX_ESCENARIOS,
    AgenteNoEncontrado,
    EstimadoDeCorrida,
    estimar_corrida,
    tope_de_creditos,
)
from core.db.models import AgentTestCase, AgentTestRun, AuditLog
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope

log = get_logger(__name__)

CORRIDAS_POR_DIA = 5
ACCION_AUDITORIA = "prueba_corrida_creada"
ACCION_CANCELAR = "prueba_corrida_cancelada"
_ACTIVAS = ("en_cola", "corriendo")


@dataclass
class ErrorPruebas(Exception):
    status: int
    code: str
    message: str
    details: dict[str, Any] | None = None


def _agente_no_encontrado() -> ErrorPruebas:
    return ErrorPruebas(404, "not_found", "No encontrado.")


async def _escenarios(
    session, tenant_id: uuid.UUID, agent_id: uuid.UUID, case_ids: list[uuid.UUID]  # type: ignore[no-untyped-def]
) -> list[uuid.UUID]:
    """Ids sin repetir, en el orden pedido. Todo escenario debe ser de este agente y negocio, vivo y aprobado."""
    ids = list(dict.fromkeys(case_ids))
    if not ids:
        raise ErrorPruebas(422, "sin_escenarios", "Elige al menos un escenario.")
    if len(ids) > MAX_ESCENARIOS:
        raise ErrorPruebas(
            422, "demasiados_escenarios", f"Máximo {MAX_ESCENARIOS} escenarios por corrida.", {"maximo": MAX_ESCENARIOS}
        )
    filas = {
        i: estado
        for i, estado in (
            await session.execute(
                select(AgentTestCase.id, AgentTestCase.estado).where(
                    AgentTestCase.tenant_id == tenant_id,
                    AgentTestCase.agent_id == agent_id,
                    AgentTestCase.deleted_at.is_(None),
                    AgentTestCase.id.in_(ids),
                )
            )
        ).all()
    }
    if len(filas) != len(ids):  # uno no existe, es de otro agente o negocio, o está archivado
        raise ErrorPruebas(404, "escenario_no_encontrado", "Algún escenario no existe.")
    if any(estado != "aprobado" for estado in filas.values()):
        raise ErrorPruebas(422, "escenario_no_aprobado", "Solo se corren escenarios aprobados.")
    return ids


def _verificar_k(k: int, estimado: EstimadoDeCorrida) -> None:
    if not 1 <= k <= estimado.k_maximo:
        raise ErrorPruebas(
            422,
            "k_fuera_de_rango",
            f"Los intentos por escenario van de 1 a {estimado.k_maximo}.",
            {"k_maximo": estimado.k_maximo},
        )


async def estimar(
    tenant_id: uuid.UUID, agent_id: uuid.UUID, case_ids: list[uuid.UUID]
) -> EstimadoDeCorrida:
    """Estimado por `k` de correr estos escenarios. No escribe ni toma candados."""
    async with tenant_scope(tenant_id), get_session() as session:
        ids = await _escenarios_o_agente(session, tenant_id, agent_id, case_ids)
        try:
            return await estimar_corrida(session, tenant_id, agent_id, len(ids))
        except AgenteNoEncontrado:
            raise _agente_no_encontrado() from None


async def _escenarios_o_agente(
    session, tenant_id: uuid.UUID, agent_id: uuid.UUID, case_ids: list[uuid.UUID]  # type: ignore[no-untyped-def]
) -> list[uuid.UUID]:
    from core.db.models import AgentRecord

    existe = (
        await session.execute(
            select(AgentRecord.id).where(
                AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id, AgentRecord.status == "active"
            )
        )
    ).scalar_one_or_none()
    if existe is None:
        raise _agente_no_encontrado()
    return await _escenarios(session, tenant_id, agent_id, case_ids)


async def crear(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    case_ids: list[uuid.UUID],
    *,
    k: int,
    confirmo_creditos: int,
    pedida_por: uuid.UUID | None,
    actor: str,
) -> AgentTestRun:
    """Crea la corrida `en_cola`. Ver el docstring del módulo."""
    from core.billing.creditos import alcanza

    async with tenant_scope(tenant_id), get_session() as session:
        ids = await _escenarios_o_agente(session, tenant_id, agent_id, case_ids)
        # Un candado por negocio serializa el cupo y el límite diario; se suelta al terminar la transacción.
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"pruebas:corrida:{tenant_id}"}
        )
        estimado = await estimar_corrida(session, tenant_id, agent_id, len(ids))
        _verificar_k(k, estimado)

        ahora = datetime.now(UTC)
        activa = (
            await session.execute(
                select(AgentTestRun.id)
                .where(
                    AgentTestRun.tenant_id == tenant_id,
                    AgentTestRun.deleted_at.is_(None),
                    AgentTestRun.estado.in_(_ACTIVAS),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if activa is not None:
            raise ErrorPruebas(409, "corrida_en_curso", "Ya hay una corrida de pruebas en curso en este negocio.")
        hoy = (
            await session.execute(
                select(func.count(AgentTestRun.id)).where(
                    AgentTestRun.tenant_id == tenant_id, AgentTestRun.created_at >= ahora - timedelta(days=1)
                )
            )
        ).scalar_one()
        if hoy >= CORRIDAS_POR_DIA:
            raise ErrorPruebas(
                429,
                "corridas_por_dia",
                f"Ya lanzaste {CORRIDAS_POR_DIA} corridas en las últimas 24 horas. Vuelve a intentarlo más tarde.",
                {"limite": CORRIDAS_POR_DIA},
            )

        vigente = estimado.de(k)
        assert vigente is not None
        if confirmo_creditos != vigente.estimado:
            raise ErrorPruebas(
                409,
                "estimado_cambio",
                "El estimado cambió. Revísalo y confirma de nuevo.",
                {"estimado": vigente.estimado, "k": k},
            )
        # Sin saldo: QuotaExceeded → 409 `plan_limit_creditos` (la ruta lo convierte); no se crea la corrida.
        await alcanza(session, tenant_id, vigente.del_saldo)

        corrida = AgentTestRun(
            tenant_id=tenant_id,
            agent_id=agent_id,
            estado="en_cola",
            k=k,
            version_probada={"escenarios": [str(i) for i in ids]},
            creditos_estimados=Decimal(vigente.estimado),
            creditos_de_cuota=Decimal(vigente.de_la_cuota),
            tope_creditos=tope_de_creditos(vigente.estimado),
            pedida_por=pedida_por,
        )
        session.add(corrida)
        try:
            await session.flush()
        except IntegrityError as exc:  # el índice parcial `uq_agent_test_runs_activa` ganó una carrera
            if "uq_agent_test_runs_activa" not in str(exc.orig):
                raise
            raise ErrorPruebas(409, "corrida_en_curso", "Ya hay una corrida de pruebas en curso en este negocio.") from exc
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action=ACCION_AUDITORIA,
                input={
                    "run_id": str(corrida.id),
                    "agent_id": str(agent_id),
                    "k": k,
                    "escenarios": len(ids),
                    "estimado": vigente.estimado,
                    "de_la_cuota": vigente.de_la_cuota,
                },
            )
        )
        log.info(ACCION_AUDITORIA, tenant_id=str(tenant_id), run_id=str(corrida.id), k=k, estimado=vigente.estimado)
        return corrida


async def cancelar(tenant_id: uuid.UUID, agent_id: uuid.UUID, run_id: uuid.UUID, *, actor: str) -> AgentTestRun:
    """Cancela una corrida activa del agente (`en_cola` o `corriendo`). El corredor lo ve entre turnos, termina el
    intento que tenía abierto (consolida lo gastado y purga) y no sigue; se cobra solo lo gastado. 404 sin eco si la
    corrida no es de este agente y negocio; 409 `corrida_no_activa` si ya terminó."""
    async with tenant_scope(tenant_id), get_session() as session:
        corrida = (
            await session.execute(
                select(AgentTestRun)
                .where(
                    AgentTestRun.tenant_id == tenant_id,
                    AgentTestRun.agent_id == agent_id,
                    AgentTestRun.id == run_id,
                    AgentTestRun.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if corrida is None:
            raise _agente_no_encontrado()
        if corrida.estado not in _ACTIVAS:
            raise ErrorPruebas(409, "corrida_no_activa", "La corrida ya terminó.", {"estado": corrida.estado})
        ahora = datetime.now(UTC)
        corrida.estado = "cancelada"
        corrida.terminada_en = ahora
        session.add(
            AuditLog(
                tenant_id=tenant_id,
                actor=actor[:120],
                action=ACCION_CANCELAR,
                input={"run_id": str(run_id), "agent_id": str(agent_id)},
            )
        )
        await session.flush()
        log.info(ACCION_CANCELAR, tenant_id=str(tenant_id), run_id=str(run_id))
        session.expunge(corrida)
        return corrida


__all__ = ["CORRIDAS_POR_DIA", "ErrorPruebas", "cancelar", "crear", "estimar"]
