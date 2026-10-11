"""Modo sombra del motor de procedimientos (HU-AOS-05, design §7.1).

Antes de encender `aos_procedures` en un negocio, el motor corre en paralelo al flujo de
siempre: calcula transiciones y comprobaciones, no responde y registra las diferencias. La
ventana es `aos_runtime = 'v3'` con `aos_procedures` apagada (adenda de ADR-0112): no hay una
bandera más y el replay del gate (runtime legacy) no la ejecuta.

- Observa cada acción que el loop ejecutó (nombre, args, si terminó bien y su resultado) sobre una
  tarea sombra. La tarea sombra empieza cuando una acción ejecutada es de la primera etapa de
  exactamente un procedimiento disponible (sin router todavía, HU-AOS-17).
- Su estado vive en Redis (`aos:sombra:{negocio}:{conversación}`, TTL de un episodio) y **no guarda
  valores** del cliente: solo nombres de datos llenos, la etapa, las acciones de la etapa y el
  booleano de cada `requiredWhen`. Así no hay PII que purgar.
- No ejecuta precondiciones ni *resolvers* (sin costo ni efectos): solo tipo, opciones y formato.
- Diferencias: una fila de `audit_log` por lote de acciones con tarea sombra (`core:motor_sombra`,
  `diferencias`), solo códigos y conteos.
- Best-effort con tope de tiempo: nunca cambia la respuesta ni corta el turno.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Mapping
from typing import Any

from agentcore_contracts import Procedure

from core.kernel.episodio import DEFAULT_IDLE_MINUTES
from core.kernel.procedures import motor
from core.shared.logger import get_logger

log = get_logger(__name__)

_TIMEOUT_S = 1.0
_PREFIJO = "aos:sombra"


def clave(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    return f"{_PREFIJO}:{tenant_id}:{conversation_id}"


def _nueva(proc: Procedure) -> dict[str, Any]:
    return {"pid": proc.id, "v": proc.version, "etapa": proc.stages[0].id, "llenos": [], "conds": {}, "acciones": []}


def _aplica(proc: Procedure, estado: Mapping[str, Any], nombre: str) -> bool:
    """¿El dato es obligatorio para salir? (`requiredWhen` por el booleano guardado)."""
    slot = proc.slots.get(nombre)
    if slot is None:
        return False
    if slot.requiredWhen is None:
        return True
    return bool((estado.get("conds") or {}).get(nombre))


def observar_puro(
    procs: Mapping[str, Procedure],
    core: frozenset[str],
    estado: dict[str, Any] | None,
    accion: str,
    args: Mapping[str, Any],
    ok: bool,
    resultado: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, Counter[str]]:
    """Aplica una acción a la tarea sombra. Puro: devuelve el estado nuevo (None = sin tarea
    sombra o terminada) y las diferencias."""
    difs: Counter[str] = Counter()
    if accion in core:
        return estado, difs
    if estado is None:
        if not ok:
            return None, difs
        iniciales = [p for p in procs.values() if accion in p.stages[0].actions]
        if len(iniciales) != 1:
            return None, difs
        estado = _nueva(iniciales[0])
        difs["inicio"] += 1
    proc = procs.get(str(estado.get("pid")))
    stage = motor.etapa(proc, str(estado.get("etapa"))) if proc is not None and proc.version == estado.get("v") else None
    if proc is None or stage is None:
        return None, difs

    llenos = set(estado.get("llenos") or [])
    conds = dict(estado.get("conds") or {})

    def llenar(nombre: str, valor: Any) -> None:
        llenos.add(nombre)
        for dependiente, slot in proc.slots.items():
            if slot.requiredWhen is not None and slot.requiredWhen.slot == nombre:
                conds[dependiente] = valor == slot.requiredWhen.equals

    if accion not in stage.actions:
        difs["accion_fuera_de_etapa"] += 1
    else:
        if motor.requiere_confirmacion(stage, accion):
            difs["accion_sin_confirmacion"] += 1
        estado["acciones"] = [*estado.get("acciones", []), {"a": accion, "ok": ok}][-20:]
    for nombre, valor in args.items():
        slot = proc.slots.get(nombre)
        # Los datos del servidor (`setBy: action:`) solo los fija el resultado; un dato de botón
        # o de contenido lo puede traer el modelo en el flujo de siempre (no hay botones).
        if slot is None or (slot.setBy or "").startswith("action:"):
            continue
        v, rechazo = motor.validar_valor(slot, valor)
        if rechazo is not None:
            difs["dato_invalido"] += 1
        else:
            llenar(nombre, v)
    if ok:
        for nombre, slot in proc.slots.items():
            if slot.setBy == f"action:{accion}" and resultado.get(nombre) is not None:
                llenar(nombre, resultado[nombre])
    estado["llenos"], estado["conds"] = sorted(llenos), conds

    for _ in range(len(proc.stages)):
        salida = stage.exitWhen
        if salida is None:
            break
        faltan = [n for n in salida.slots if _aplica(proc, estado, n) and n not in llenos]
        accion_ok = salida.action is None or any(
            a.get("a") == salida.action and (salida.ok is None or a.get("ok") == salida.ok)
            for a in estado.get("acciones", [])
        )
        if faltan or not accion_ok:
            break
        if salida.confirmed:
            difs["dato_sin_confirmar"] += len(salida.confirmed)
        if motor.puerta_de_salida(stage):
            difs["salida_sin_confirmacion"] += 1
        destino = motor.siguiente(proc, stage)
        if destino is None:
            difs["cierre"] += 1
            return None, difs
        difs["transicion"] += 1
        stage = destino
        estado["etapa"], estado["acciones"] = destino.id, []
    return estado, difs


class Sombra:
    def __init__(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, procs: Mapping[str, Procedure], core: frozenset[str]
    ) -> None:
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.procs = dict(procs)
        self.core = core
        self.difs: Counter[str] = Counter()
        self.procedure_id: str | None = None

    async def observar(self, accion: str, args: Mapping[str, Any], resultado: Mapping[str, Any]) -> None:
        try:
            async with asyncio.timeout(_TIMEOUT_S):
                await self._observar(accion, args, resultado)
        except Exception as exc:
            log.warning("motor_sombra_fallo", error=type(exc).__name__)

    async def _observar(self, accion: str, args: Mapping[str, Any], resultado: Mapping[str, Any]) -> None:
        from core.memory.working import get_redis

        r = get_redis()
        k = clave(self.tenant_id, self.conversation_id)
        crudo = await r.get(k)
        estado = json.loads(crudo) if crudo else None
        ok = not (isinstance(resultado, Mapping) and resultado.get("error"))
        if estado is not None:
            self.procedure_id = str(estado.get("pid"))
        nuevo, difs = observar_puro(self.procs, self.core, estado, accion, args or {}, ok, resultado or {})
        if nuevo is not None:
            self.procedure_id = str(nuevo.get("pid"))
            await r.set(k, json.dumps(nuevo), ex=DEFAULT_IDLE_MINUTES * 60)
        elif estado is not None:
            await r.delete(k)
        self.difs.update(difs)

    async def volcar(self) -> None:
        """Una fila de auditoría con las diferencias del lote (sin valores)."""
        if not self.difs:
            return
        difs, self.difs = dict(self.difs), Counter()
        try:
            from core.shared.audit import record_audit

            async with asyncio.timeout(_TIMEOUT_S):
                await record_audit(
                    actor="core:motor_sombra",
                    action="diferencias",
                    conversation_id=self.conversation_id,
                    input_data={"procedure_id": self.procedure_id, "codigos": difs},
                )
            log.info("motor_sombra_diferencias", procedure_id=self.procedure_id, codigos=difs)
        except Exception as exc:
            log.warning("motor_sombra_volcado_fallo", error=type(exc).__name__)
