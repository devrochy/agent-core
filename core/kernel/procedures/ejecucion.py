"""Motor de procedimientos con estado (HU-AOS-05, ADR-0112 §2, ADR-0113 §1, ADR-0114).

El LLM propone y el kernel decide: con una tarea en foco, solo se ofrecen y ejecutan las
acciones de su etapa (más las del core), los datos se validan en el servidor (tipo, opciones,
formato y el *resolver* de la habilidad), lo que compromete espera una confirmación explícita y
la tarea se cierra con un evento de dominio del outbox.

Cuándo actúa:
- **Activo** con `tenants.aos_procedures` (exige `aos_runtime = 'v3'`, CHECK de 0070).
- **Sombra** con `aos_runtime = 'v3'` y `aos_procedures` apagada: el turno sigue con el flujo de
  siempre y el motor solo calcula y registra diferencias (`sombra.py`), sin cambiar nada de lo que
  ve el modelo ni la respuesta.
- Apagado en cualquier otro caso: `contexto_del_motor` devuelve None tras la misma lectura de
  banderas de antes y el turno es idéntico.

Transacciones y efectos (revisión del agente `arquitecto`):
- Antes del efecto, fail-closed: el chequeo de etapa y de confirmación y `action_requested` se
  escriben bajo el lock de la conversación; si la escritura falla, la acción se deniega.
- Después del efecto, el resultado llega siempre al modelo: un fallo al registrar se loguea. Nunca
  hay un lock de BD abierto mientras corre una acción (la clave de idempotencia de la acción es la
  del `tool_call` y un reintento del modelo saldría con otra).
- Orden de bloqueos: conversación → tareas (el mismo de `state.py` y `episodio.py`), también en el
  consumer y en el worker. Cada decisión relee la proyección; lo que se calculó al armar el turno
  solo sirve para el prompt y el filtro inicial de acciones.
- Precondiciones y transiciones solo dentro del turno, con el ejecutor que inyecta el loop (pasa por
  los permisos, la salud del pack y la auditoría de siempre). El consumer y el worker solo fijan
  datos, confirmaciones y cierres; el turno siguiente avanza.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from agentcore_contracts import Procedure, Slot, Stage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import ConversationTask
from core.kernel import state
from core.kernel.procedures import comandos, motor, registro
from core.kernel.procedures.catalogo import CORE_OWNER
from core.kernel.procedures.textos import confirmacion_alta, error_de_resolver, mensaje
from core.kernel.state import Evento, Proyeccion
from core.shared import turno as turno_vigente
from core.shared.logger import get_logger
from core.shared.tenant import get_current_tenant_id

log = get_logger(__name__)

# (nombre, args, clave estable) -> resultado: lo inyecta el loop (envuelve su ejecución segura de
# acciones; la clave da la idempotencia de la precondición entre reintentos).
Ejecutor = Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]]
Modo = Literal["activo", "sombra"]

_TIMEOUT_RESOLVER_S = 3.0
# Presupuesto de todos los resolvers de un `actualizar_tarea` (corren con la conversación bloqueada).
_PRESUPUESTO_RESOLVERS_S = 5.0
# Marca de "el último inbound fue un botón": ese turno no confirma por texto (el título del botón
# no es un sí escrito por el cliente).
_MARCA_INTERACTIVO = "aos:interactivo"
_TTL_MARCA_S = 900
_MAX_AVANCES = 31  # una vuelta por etapa (el contrato admite hasta 30)


class _NoGobierna(Exception):
    """La tarea en foco no la gobierna el motor (no hay foco o el binario no trae su versión)."""


@dataclass(frozen=True)
class _Foco:
    fila: ConversationTask
    tarea: Proyeccion
    proc: Procedure
    stage: Stage


@dataclass
class ResultadoComando:
    ok: bool
    errores: list[str] = field(default_factory=list)
    acciones: list[str] = field(default_factory=list)
    # Tarjeta de estado vigente al terminar el comando (HU-AOS-06): lo que el modelo ve después.
    tarjeta: str = ""


@dataclass
class Decision:
    """El motor no deja ejecutar la acción: lo que vuelve al modelo y cómo se audita."""

    resultado: dict[str, Any]
    auditoria: str  # denied | confirmation_required
    args_hash: str = ""


@dataclass
class Previo:
    """Lo que `antes_de_accion` resolvió con la proyección leída bajo lock.

    - `decision`: si no es None, la acción no se ejecuta.
    - `task_id`/`stage`: la tarea y la etapa que gobiernan la acción (None = no la gobierna el
      motor: aplica la confirmación del binding v2 de siempre). `tras_accion` solo registra el
      resultado en esa misma tarea y etapa."""

    decision: Decision | None = None
    task_id: uuid.UUID | None = None
    stage: str | None = None

    @property
    def gobernada(self) -> bool:
        return self.task_id is not None


def _secreto() -> str:
    from core.config import get_settings

    return get_settings().app_secret_key


def _clave_marca(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> str:
    return f"{_MARCA_INTERACTIVO}:{tenant_id}:{conversation_id}"


async def _marcar_interactivo(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    try:
        from core.memory.working import get_redis

        await get_redis().set(_clave_marca(tenant_id, conversation_id), "1", ex=_TTL_MARCA_S)
    except Exception as exc:
        log.warning("motor_marca_interactivo_fallo", error=type(exc).__name__)


async def _tomar_marca_interactivo(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
    """True si el inbound del turno fue un botón. Si Redis falla, se asume que sí (fail-closed:
    el turno no confirma por texto)."""
    try:
        from core.memory.working import get_redis

        return bool(await get_redis().getdel(_clave_marca(tenant_id, conversation_id)))
    except Exception as exc:
        log.warning("motor_marca_interactivo_lectura_fallo", error=type(exc).__name__)
        return True


async def _ultimo_inbound_respondido(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> Any:
    """Hora del último mensaje del cliente que ya tuvo respuesta: una confirmación por texto solo
    vale para una petición hecha después de él (en el turno inmediatamente anterior)."""
    return (
        await session.execute(
            text(
                "SELECT max(i.created_at) FROM messages i WHERE i.tenant_id = :t AND i.conversation_id = :c "
                "AND i.deleted_at IS NULL AND i.direction = 'inbound' AND i.created_at < ("
                "  SELECT max(o.created_at) FROM messages o WHERE o.tenant_id = :t AND o.conversation_id = :c "
                "  AND o.deleted_at IS NULL AND o.direction = 'outbound')"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------- utilidades de BD


@asynccontextmanager
async def _con_lock(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Una transacción con la fila de la conversación bloqueada (conversación → tareas)."""
    from core.db.session import get_session

    async with get_session() as session:
        await _bloquear_conversacion(session, tenant_id, conversation_id)
        yield session


