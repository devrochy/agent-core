"""Colaboración A2A del turno y handoff en dos fases (ADR-0116, adenda HU-AOS-19 §2-§3).

- `ContextoA2A`: lo que el motor del turno necesita para ofrecer y aplicar los comandos entre
  agentes. Se arma una vez por turno desde la ranura de la release: el grafo congelado, los límites
  del KernelSpec congelado, el agente del turno y un `EstadoDelTurno` que comparten todas las
  operaciones del turno.
- `destinos_de_handoff`: especialistas a los que el agente del turno puede pasar la conversación.
  Hace falta un enlace `handoff` y que el destino declare el procedimiento de cada tarea abierta;
  si no, la tarea quedaría con un dueño que no la puede gobernar.
- **Fase 1** (en el turno, `ContextoMotor._pasar`): la guarda registra la operación y, si la
  acepta, el `Turno` guarda un `Pendiente` con todo lo que necesita la fase 2.
- **Fase 2** (`cerrar`, la llama `decisions._abrir_turno` después de la entrega y antes de
  `turn_outcomes`): con el turno `replied` o `silent` cambia el dueño de la conversación y de sus
  tareas abiertas bajo lock; si no, cancela. Nunca lanza: si lo hiciera, el consumer reintentaría el
  turno y el cliente recibiría la respuesta dos veces.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from core.kernel import a2a
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

_TIMEOUT_S = 2.0
# Solo con estos estados el cliente vio la respuesta del turno (con `neutral`, el guardrail la
# reemplazó y no vio la despedida).
_ESTADOS_QUE_APLICAN = frozenset({"replied", "silent"})


@dataclass(frozen=True)
class Pendiente:
    """Un handoff aceptado en la fase 1: lo que la fase 2 necesita sin leer la release."""

    estado: a2a.EstadoDelTurno
    call_id: uuid.UUID
    desde: uuid.UUID
    hacia: uuid.UUID
    conversation_id: uuid.UUID
    # Tareas abiertas cuando se aceptó (las que el destino declara). Si en la fase 2 aparece otra,
    # se cancela: el destino podría no gobernarla.
    tareas: frozenset[uuid.UUID] = frozenset()


@dataclass
class ContextoA2A:
    grafo: a2a.GrafoCongelado
    limites: a2a.Limites
    estado: a2a.EstadoDelTurno
    agente: Any  # AgenteCongelado del turno
    agentes: tuple[Any, ...]
    # HU-AOS-19 parte 2: los procedimientos publicados de cada componente (id → `Procedure`) y
    # todos los de la release (para saber qué acciones gobierna el motor).
    procedimientos_por_agente: Mapping[uuid.UUID, Mapping[str, Any]] = field(default_factory=dict)
    procedimientos: Mapping[str, Any] = field(default_factory=dict)
    packs: frozenset[str] = frozenset()

    @classmethod
    def del_turno(cls, conversation_id: uuid.UUID) -> ContextoA2A | None:
        """None sin release, sin grafo o sin agente en foco: no hay comandos entre agentes."""
        from core.shared import release_vigente, turno

        release = release_vigente.vigente()
        agente = release_vigente.agente_del_turno()
        if release is None or release.a2a_graph is None or agente is None:
            return None
        kernel = release_vigente.kernel_del_turno()
        grafo = a2a.GrafoCongelado.desde_manifiesto(
            release.a2a_graph, {c.instance_id: c.agent_id for c in release.agentes}
        )
        estado = a2a.EstadoDelTurno(
            release.tenant_id,
            release.connection_id,
            conversation_id,
            turno.turno_id(),
            release_id=release.release_id,
        )
        return cls(
            grafo,
            a2a.Limites.del_kernel(kernel.content if kernel else None),
            estado,
            agente,
            release.agentes,
            procedimientos_por_agente=dict(release.procedimientos_por_agente or {}),
            procedimientos=dict(release.procedimientos),
            packs=frozenset((*release.skills, *release.tenant_plugins)),
        )

    def destinos(self, modo: str) -> dict[str, Any]:
        """Especialistas alcanzables desde el agente del turno con `modo`, por key."""
        salida: dict[str, Any] = {}
        for c in self.agentes:
            if c.role != "specialist" or c.instance_id == self.agente.instance_id:
                continue
            enlace = self.grafo.enlace(self.agente.instance_id, c.instance_id)
            if enlace is not None and modo in enlace.modes:
                salida[c.key] = c
        return salida


def declara(agente: Any, procedure_ids: Iterable[str]) -> bool:
    """¿El agente declara todos estos procedimientos? (`release/2`; sin la lista, no)."""
    propios = {pid for pid, _v in (agente.procedimientos or ())}
    return agente.procedimientos is not None and set(procedure_ids) <= propios


def propias(filas: Iterable[Any], desde: uuid.UUID | None) -> list[Any]:
    """Las tareas abiertas que viajan con un handoff de `desde` (HU-AOS-20): la tarea en foco y las
    pausadas sin dueño o de `desde`. Una pausada de otro especialista (la pila de tareas) conserva
    su dueño."""
    return [
        f
        for f in filas
        if (f.status == "active" and f.stack_position == 0) or f.owner_instance_id in (None, desde)
    ]


def destinos_de_handoff(ctx: ContextoA2A, abiertas: Iterable[str]) -> dict[str, Any]:
    procedimientos = set(abiertas)
    return {k: c for k, c in ctx.destinos("handoff").items() if declara(c, procedimientos)}


# ------------------------------------------------------------------ fase 2
async def cerrar(turno: Any) -> uuid.UUID | None:
    """Al cerrar el turno: aplica o cancela el handoff pendiente y cierra la conversación A2A.
    Devuelve el nuevo dueño si el cambio hizo commit. Nunca lanza."""
    pendiente: Pendiente | None = getattr(turno, "handoff", None)
    estado: a2a.EstadoDelTurno | None = getattr(turno, "a2a", None)
    if pendiente is None and (estado is None or estado.a2a_conversation_id is None):
        return None
    propios = {e.tenant_id for e in (estado, pendiente.estado if pendiente else None) if e is not None}
    if get_current_tenant_id() != turno.tenant_id or propios - {turno.tenant_id}:
        log.warning("handoff_fuera_del_negocio", conversation_id=str(turno.conversation_id))
        return None
    # Un solo presupuesto para toda la fase 2 (aplicar y, si falla, cancelar): corre dentro del
    # tope del turno, después de la entrega.
    limite = asyncio.get_running_loop().time() + _TIMEOUT_S
    try:
        async with asyncio.timeout_at(limite):
            if pendiente is None:
                assert estado is not None
                await a2a.cerrar_turno(estado)
                return None
            if turno.status not in _ESTADOS_QUE_APLICAN:
                await _cancelar(pendiente, None)
                log.info("handoff_cancelado", status=turno.status)
                return None
            if await _aplicar(turno, pendiente):
                log.info("handoff_aplicado", desde=str(pendiente.desde), hacia=str(pendiente.hacia))
                return pendiente.hacia
            await _cancelar(pendiente, None)
            log.info("handoff_cancelado", status="dueno_cambiado")
            return None
    except Exception as exc:
        # Si el tope cortó justo después del COMMIT, el dueño quedó cambiado y el ledger dirá
        # `cancelled` (inconsistencia de auditoría aceptada, adenda HU-AOS-19 §3).
        log.warning("handoff_fallo", error=type(exc).__name__)
        if pendiente is not None:
            try:
                async with asyncio.timeout_at(limite):
                    await _cancelar(pendiente, "error")
            except Exception as exc2:
                log.warning("handoff_cancelar_fallo", error=type(exc2).__name__)
        return None


async def _aplicar(turno: Any, p: Pendiente) -> bool:
    from core.db.session import get_session
    from core.kernel import state

    tid = turno.tenant_id
    async with get_session() as s:
        actual = (
            await s.execute(
                text(
                    "SELECT active_agent_instance_id FROM conversations WHERE tenant_id = :t AND id = :c FOR UPDATE"
                ),
                {"t": tid, "c": p.conversation_id},
            )
        ).first()
        if actual is None or actual[0] != p.desde:
            # Otro turno de la misma conversación ya cambió el dueño (el turn gate es por negocio).
            return False
        abiertas = await state.tareas_abiertas(s, tenant_id=tid, conversation_id=p.conversation_id)
        if {f.id for f in abiertas} - p.tareas:
            # Se abrió una tarea después de aceptar: el destino podría no gobernarla. Antes de
            # escribir nada (la sesión confirma al salir).
            return False
        await s.execute(
            text(
                "UPDATE conversations SET active_agent_instance_id = :to, owner_since = now() "
                "WHERE tenant_id = :t AND id = :c"
            ),
            {"t": tid, "c": p.conversation_id, "to": p.hacia},
        )
        for fila in propias(abiertas, p.desde):
            if fila.owner_instance_id == p.hacia:
                continue
            datos: dict[str, Any] = {"to_instance_id": str(p.hacia)}
            if fila.owner_instance_id is not None:
                datos["from_instance_id"] = str(fila.owner_instance_id)
            await state.registrar(
                s,
                tenant_id=tid,
                conversation_id=p.conversation_id,
                task_id=fila.id,
                tipo="owner_changed",
                data=datos,
                actor="kernel",
                turn_id=turno.id if turno._token_vigente is not None else None,
            )
        await a2a.cerrar_handoff(s, p.estado, call_id=p.call_id, desde=p.desde, hacia=p.hacia, aplicado=True)
    return True


async def _cancelar(p: Pendiente, razon: str | None) -> None:
    from core.db.session import get_session

    async with get_session() as s:
        await a2a.cerrar_handoff(
            s, p.estado, call_id=p.call_id, desde=p.desde, hacia=p.hacia, aplicado=False, razon=razon
        )


def destinos_para_el_modelo(destinos: Mapping[str, Any]) -> list[str]:
    """Las keys de los destinos, en orden fijo (determinista para el prompt)."""
    return sorted(destinos)


__all__ = ["ContextoA2A", "Pendiente", "cerrar", "declara", "destinos_de_handoff", "destinos_para_el_modelo"]
