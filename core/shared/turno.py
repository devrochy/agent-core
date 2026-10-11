"""Turno vigente del Coordinador (HU-AOS-10, adenda de ADR-0112).

El kernel (`core/kernel/decisions.py`) fija aquí el turno **después** de commitear su
cabecera en `core_decisions`. Los modelos leen `turno_id()` y `trace_id()` como default de
columna (`messages`, `usage_ledger`, `audit_log`): toda fila que el turno causa lleva su
`turn_id` sin tocar a quien la escribe. Fuera de un turno, o si la cabecera no se escribió,
el default es NULL.

Vive en `core.shared` porque lo leen los modelos, y `core.db` no importa `core.kernel`.

Guarda de negocio: la FK `(tenant_id, turn_id) → core_decisions (tenant_id, id)` se revisa en
el COMMIT. Si dentro de un turno se escribe bajo el `tenant_scope` de OTRO negocio (cobro de
plataforma, packs que cambian de scope), el default devuelve NULL: nunca hace fallar una
escritura que antes funcionaba.

Tareas Celery (seguimiento #439): el turno viaja a la tarea en headers del mensaje
(`core.observability.celery_trazas`) y el worker lo fija aquí como `heredado`; `run_async`
lo lleva a la corrutina (copia el contexto del hilo de la tarea). Un turno heredado no se
vuelve a propagar a tareas nuevas que encole la tarea (solo su propio reintento, que Celery
republica con los mismos headers).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from core.shared.tenant import get_current_tenant_id


@dataclass(frozen=True)
class TurnoVigente:
    turn_id: uuid.UUID
    tenant_id: uuid.UUID
    trace_id: str | None
    # True: llegó de los headers de una tarea Celery (no lo abrió el kernel en este proceso).
    heredado: bool = False


_vigente: ContextVar[TurnoVigente | None] = ContextVar("turno_vigente", default=None)


def fijar(turno: TurnoVigente | None) -> Token[TurnoVigente | None]:
    """Fija el turno vigente (None lo limpia: el worker lo hace al empezar cada tarea)."""
    return _vigente.set(turno)


def soltar(token: Token[TurnoVigente | None]) -> None:
    _vigente.reset(token)


def _del_negocio(tenant_id: object) -> TurnoVigente | None:
    turno = _vigente.get()
    if turno is None or tenant_id is None:
        return None
    return turno if str(tenant_id) == str(turno.tenant_id) else None


def _tenant_de_la_fila(context: Any) -> object:
    """`tenant_id` de la fila que se inserta (default de columna con contexto); sin
    contexto, el negocio del `tenant_scope`."""
    if context is not None:
        try:
            return context.get_current_parameters().get("tenant_id")
        except Exception:
            return None
    return get_current_tenant_id()


def vigente() -> TurnoVigente | None:
    """Turno vigente si es del negocio del `tenant_scope` actual (la guarda del default de
    columna, sin fila); si no, None."""
    return _del_negocio(get_current_tenant_id())


def turno_id(context: Any = None) -> uuid.UUID | None:
    """`turn_id` del turno vigente si la fila (o el negocio en contexto) es de su mismo
    negocio; si no, None. Sirve de default de columna de SQLAlchemy."""
    turno = _del_negocio(_tenant_de_la_fila(context))
    return turno.turn_id if turno is not None else None


def trace_id(context: Any = None) -> str | None:
    """`trace_id` del turno vigente, con la misma guarda de negocio que `turno_id`."""
    turno = _del_negocio(_tenant_de_la_fila(context))
    return turno.trace_id if turno is not None else None


# Defaults de columna: SQLAlchemy pasa el contexto de ejecución solo a callables con un
# argumento posicional obligatorio. Solo `default` (al insertar), nunca `onupdate`.
def turn_id_de_fila(context: Any) -> uuid.UUID | None:
    return turno_id(context)


def trace_id_de_fila(context: Any) -> str | None:
    return trace_id(context)