async def _bloquear_conversacion(session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
    await session.execute(
        text("SELECT 1 FROM conversations WHERE tenant_id = :t AND id = :c FOR UPDATE"),
        {"t": tenant_id, "c": conversation_id},
    )


async def _foco(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, permitidos: frozenset[str]
) -> _Foco | None:
    filas = await state.tareas_abiertas(session, tenant_id=tenant_id, conversation_id=conversation_id)
    fila = next((f for f in filas if f.status == "active" and f.stack_position == 0), None)
    if fila is None:
        return None
    tarea = state.proyeccion_de(fila)
    proc = registro.definicion(tarea.procedure_id, tarea.procedure_version)
    stage = motor.etapa(proc, tarea.stage) if proc is not None else None
    if proc is None or stage is None:
        log.info("motor_tarea_sin_definicion", procedure_id=tarea.procedure_id, version=tarea.procedure_version)
        return None
    if not registro.gobernable(proc, permitidos):
        # La habilidad se deshabilitó o se auto-desactivó para el negocio (ADR-0024).
        log.info("motor_tarea_de_habilidad_no_disponible", procedure_id=tarea.procedure_id)
        return None
    return _Foco(fila=fila, tarea=tarea, proc=proc, stage=stage)


async def _eventos(session: AsyncSession, foco: _Foco) -> list[Evento]:
    return await state.eventos_de_tarea(
        session, tenant_id=foco.fila.tenant_id, conversation_id=foco.fila.conversation_id, task_id=foco.fila.id
    )


async def _registrar(
    session: AsyncSession, fila: ConversationTask, tipo: str, data: Mapping[str, Any], actor: str
) -> ConversationTask:
    return await state.registrar(
        session,
        tenant_id=fila.tenant_id,
        conversation_id=fila.conversation_id,
        task_id=fila.id,
        tipo=tipo,
        data=data,
        actor=actor,
        turn_id=turno_vigente.turno_id(),
    )


def _evento_por_seq(eventos: Sequence[Evento], seq: Any) -> Evento | None:
    return next((e for e in eventos if e.seq == seq), None)


async def _confirmar_pendiente(session: AsyncSession, foco: _Foco, actor: str = "kernel") -> None:
    """`confirmed` de lo que la tarea tiene pendiente, con el hash de args de la petición."""
    pendiente = foco.tarea.pending_confirmation or {}
    peticion = _evento_por_seq(await _eventos(session, foco), pendiente.get("seq"))
    data: dict[str, Any] = {"target": {"kind": pendiente.get("kind"), "name": pendiente.get("name")}}
    if peticion is not None and peticion.data.get("args_hash"):
        data["args_hash"] = peticion.data["args_hash"]
    await _registrar(session, foco.fila, "confirmed", data, actor)


async def _devolver_control(session: AsyncSession, fila: ConversationTask, proc: Procedure | None) -> None:
    """Al cerrar la tarea en foco, la primera en pausa vuelve al foco (`returnControl`)."""
    if proc is not None and proc.completion is not None and not proc.completion.returnControl:
        return
    filas = await state.tareas_abiertas(session, tenant_id=fila.tenant_id, conversation_id=fila.conversation_id)
    pausada = next((f for f in filas if f.status == "suspended"), None)
    if pausada is not None and not any(f.status == "active" and f.stack_position == 0 for f in filas):
        await _registrar(session, pausada, "resumed", {}, "kernel")


async def _completar(session: AsyncSession, foco_fila: ConversationTask, proc: Procedure | None, motivo: str) -> None:
    await _registrar(session, foco_fila, "completed", {"reason": motivo}, "kernel")
    await _devolver_control(session, foco_fila, proc)


# ---------------------------------------------------------------- validación de datos


async def _validar_dato(
    proc: Procedure, nombre: str, slot: Slot, valor: Any, ctx: Any, datos: dict[str, Any], permitidos: frozenset[str]
) -> tuple[Any, str | None]:
    """Valor normalizado o el error de negocio en lenguaje claro."""
    v, rechazo = motor.validar_valor(slot, valor)
    if rechazo is not None:
        if rechazo.codigo == "enum_invalido":
            return None, mensaje("enum_invalido", dato=nombre)
        return None, mensaje(rechazo.codigo, dato=nombre, esperado=rechazo.detalle)
    if slot.resolver is None:
        return v, None
    r = registro.resolver(slot.resolver, permitidos)
    if r is None or r.validar is None:
        return None, mensaje("resolver_no_disponible", dato=nombre)
    try:
        async with asyncio.timeout(_TIMEOUT_RESOLVER_S):
            veredicto = await r.validar(v, ctx, dict(datos))
    except Exception as exc:
        log.warning("motor_resolver_fallo", procedure_id=proc.id, resolver=slot.resolver, error=type(exc).__name__)
        return None, mensaje("resolver_no_disponible", dato=nombre)
    if not veredicto.ok:
        return None, mensaje("resolver_rechazo", dato=nombre, error=error_de_resolver(veredicto.error))
    return (veredicto.value if veredicto.value is not None else v), None


def _contexto_de_herramienta(conversation_id: uuid.UUID, customer_id: uuid.UUID, channel: str, clave: str) -> Any:
    from core.mcp.host import new_tool_context

    return new_tool_context(
        conversation_id=conversation_id, customer_id=customer_id, idempotency_key=clave, channel=channel
    )


# ---------------------------------------------------------------- contexto del turno


class ContextoMotor:
    """El motor durante un turno del loop (activo o en sombra)."""

    def __init__(
        self,
        *,
        modo: Modo,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        customer_id: uuid.UUID,
        channel: str,
        procedimientos: dict[str, Procedure],
        herramientas_base: list[Any],
        candidatas: list[Any],
        tarjeta: str = "",
        foco: _Foco | None = None,
        texto_cliente: str = "",
        seq_al_empezar: dict[uuid.UUID, int] | None = None,
        permitidos: frozenset[str] = frozenset(),
        confirmar_tambien: frozenset[str] = frozenset(),
        abiertas: frozenset[str] = frozenset(),
    ) -> None:
        self.modo: Modo = modo
        self.tenant_id = tenant_id
        self.conversation_id = conversation_id
        self.customer_id = customer_id
        self.channel = channel
        self.procedimientos = procedimientos
        self._permitidos = permitidos
        # Acciones con `requiresConfirmation` en el binding del negocio: el motor también las
        # confirma (con su puerta, atada a los args), además de las que pide el procedimiento.
        self._confirmar_tambien = confirmar_tambien
        self.tarjeta = tarjeta
        self.texto_cliente = texto_cliente
        self.ejecutor: Ejecutor | None = None
        self._base = list(herramientas_base)
        self._candidatas = list(candidatas)
        self._core = frozenset(t.name for t in self._base if getattr(t, "plugin_id", "") == CORE_OWNER)
        self._foco = foco
        self._seq_al_empezar = dict(seq_al_empezar or {})
        # Procedimientos con una tarea abierta: no son candidatos de `iniciar_procedimiento`.
        self._abiertas = frozenset(abiertas)
        self._cambio = False
        self._sombra: Any = None
        if modo == "sombra":
            from core.kernel.procedures.sombra import Sombra

            self._sombra = Sombra(tenant_id, conversation_id, procedimientos, self._core)

    # -- lo que ve el modelo
    @property
    def en_foco(self) -> bool:
        return self.modo == "activo" and self._foco is not None

    def herramientas(self) -> list[Any]:
        """Acciones a ofrecer: las de siempre sin foco; con foco, las de la etapa más las del core
        que el turno ya ofrecía (las declarativas del negocio no: fail-closed). Activo, además, los
        comandos del hilo que aplican al estado (HU-AOS-06)."""
        if self.modo != "activo":
            return list(self._base)
        if self._foco is None:
            # Una tool declarativa del negocio con el nombre de un comando no se ofrece.
            salida = [t for t in self._base if t.name not in comandos.NOMBRES]
        else:
            permitidas = set(self._foco.stage.actions)
            salida = [t for t in self._candidatas if t.name in permitidas]
            nombres = {t.name for t in salida}
            salida += [t for t in self._base if t.name in self._core and t.name not in nombres]
        return salida + self.comandos()

    # -- comandos del hilo como tools del LLM (HU-AOS-06)
    def _candidatos(self) -> list[Procedure]:
        """Procedimientos que se pueden iniciar: los disponibles sin una tarea abierta. Es la misma
        lista del enum de `iniciar_procedimiento` y la que comprueba el motor."""
        return [p for pid, p in sorted(self.procedimientos.items()) if pid not in self._abiertas]

    def _par_en_foco(self) -> tuple[Proyeccion, Procedure] | None:
        return (self._foco.tarea, self._foco.proc) if self._foco is not None else None

    def _comandos_aplicables(self) -> list[str]:
        if self.modo != "activo":
            return []
        return comandos.aplicables(foco=self._par_en_foco(), candidatos=self._candidatos())

    def comandos(self) -> list[Any]:
        """Los comandos que aplican al estado, como `MCPTool` (vacío fuera del modo activo)."""
        nombres = self._comandos_aplicables()
        if not nombres:
            return []
        candidatos = self._candidatos()
        if nombres[0] == comandos.INICIAR:
            log.info("motor_candidatos_de_inicio", cantidad=len(candidatos))
        return comandos.herramientas(nombres, foco=self._par_en_foco(), candidatos=candidatos)

    def es_comando(self, nombre: str) -> bool:
        """¿Lo despacha el motor? Solo activo y solo los nombres reservados de los comandos."""
        return self.modo == "activo" and nombre in comandos.NOMBRES

    async def ejecutar_comando(self, nombre: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Aplica un comando del modelo y devuelve el `CommandResult` del design (§5.2): `ok`, la
        tarjeta actualizada, los errores en lenguaje claro y las acciones de la etapa vigente.

        Un comando que no aplica al estado actual (no se ofreció o el estado cambió en el lote) o
        con argumentos fuera del schema no se aplica. Nunca lanza."""
        try:
            error = comandos.argumentos_invalidos(nombre, args)
            if error is None and nombre not in self._comandos_aplicables():
                error = comandos.texto("comando_no_disponible", comando=nombre)
            if error is not None:
                r = ResultadoComando(False, [error])
            else:
                r = await self.aplicar_comando(nombre, args)
        except Exception as exc:
            log.warning("motor_comando_fallo", comando=nombre, error=type(exc).__name__)
            r = ResultadoComando(False, [mensaje("error_interno")])
        await self._releer_seguro()
        return {
            "ok": r.ok,
            "state_card": self.tarjeta,
            "errors": list(r.errores),
            "allowed_actions": self._acciones(),
        }

    # -- antes y después de cada acción
    async def antes_de_accion(self, accion: str, args: dict[str, Any]) -> Previo:
        """Decide con la proyección leída bajo lock (nunca con la del inicio del turno)."""
        if self.modo != "activo" or accion in self._core:
            return Previo()
        h = motor.hash_args(args)
        try:
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None:
                    return Previo()
                if accion not in foco.stage.actions:
                    return Previo(Decision(
                        {"error": mensaje("fuera_de_etapa", accion=accion, etapa=foco.stage.id)}, "denied", h
                    ))
                gobernada = Previo(task_id=foco.fila.id, stage=foco.stage.id)
                if motor.requiere_confirmacion(foco.stage, accion) or accion in self._confirmar_tambien:
                    eventos = await _eventos(s, foco)
                    if not motor.accion_confirmada(eventos, accion, h):
                        pendiente = foco.tarea.pending_confirmation or {}
                        peticion = _evento_por_seq(eventos, pendiente.get("seq"))
                        misma = (
                            pendiente.get("kind") == "action"
                            and pendiente.get("name") == accion
                            and peticion is not None
                            and peticion.data.get("args_hash") == h
                        )
                        if not misma:
                            await _registrar(
                                s, foco.fila, "confirmation_requested",
                                {"target": {"kind": "action", "name": accion}, "args_hash": h}, "kernel",
                            )
                        gobernada.decision = Decision(
                            {"status": "confirmation_required", "message": mensaje("confirmacion_requerida", accion=accion)},
                            "confirmation_required",
                            h,
                        )
                        return gobernada
                await _registrar(s, foco.fila, "action_requested", {"action": accion, "args_hash": h}, "llm")
            return gobernada
        except Exception as exc:
            log.warning("motor_antes_de_accion_fallo", accion=accion, error=type(exc).__name__)
            return Previo(Decision({"error": mensaje("error_interno")}, "denied", h))

    async def tras_accion(
        self, accion: str, args: dict[str, Any], resultado: dict[str, Any], previo: Previo | None = None
    ) -> dict[str, Any]:
        """Registra el resultado en la tarea que autorizó la acción, fija los datos del servidor y
        avanza. Nunca lanza."""
        if self.modo == "sombra":
            await self._sombra.observar(accion, args, resultado)
            return resultado
        if self.modo != "activo" or previo is None or not previo.gobernada:
            return resultado
        escalar = False
        task_id: uuid.UUID | None = None
        try:
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None or foco.fila.id != previo.task_id or foco.stage.id != previo.stage:
                    # La tarea cerró o cambió de etapa mientras corría la acción: el resultado no
                    # se anota en otra tarea.
                    log.warning("motor_resultado_sin_su_tarea", accion=accion)
                    return resultado
                task_id = foco.fila.id
                ok = not (isinstance(resultado, dict) and resultado.get("error"))
                data: dict[str, Any] = {"action": accion}
                if ok:
                    candidatos = motor.candidatos_de_resultado(foco.proc, accion, resultado)
                    if candidatos:
                        data["candidates"] = candidatos
                await _registrar(s, foco.fila, "action_succeeded" if ok else "action_failed", data, "tool")
                if ok:
                    await self._datos_de_accion(s, foco, accion, resultado)
                recovery = foco.proc.recovery
                if not ok and recovery is not None and recovery.actionError == "relevo_humano":
                    await _registrar(s, foco.fila, "escalated", {"reason": "action_error"}, "kernel")
                    escalar = True
        except Exception as exc:
            log.warning("motor_tras_accion_fallo", accion=accion, error=type(exc).__name__)
            return resultado
        if escalar:
            return {**resultado, "escalate": True}
        if task_id is not None:
            await self._avanzar_seguro(task_id)
        return resultado

    async def _datos_de_accion(self, s: AsyncSession, foco: _Foco, accion: str, resultado: Mapping[str, Any]) -> None:
        """Datos con `setBy: action:<acción>`: el nombre del dato es la clave del resultado."""
        for nombre, slot in foco.proc.slots.items():
            if slot.setBy != f"action:{accion}" or resultado.get(nombre) is None:
                continue
            valor, rechazo = motor.validar_valor(slot, resultado[nombre])
            if rechazo is not None:
                log.warning("motor_dato_de_accion_invalido", procedure_id=foco.proc.id, dato=nombre, codigo=rechazo.codigo)
                continue
            try:
                await _registrar(
                    s, foco.fila, "slot_set",
                    {"slot": nombre, "value": valor, "source": f"action:{accion}", "data_class": slot.dataClass},
                    "tool",
                )
            except state.TransicionInvalida:
                log.warning("motor_dato_de_accion_rechazado", procedure_id=foco.proc.id, dato=nombre)

    async def tras_lote(self) -> bool:
        """Al terminar un lote de acciones: vuelca la sombra y dice si cambiaron las acciones a
        ofrecer (la etapa o el foco cambiaron en el lote)."""
        if self._sombra is not None:
            await self._sombra.volcar()
        if not self._cambio:
            return False
        self._cambio = False
        await self._releer_seguro()
        return True

    async def _releer_seguro(self) -> bool:
        """Relee el foco, las tareas abiertas y la tarjeta (lo que ofrece la vuelta siguiente y lo
        que devuelve un comando). No toca `_cambio`: el loop recalcula las tools con `tras_lote`.
        False si la lectura falló: la tarjeta queda vacía (nunca la de antes del cambio)."""
        if self.modo != "activo":
            return True
        try:
            from core.db.session import get_session
            from core.kernel.tarjeta import renderizar_tarjeta

            async with get_session() as s:
                filas = await state.tareas_abiertas(s, tenant_id=self.tenant_id, conversation_id=self.conversation_id)
                self._abiertas = frozenset(f.procedure_id for f in filas)
                self.tarjeta = renderizar_tarjeta(
                    [state.proyeccion_de(f) for f in filas], procedimientos=registro.definicion
                )
                self._foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            return True
        except Exception as exc:
            log.warning("motor_releer_foco_fallo", error=type(exc).__name__)
            self.tarjeta = ""
            return False

    async def instantaneas(self) -> list[dict[str, Any]] | None:
        """Estado de las tareas para el verificador (ADR-0119, HU-AOS-06), o None fuera del modo
        activo o si la lectura falla (el verificador sigue como sin estado)."""
        if self.modo != "activo":
            return None
        try:
            from core.db.session import get_session

            async with get_session() as s:
                return await instantaneas_de_tareas(s, self.tenant_id, self.conversation_id, self._permitidos)
        except Exception as exc:
            log.warning("motor_instantaneas_fallo", error=type(exc).__name__)
            return None

    # -- avance de etapa
    async def _avanzar_seguro(self, task_id: uuid.UUID) -> None:
        try:
            await self._avanzar(task_id)
        except Exception as exc:
            log.warning("motor_avanzar_fallo", task_id=str(task_id), error=type(exc).__name__)

    async def _avanzar(self, task_id: uuid.UUID) -> None:
        for _ in range(_MAX_AVANCES):
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None or foco.fila.id != task_id:
                    return
                stage, proc, tarea = foco.stage, foco.proc, foco.tarea
                eventos_etapa = motor.eventos_de_la_etapa(await _eventos(s, foco))
                fila_id = foco.fila.id
                if motor.es_terminal(stage):
                    if not (proc.completion and proc.completion.events):
                        await _completar(s, foco.fila, proc, "etapa_final")
                        self._cambio = True
                    return
                if not motor.salida_cumplida(proc, stage, tarea, eventos_etapa):
                    return
                valores_hash = motor.hash_args(motor.valores(tarea))
                if motor.puerta_de_salida(stage) and not motor.etapa_confirmada(eventos_etapa, stage, valores_hash):
                    pendiente = tarea.pending_confirmation or {}
                    peticion = _evento_por_seq(eventos_etapa, pendiente.get("seq"))
                    misma = (
                        pendiente.get("kind") == "stage" and pendiente.get("name") == stage.id
                        and peticion is not None and peticion.data.get("args_hash") == valores_hash
                    )
                    if not misma:  # un dato cambió después del sí: se vuelve a pedir
                        await _registrar(
                            s, foco.fila, "confirmation_requested",
                            {"target": {"kind": "stage", "name": stage.id}, "args_hash": valores_hash}, "kernel",
                        )
                    return
                seq, precondiciones, datos = tarea.last_seq, list(stage.preconditions), motor.valores(tarea)
            # Precondiciones sin lock: corren como cualquier acción del turno.
            for pre in precondiciones:
                motivo = await self._precondicion(pre, datos, f"pre-{fila_id}-{seq}-{pre}")
                if motivo is not None:
                    async with _con_lock(self.tenant_id, self.conversation_id) as s:
                        foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                        if foco is not None and foco.fila.id == task_id and foco.tarea.last_seq == seq:
                            await _registrar(s, foco.fila, "precondition_failed", {"action": pre, "reason": motivo}, "kernel")
                    return
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None or foco.fila.id != task_id or foco.tarea.last_seq != seq:
                    return  # otra escritura movió la tarea mientras corrían las precondiciones
                destino = motor.siguiente(foco.proc, foco.stage)
                if destino is None:
                    if not (foco.proc.completion and foco.proc.completion.events):
                        await _completar(s, foco.fila, foco.proc, "etapa_final")
                        self._cambio = True
                    return  # con eventos de cierre, la tarea espera el evento
                await _registrar(
                    s, foco.fila, "stage_entered", {"from_stage": foco.stage.id, "to_stage": destino.id}, "kernel"
                )
                self._cambio = True

    async def _precondicion(self, accion: str, datos: Mapping[str, Any], clave: str) -> str | None:
        """None si se cumple; si no, el código del motivo. Args = datos de la tarea con el nombre
        de una propiedad del input schema de la acción. "Se cumple" = resultado sin `error`, sin
        `ok: false` y que no pide confirmación (una precondición es una lectura)."""
        if self.ejecutor is None:
            return "precondicion_sin_ejecutor"
        from core.mcp.host import mcp_host

        herramienta = mcp_host.get_tool(accion)
        esquema = (herramienta.parameters if herramienta is not None else {}) or {}
        propiedades = esquema.get("properties") or {}
        args = {k: datos[k] for k in propiedades if datos.get(k) is not None}
        if any(r not in args for r in esquema.get("required") or []):
            return "precondicion_sin_datos"
        try:
            resultado = await self.ejecutor(accion, args, clave)
        except Exception as exc:
            log.warning("motor_precondicion_fallo", accion=accion, error=type(exc).__name__)
            return "precondicion_fallida"
        if not isinstance(resultado, dict) or resultado.get("error") or resultado.get("ok") is False:
            return "precondicion_fallida"
        if resultado.get("status") == "confirmation_required":
            return "precondicion_fallida"
        return None

    # -- comandos (los ofrece HU-AOS-06; aquí se aplican)
    async def aplicar_comando(self, nombre: str, args: Mapping[str, Any]) -> ResultadoComando:
        if self.modo != "activo":
            return ResultadoComando(False, [mensaje("sin_tarea")])
        try:
            if nombre == "iniciar_procedimiento":
                return await self._iniciar(str(args.get("procedure_id") or ""))
            if nombre == "actualizar_tarea":
                slots = args.get("slots")
                return await self._actualizar(slots if isinstance(slots, Mapping) else {})
            if nombre == "confirmar_tarea":
                return await self._confirmar()
            if nombre == "cancelar_tarea":
                return await self._cancelar()
        except _NoGobierna:
            return ResultadoComando(False, [mensaje("sin_tarea")])
        except Exception as exc:
            log.warning("motor_comando_fallo", comando=nombre, error=type(exc).__name__)
            return ResultadoComando(False, [mensaje("error_interno")])
        raise ValueError(f"comando desconocido: {nombre}")

    def _acciones(self) -> list[str]:
        """Acciones de la etapa en foco que de verdad se ofrecen (las filtradas por conexión o por
        la salud del pack no)."""
        if self._foco is None:
            return []
        ofrecidas = {t.name for t in self._candidatas}
        return [a for a in self._foco.stage.actions if a in ofrecidas]

    async def _terminar_comando(self, task_id: uuid.UUID | None) -> ResultadoComando:
        # `_cambio` queda encendido: el `tras_lote` del loop recalcula las tools de la vuelta
        # siguiente (las de la etapa nueva y los comandos que ahora aplican).
        self._cambio = True
        if task_id is not None:
            await self._avanzar_seguro(task_id)
        await self._releer_seguro()
        return ResultadoComando(True, [], self._acciones(), self.tarjeta)

    async def _iniciar(self, procedure_id: str) -> ResultadoComando:
        proc = self.procedimientos.get(procedure_id)
        if proc is None or not await self._sigue_sano(proc):
            return ResultadoComando(False, [mensaje("procedimiento_no_disponible")])
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            abiertas = await state.tareas_abiertas(s, tenant_id=self.tenant_id, conversation_id=self.conversation_id)
            if any(f.status == "active" and f.stack_position == 0 for f in abiertas) or any(
                f.procedure_id == proc.id for f in abiertas
            ):
                return ResultadoComando(False, [mensaje("tarea_en_curso")])
            fila = await state.iniciar(
                s, tenant_id=self.tenant_id, conversation_id=self.conversation_id, procedure_id=proc.id,
                procedure_version=proc.version, stage=proc.stages[0].id, actor="kernel",
                turn_id=turno_vigente.turno_id(),
            )
            task_id = fila.id
        self._seq_al_empezar[task_id] = 1
        return await self._terminar_comando(task_id)

    async def _exigir_foco_sano(self) -> None:
        """Los comandos que escriben no gobiernan la tarea de un pack que se auto-desactivó a mitad
        del turno (sus resolvers no se llaman)."""
        if self._foco is not None and not await self._sigue_sano(self._foco.proc):
            raise _NoGobierna

    async def _sigue_sano(self, proc: Procedure) -> bool:
        """La habilidad del procedimiento sigue sana para el negocio (un pack que se
        auto-desactivó a mitad del turno no abre tareas nuevas). Fail-closed."""
        try:
            from core.plugin_manager import plugin_manager

            rotos = set(await plugin_manager.disabled_plugins_for_tenant(self.tenant_id))
        except Exception as exc:
            log.warning("motor_salud_de_packs_fallo", error=type(exc).__name__)
            return False
        return registro.gobernable(proc, self._permitidos - rotos)

    async def _actualizar(self, slots: Mapping[str, Any]) -> ResultadoComando:
        await self._exigir_foco_sano()
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            if foco is None:
                raise _NoGobierna
            proc = foco.proc
            # El servidor usa la misma lista que el schema del comando: un dato con `setBy`, de
            # ubicación o archivo, o con candidatos no lo escribe el modelo.
            escribibles = comandos.escribibles(proc)
            errores: list[str] = []
            validos: dict[str, Any] = {}
            ctx = _contexto_de_herramienta(
                self.conversation_id, self.customer_id, self.channel, f"{self.conversation_id}:resolver"
            )
            datos = motor.valores(foco.tarea)
            try:
                # Un presupuesto para todos los resolvers: corren con la conversación bloqueada.
                async with asyncio.timeout(_PRESUPUESTO_RESOLVERS_S):
                    for nombre, valor in list(slots.items())[:40]:
                        slot = proc.slots.get(str(nombre))
                        if slot is None:
                            errores.append(mensaje("dato_desconocido", dato=_nombre_seguro(nombre)))
                            continue
                        if slot.setBy is not None or str(nombre) not in escribibles:
                            errores.append(mensaje("dato_del_servidor", dato=nombre))
                            continue
                        v, error = await _validar_dato(
                            proc, nombre, slot, valor, ctx, {**datos, **validos}, self._permitidos
                        )
                        if error is not None:
                            errores.append(error)
                        else:
                            validos[nombre] = v
            except TimeoutError:
                return ResultadoComando(False, [mensaje("resolver_no_disponible", dato="los datos")], list(foco.stage.actions))
            if errores:  # todo o nada: el modelo corrige, nunca se adivina
                return ResultadoComando(False, errores, list(foco.stage.actions))
            por_confirmar: str | None = None
            for nombre, v in validos.items():
                fila = await _registrar(
                    s, foco.fila, "slot_set",
                    {"slot": nombre, "value": v, "source": "llm", "data_class": proc.slots[nombre].dataClass}, "llm",
                )
                if proc.slots[nombre].confirm and por_confirmar is None:
                    por_confirmar = nombre
                foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=proc, stage=foco.stage)
            if por_confirmar is not None and foco.tarea.pending_confirmation is None:
                await _registrar(
                    s, foco.fila, "confirmation_requested", {"target": {"kind": "slot", "name": por_confirmar}}, "kernel"
                )
            task_id = foco.fila.id
        return await self._terminar_comando(task_id)

    async def _confirmar(self) -> ResultadoComando:
        """Exige la evidencia del turno, que pone el kernel (nunca un argumento del LLM): un "sí"
        explícito de confianza alta del cliente, posterior a la petición de confirmación."""
        await self._exigir_foco_sano()
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            if foco is None:
                raise _NoGobierna
            pendiente = foco.tarea.pending_confirmation
            if not pendiente:
                return ResultadoComando(False, [mensaje("sin_confirmacion_pendiente")], list(foco.stage.actions))
            pedida_en_este_turno = int(pendiente.get("seq") or 0) > self._seq_al_empezar.get(foco.fila.id, 0)
            peticion = _evento_por_seq(await _eventos(s, foco), pendiente.get("seq"))
            respondido = await _ultimo_inbound_respondido(s, self.tenant_id, self.conversation_id)
            # El sí escrito contesta la petición del turno inmediatamente anterior, no una vieja.
            del_turno_anterior = (
                peticion is not None and respondido is not None and peticion.created_at > respondido
            )
            if pedida_en_este_turno or not del_turno_anterior or not confirmacion_alta(self.texto_cliente):
                return ResultadoComando(False, [mensaje("falta_confirmacion")], list(foco.stage.actions))
            await _confirmar_pendiente(s, foco)
            task_id = foco.fila.id
        return await self._terminar_comando(task_id)

    async def _cancelar(self) -> ResultadoComando:
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            if foco is None:
                raise _NoGobierna
            await _registrar(s, foco.fila, "cancelled", {"reason": "pedido_del_cliente"}, "llm")
            await _devolver_control(s, foco.fila, foco.proc)
        return await self._terminar_comando(None)


def payload_de_confirmacion(tenant_id: uuid.UUID, task_id: uuid.UUID, seq: int, args_hash: str = "") -> str:
    """Id firmado del botón que confirma la petición `seq` de una tarea (lo envía HU-AOS-09)."""
    return motor.payload_boton_confirmar(_secreto(), str(tenant_id), str(task_id), seq, args_hash)


_MAX_INSTANTANEAS = 20


def _datos_de_cierre(proc: Procedure | None) -> frozenset[str]:
    """Datos que el procedimiento declara como respaldo de sus afirmaciones de estado o de link
    (`claims[].requires` y `mustMatch` con `slot:`). Solo esos respaldan un cierre desde el
    estado: un dato de una lectura (`estado_pago = pendiente`) no respalda "tu pago quedó
    registrado" en los turnos siguientes."""
    if proc is None:
        return frozenset()
    datos: set[str] = set()
    for claim in proc.claims:
        if claim.kind == "amount":
            continue
        for fuente in [*claim.mustMatch, *([claim.requires] if claim.requires else [])]:
            if fuente.startswith("slot:"):
                datos.add(fuente.removeprefix("slot:"))
    return frozenset(datos)


async def instantaneas_de_tareas(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    permitidos: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Tareas del episodio en curso para el verificador (ADR-0119, HU-AOS-06), solo datos.

    Son las abiertas y las que cambiaron desde el inicio del episodio (`episode_started_at`).
    Cada una: `procedure_id`, `status`, `slots` (`{nombre: {value, source}}`) y `cierre`:
    si puede respaldar una afirmación de cierre. Solo pueden la tarea en foco y las que se
    completaron después de la última respuesta del asistente (un cierre por evento entre
    turnos): así el pedido de la compra anterior del episodio no respalda "tu pedido quedó
    creado" de la siguiente. Montos y referencias sí salen de todas. Además, solo respaldan
    cierres los datos que una afirmación del procedimiento declara (`respalda_cierre`) y solo si
    el motor gobierna la tarea (su definición está en el binario y su habilidad, habilitada)."""
    fila_conv = (
        await session.execute(
            text(
                "SELECT c.episode_started_at, (SELECT max(o.created_at) FROM messages o "
                "WHERE o.tenant_id = c.tenant_id AND o.conversation_id = c.id AND o.deleted_at IS NULL "
                "AND o.direction = 'outbound') FROM conversations c WHERE c.tenant_id = :t AND c.id = :c"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).first()
    if fila_conv is None:
        return []
    inicio, ultima_respuesta = fila_conv[0], fila_conv[1]
    filtro = ConversationTask.status.in_(state.ABIERTOS)
    if inicio is not None:
        filtro = filtro | (ConversationTask.updated_at >= inicio)
    filas = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.conversation_id == conversation_id,
                ConversationTask.parent_task_id.is_(None),
                filtro,
            )
            .order_by(ConversationTask.updated_at.desc(), ConversationTask.id)
            .limit(_MAX_INSTANTANEAS)
        )
    ).scalars().all()
    salida: list[dict[str, Any]] = []
    for fila in filas:
        proc = registro.definicion(fila.procedure_id, fila.procedure_version)
        gobernada = proc is not None and registro.gobernable(proc, permitidos)
        de_cierre = _datos_de_cierre(proc) if gobernada else frozenset()
        en_foco = fila.status == "active" and fila.stack_position == 0
        cerrada_ahora = fila.status == "completed" and (
            ultima_respuesta is None or (fila.completed_at is not None and fila.completed_at > ultima_respuesta)
        )
        salida.append({
            "procedure_id": fila.procedure_id,
            "status": fila.status,
            "cierre": gobernada and (en_foco or cerrada_ahora),
            "slots": {
                str(n): {
                    "value": (m or {}).get("value"),
                    "source": (m or {}).get("source"),
                    "respalda_cierre": str(n) in de_cierre,
                }
                for n, m in (fila.slots or {}).items()
            },
        })
    return salida


def _nombre_seguro(nombre: Any) -> str:
    """Un nombre de dato que viene del modelo, acotado y sin saltos (va dentro de un mensaje)."""
    return " ".join(str(nombre).split())[:40] or "?"


async def contexto_del_motor(
    conversation_id: uuid.UUID,
    *,
    customer_id: uuid.UUID,
    channel: str,
    enabled: set[str],
    disabled: set[str],
    herramientas_base: list[Any],
    candidatas: list[Any],
    texto_cliente: str,
    confirmar_tambien: frozenset[str] = frozenset(),
) -> ContextoMotor | None:
    """El motor del turno, o None si el negocio no lo usa (misma lectura de banderas de siempre).

    Activo: abre el episodio (expira las tareas viejas), lee las tareas abiertas, arma la tarjeta
    con las definiciones de los procedimientos y fija la tarea en foco. Fail-closed: un error de
    base o de los playbooks sube por el camino de error del turno."""
    from core.db.session import get_session
    from core.kernel.banderas import banderas_del_negocio
    from core.kernel.episodio import DEFAULT_IDLE_MINUTES, abrir_episodio
    from core.kernel.tarjeta import renderizar_tarjeta
    from core.shared import release_vigente

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    permitidos = frozenset(set(enabled) - set(disabled))
    comunes: dict[str, Any] = {
        "tenant_id": tenant_id, "conversation_id": conversation_id, "customer_id": customer_id,
        "channel": channel, "herramientas_base": herramientas_base, "candidatas": candidatas,
        "permitidos": permitidos,
    }
    async with get_session() as session:
        banderas = await banderas_del_negocio(session, tenant_id)
        if banderas.procedimientos:
            # Un solo episodio por turno: con release (aos_runtime = v3) manda su `idleMinutes`.
            await abrir_episodio(
                session,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                idle_minutes=release_vigente.idle_minutes(DEFAULT_IDLE_MINUTES),
            )
            filas = await state.tareas_abiertas(session, tenant_id=tenant_id, conversation_id=conversation_id)
            tareas = [state.proyeccion_de(f) for f in filas]
            tarjeta = renderizar_tarjeta(tareas, procedimientos=registro.definicion)
            foco = await _foco(session, tenant_id, conversation_id, permitidos)
    if banderas.procedimientos:
        # Si el inbound fue un botón, su título no es un sí escrito: el turno no confirma por texto.
        if await _tomar_marca_interactivo(tenant_id, conversation_id):
            texto_cliente = ""
        return ContextoMotor(
            modo="activo", procedimientos=registro.disponibles(enabled, disabled), tarjeta=tarjeta,
            foco=foco, texto_cliente=texto_cliente, seq_al_empezar={f.id: f.last_seq for f in filas},
            confirmar_tambien=confirmar_tambien, abiertas=frozenset(f.procedure_id for f in filas), **comunes,
        )
    if banderas.runtime == "v3" and registro.hay_procedimientos():
        procs = registro.disponibles(enabled, disabled)
        if procs:
            return ContextoMotor(modo="sombra", procedimientos=procs, **comunes)
    return None


# ---------------------------------------------------------------- entradas sin LLM (consumer)


async def entrada_sin_llm(conversation: Any, fields: Mapping[str, str]) -> None:
    """Datos que el cliente da sin pasar por el LLM (ADR-0113 §1): el botón de confirmación, un
    botón o fila de lista que llena un dato (`setBy: interactive:<prefijo>:`) o el contenido que
    envía (`setBy: content:<tipo>`). Solo fija datos y confirmaciones: el turno avanza.

    Corre en el consumer, después de las políticas y antes del debounce, así el turno (inmediato o
    con debounce) ya ve el dato en la tarjeta. Sin `interactive_id` ni media no consulta nada.
    Best-effort: un fallo se loguea y el turno sigue con el texto de siempre."""
    import json

    interactivo = (fields.get("interactive_id") or "").strip()[:256]
    try:
        media = json.loads(fields.get("media") or "[]")
    except ValueError:
        media = []
    if not interactivo and not media:
        return
    try:
        await _entrada_sin_llm(conversation, interactivo, media if isinstance(media, list) else [])
    except Exception as exc:
        log.warning("motor_entrada_sin_llm_fallo", error=type(exc).__name__)


async def _entrada_sin_llm(conversation: Any, interactivo: str, media: list[Any]) -> None:
    from core.db.session import get_session
    from core.kernel.banderas import banderas_del_negocio
    from core.kernel.episodio import DEFAULT_IDLE_MINUTES, abrir_episodio
    from core.shared import release_vigente

    tenant_id, conversation_id = conversation.tenant_id, conversation.id
    async with get_session() as s:
        if not (await banderas_del_negocio(s, tenant_id)).procedimientos:
            return
    if interactivo:
        await _marcar_interactivo(tenant_id, conversation_id)
    permitidos = frozenset(await registro.packs_permitidos(tenant_id))
    async with get_session() as s:
        # Primero el episodio (bloquea la conversación y expira lo viejo): un botón tras 12 h no
        # resucita una tarea vencida.
        await abrir_episodio(
            s,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            idle_minutes=release_vigente.idle_minutes(DEFAULT_IDLE_MINUTES),
        )
        await _bloquear_conversacion(s, tenant_id, conversation_id)
        foco = await _foco(s, tenant_id, conversation_id, permitidos)
        if foco is None:
            return
        if interactivo:
            boton = motor.leer_boton_confirmar(interactivo)
            if boton is not None:
                pendiente = foco.tarea.pending_confirmation or {}
                peticion = _evento_por_seq(await _eventos(s, foco), pendiente.get("seq"))
                args_hash = str((peticion.data.get("args_hash") if peticion is not None else "") or "")
                if (
                    boton.task_id == str(foco.fila.id)
                    and pendiente.get("seq") == boton.seq
                    and motor.boton_valido(boton, _secreto(), str(tenant_id), args_hash)
                ):
                    await _confirmar_pendiente(s, foco)
                else:
                    log.info("motor_boton_confirmar_vencido", task_id=str(foco.fila.id))
                return
            par = motor.dato_de_boton(foco.proc, interactivo)
            if par is not None:
                await _dato_interactivo(s, foco, conversation, *par, permitidos=permitidos)
        for item in media[:5]:
            if not isinstance(item, Mapping):
                continue
            tipo = motor.tipo_de_contenido(str(item.get("type") or ""))
            nombre = motor.dato_de_contenido(foco.proc, foco.tarea, tipo) if tipo else None
            if nombre is None:
                continue
            slot = foco.proc.slots[nombre]
            valor, rechazo = motor.validar_valor(slot, {"media_id": str(item.get("id") or ""), "mime": str(item.get("type") or "")})
            if rechazo is not None:
                continue
            fila = await _registrar(
                s, foco.fila, "slot_set",
                {"slot": nombre, "value": valor, "source": f"content:{tipo}", "data_class": slot.dataClass}, "kernel",
            )
            foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=foco.proc, stage=foco.stage)


async def _dato_interactivo(
    s: AsyncSession, foco: _Foco, conversation: Any, nombre: str, valor: str, *, permitidos: frozenset[str]
) -> None:
    slot = foco.proc.slots[nombre]
    ctx = _contexto_de_herramienta(conversation.id, conversation.customer_id, conversation.channel, f"{conversation.id}:resolver")
    datos = motor.valores(foco.tarea)
    if slot.candidates is not None:
        fuente = slot.candidates.source
        if fuente.startswith("resolver:"):
            r = registro.resolver(fuente.removeprefix("resolver:"), permitidos)
            try:
                async with asyncio.timeout(_TIMEOUT_RESOLVER_S):
                    opciones = await r.candidatos(ctx, dict(datos)) if r is not None and r.candidatos else None
            except Exception as exc:
                log.warning("motor_candidatos_fallo", procedure_id=foco.proc.id, error=type(exc).__name__)
                opciones = None
        else:
            opciones = motor.ultimos_candidatos(await _eventos(s, foco), nombre)
        if opciones is None or valor not in {str(o) for o in opciones}:
            log.info("motor_opcion_no_ofrecida", procedure_id=foco.proc.id, dato=nombre)
            return
    v, error = await _validar_dato(foco.proc, nombre, slot, valor, ctx, datos, permitidos)
    if error is not None:
        log.info("motor_dato_interactivo_rechazado", procedure_id=foco.proc.id, dato=nombre)
        return
    await _registrar(
        s, foco.fila, "slot_set",
        {"slot": nombre, "value": v, "source": slot.setBy, "data_class": slot.dataClass}, "kernel",
    )


# ---------------------------------------------------------------- cierre por evento (worker)


async def cerrar_por_evento(
    session: AsyncSession, tenant_id: uuid.UUID, topic: str, payload: Mapping[str, Any]
) -> int:
    """Completa la tarea abierta cuyo procedimiento declara el evento y que el evento correlaciona
    por las claves del topic (`CLAVES_DE_CORRELACION`, `motor.correlaciona`). En la sesión del
    despachador del outbox: si falla, el evento se reintenta; una tarea ya cerrada se salta
    (idempotente). Si el evento correlaciona con tareas de más de una conversación, no cierra
    ninguna (ambiguo). Costo cero si ningún procedimiento registrado declara el topic.
    Devuelve cuántas cerró."""
    from core.shared.events import CLAVES_DE_CORRELACION

    claves = CLAVES_DE_CORRELACION.get(topic)
    procs = registro.con_evento(topic) if claves else {}
    if not procs or not claves:
        return 0
    filas = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.status.in_(state.ABIERTOS),
                ConversationTask.procedure_id.in_(list(procs)),
            )
            .order_by(ConversationTask.conversation_id, ConversationTask.id)
            .limit(200)
        )
    ).scalars().all()
    candidatas: list[tuple[uuid.UUID, uuid.UUID, Procedure]] = []
    for fila in filas:
        tarea = state.proyeccion_de(fila)
        proc = registro.definicion(tarea.procedure_id, tarea.procedure_version)
        if proc is not None and motor.correlaciona(proc, tarea, payload, claves):
            candidatas.append((fila.conversation_id, fila.id, proc))
    if not candidatas:
        return 0
    from core.kernel.banderas import banderas_del_negocio

    if not (await banderas_del_negocio(session, tenant_id)).procedimientos:
        return 0
    permitidos = await registro.packs_permitidos(tenant_id)
    candidatas = [c for c in candidatas if registro.gobernable(c[2], permitidos)]
    if len({c[0] for c in candidatas}) > 1:
        log.warning("motor_evento_ambiguo", topic=topic, conversaciones=len({c[0] for c in candidatas}))
        return 0
    cerradas = 0
    for conversation_id, task_id, proc in candidatas:  # ordenadas: conversación → tareas
        await _bloquear_conversacion(session, tenant_id, conversation_id)
        fila = (
            await session.execute(
                select(ConversationTask).where(
                    ConversationTask.tenant_id == tenant_id, ConversationTask.id == task_id
                )
            )
        ).scalar_one()
        await session.refresh(fila)
        if fila.status not in state.ABIERTOS:
            continue
        evento = next(e for e in proc.completion.events if e.split(".", 1)[1] == topic) if proc.completion else topic
        fila = await state.registrar(
            session, tenant_id=tenant_id, conversation_id=conversation_id, task_id=task_id,
            tipo="completed", data={"reason": "evento", "event": evento}, actor="kernel",
        )
        await _devolver_control(session, fila, proc)
        cerradas += 1
    if cerradas:
        log.info("motor_tareas_cerradas_por_evento", topic=topic, count=cerradas)
    return cerradas
