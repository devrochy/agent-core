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
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from agentcore_contracts import Procedure, Slot, Stage
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import ConversationTask
from core.kernel import state
from core.kernel.procedures import comandos, motor, registro
from core.kernel.procedures.catalogo import CORE_OWNER
from core.kernel.procedures.textos import confirmacion_suficiente, error_de_resolver, mensaje
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
# Resumen de confirmación que provee un pack (HU-AOS-07): corre al cerrar el turno, sin lock.
_TIMEOUT_RESUMEN_S = 2.0
_SIN_RESUMEN_DEL_PACK = object()
_MAX_LINEAS_RESUMEN = 20
# Presupuesto de todos los resolvers de un `actualizar_tarea` (corren con la conversación bloqueada).
_PRESUPUESTO_RESOLVERS_S = 5.0
# Marca de "el último inbound fue un botón": ese turno no confirma por texto (el título del botón
# no es un sí escrito por el cliente).
_MARCA_INTERACTIVO = "aos:interactivo"
_TTL_MARCA_S = 900
_MAX_AVANCES = 31  # una vuelta por etapa (el contrato admite hasta 30)
# Vueltas del turno que solo llevaron comandos del hilo y no gastan el tope de vueltas del agente
# (adenda 2026-10-13 de ADR-0113): llevar el estado no es trabajo del cliente. Las negaciones del
# kernel sí cuentan (son el síntoma de un modelo perdido; al agotarse, el kernel pregunta lo que
# falta en vez de escalar).
VUELTAS_LIBRES_DE_COMANDOS = 3


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
      resultado en esa misma tarea y etapa.
    - `args`: los argumentos efectivos (ligados a los datos de la tarea)."""

    decision: Decision | None = None
    task_id: uuid.UUID | None = None
    stage: str | None = None
    # Argumentos efectivos de una acción gobernada (HU-AOS-07): los del modelo con los datos
    # ligados de la tarea (`motor.ligar_argumentos`). El loop ejecuta, audita y registra ESTOS;
    # None = los del modelo tal cual.
    args: dict[str, Any] | None = None

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


def _fresca(peticion: Evento | None, respondido: Any) -> bool:
    """¿La petición de confirmación es posterior al último mensaje del cliente que ya tuvo
    respuesta? (la hizo el turno inmediatamente anterior o este): solo a esa contesta un sí."""
    return peticion is not None and respondido is not None and peticion.created_at > respondido


async def _contesta_el_mensaje_del_kernel(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, peticion: Evento | None
) -> bool:
    """¿El mensaje del cliente de este turno contesta DIRECTO el mensaje que llevó la petición?
    (revisión de seguridad, para el sí a secas de un dato): después de la petición salió UN solo
    mensaje al cliente (no fallido: el del kernel) y el último mensaje del cliente es posterior a
    él. Un aviso proactivo ("¿confirmas tu cita? responde sí") o un mensaje de una persona del
    equipo en el medio, o un "sí" escrito antes de ver la pregunta, no confirman el dato."""
    if peticion is None:
        return False
    fila = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE direction = 'outbound') AS salidas, "
                "max(created_at) FILTER (WHERE direction = 'outbound') AS salida, "
                "max(created_at) FILTER (WHERE direction = 'inbound') AS entrada "
                "FROM messages WHERE tenant_id = :t AND conversation_id = :c AND deleted_at IS NULL "
                "AND created_at > :p AND (direction = 'inbound' OR status <> 'failed')"
            ),
            {"t": tenant_id, "c": conversation_id, "p": peticion.created_at},
        )
    ).first()
    if fila is None or fila.salidas != 1 or fila.salida is None or fila.entrada is None:
        return False
    return bool(fila.entrada > fila.salida)


def _datos_de_peticion(peticion: Evento | None, pendiente: Mapping[str, Any]) -> dict[str, Any]:
    """Los datos para volver a pedir la MISMA confirmación (target, hash de args y huella)."""
    data: dict[str, Any] = {"target": {"kind": pendiente.get("kind"), "name": pendiente.get("name")}}
    for clave in ("args_hash", "resumen"):
        if peticion is not None and peticion.data.get(clave):
            data[clave] = peticion.data[clave]
    return data


def rotulos_de(proc: Procedure | None) -> dict[str, str]:
    """Rótulos humanos de los datos que publicó la habilidad dueña del procedimiento
    (`MCPHost.register_rotulos`, adenda 2026-10-13); {} si no publicó."""
    if proc is None:
        return {}
    from core.mcp.host import mcp_host

    return mcp_host.get_rotulos(proc.owner)


# Máximo de mensajes del cliente que se revisan para saber si dijo un dato (adenda 2026-10-12).
_MAX_INBOUND_DICHOS = 100


async def _lo_dicho_por_el_cliente(
    session: AsyncSession, tenant_id: uuid.UUID, conversation_id: uuid.UUID, customer_id: uuid.UUID
) -> tuple[list[str], list[str]]:
    """(textos que escribió el cliente en la conversación, teléfonos del canal). Una consulta:
    los últimos mensajes entrantes (texto y transcripción de audio o imagen) y el teléfono
    verificado por el canal (`contact_phone`, `external_id`)."""
    filas = (
        await session.execute(
            text(
                "SELECT 'm' AS k, left(coalesce(m.content, ''), 2000) || ' ' || left(coalesce(m.media_transcription, ''), 2000) AS t "
                "FROM (SELECT content, media_transcription FROM messages WHERE tenant_id = :t "
                "AND conversation_id = :c AND deleted_at IS NULL AND direction = 'inbound' "
                "ORDER BY created_at DESC LIMIT :n) m "
                "UNION ALL SELECT 'p', coalesce(contact_phone, '') || ' ' || coalesce(external_id, '') "
                "FROM customers WHERE tenant_id = :t AND id = :cu"
            ),
            {"t": tenant_id, "c": conversation_id, "cu": customer_id, "n": _MAX_INBOUND_DICHOS},
        )
    ).all()
    return [str(f.t) for f in filas if f.k == "m"], [str(f.t) for f in filas if f.k == "p"]


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


# ---------------------------------------------------------------- cambios de datos (HU-AOS-07)


def _previo(tarea: Proyeccion, nombre: str, nuevo: Any) -> int | None:
    """El `seq` del valor anterior si el dato ya tenía OTRO valor; None si es nuevo o el mismo."""
    meta = tarea.slots.get(nombre)
    if not isinstance(meta, Mapping) or meta.get("value") is None or meta.get("value") == nuevo:
        return None
    return int(meta.get("seq") or 0)


async def _invalidar_y_retroceder(
    s: AsyncSession, foco: _Foco, cambiados: Mapping[str, int], escritos: set[str]
) -> _Foco:
    """Borra (`slot_cleared`) los datos que una acción fijó después del valor viejo de un dato que
    cambió y, si una etapa anterior del camino ya no cumple sus datos, vuelve a ella
    (`stage_entered` hacia atrás, que también descarta la confirmación pendiente)."""
    viejos = motor.dependientes(foco.tarea, min(cambiados.values()), frozenset(escritos))
    for nombre in viejos:
        fila = await _registrar(s, foco.fila, "slot_cleared", {"slot": nombre, "reason": "dato_cambiado"}, "kernel")
        foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=foco.proc, stage=foco.stage)
    destino = motor.etapa_de_retroceso(foco.proc, foco.tarea)
    if destino is not None:
        fila = await _registrar(
            s, foco.fila, "stage_entered", {"from_stage": foco.stage.id, "to_stage": destino.id}, "kernel"
        )
        foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=foco.proc, stage=destino)
    if viejos or destino is not None:
        log.info(
            "motor_dato_cambiado", procedure_id=foco.proc.id, invalidados=len(viejos),
            retrocede=destino.id if destino is not None else None,
        )
    return foco


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
    from core.shared import release_vigente

    return new_tool_context(
        conversation_id=conversation_id,
        customer_id=customer_id,
        idempotency_key=clave,
        channel=channel,
        # Mismo permiso de memoria que el loop (ADR-0114 §3, HU-AOS-15); None en legacy.
        write_namespaces=release_vigente.write_namespaces_del_turno(),
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
        sin_gobierno: str | None = None,
        de_la_release: dict[str, Procedure] | None = None,
        packs_de_la_release: frozenset[str] | None = None,
        declaradas_por_el_agente: frozenset[str] | None = None,
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
        # Datos de la tarea en foco que el motor fijó en ESTE turno por `actualizar_tarea` (todo o
        # nada: solo los aceptados). Evidencia del verificador para "anoté tu dirección" (ADR-0119).
        self.datos_fijados: set[str] = set()
        self._base = list(herramientas_base)
        self._candidatas = list(candidatas)
        self._core = frozenset(t.name for t in self._base if getattr(t, "plugin_id", "") == CORE_OWNER)
        self._foco = foco
        self._seq_al_empezar = dict(seq_al_empezar or {})
        # Procedimientos con una tarea abierta: no son candidatos de `iniciar_procedimiento`.
        self._abiertas = frozenset(abiertas)
        # Procedimiento de la tarea activa en foco que el motor no gobierna (versión fuera del
        # binario o habilidad deshabilitada): queda el flujo v2 de respaldo y SUS puertas no se
        # ocultan (las de los demás procedimientos, sí).
        self._sin_gobierno = sin_gobierno
        # HU-AOS-16: con release, las puertas se calculan sobre TODOS los procedimientos
        # publicados de la release (no solo los del agente en foco: un especialista que ligue
        # una puerta sin declarar el procedimiento no recupera el camino de siempre), y la
        # oferta y la negación son fail-closed por scope (packs de la release) y por agente
        # (lo que su versión declara). None = sin release: lo de siempre.
        self._de_la_release = de_la_release
        self._packs = packs_de_la_release
        self._del_agente = declaradas_por_el_agente
        self._plugin_de = {
            t.name: getattr(t, "plugin_id", None) for t in (*self._base, *self._candidatas)
        }
        self._cambio = False
        self._sombra: Any = None
        # HU-AOS-09: args de cada acción que pidió confirmación (por hash: son los que firma el
        # botón) y resultados de las acciones de la etapa en el turno, para armar el resumen.
        self._pedidas: dict[str, tuple[str, dict[str, Any]]] = {}
        self._resultados: dict[str, list[Mapping[str, Any]]] = {}
        if modo == "sombra":
            from core.kernel.procedures.sombra import Sombra

            self._sombra = Sombra(tenant_id, conversation_id, procedimientos, self._core)
        self.tarjeta = self._con_pista_sin_foco(tarjeta)

    # -- lo que ve el modelo
    @property
    def activo(self) -> bool:
        """Motor activo (`tenants.aos_procedures`); en sombra, False: nada de lo que ve el modelo
        ni de la salida cambia."""
        return self.modo == "activo"

    @property
    def en_foco(self) -> bool:
        return self.modo == "activo" and self._foco is not None

    @property
    def foco_task_id(self) -> uuid.UUID | None:
        """Id de la tarea en foco al empezar el turno (`core_decisions.focus_task_id`, HU-AOS-15)."""
        return self._foco.fila.id if self.en_foco and self._foco is not None else None

    def herramientas(self) -> list[Any]:
        """Acciones a ofrecer: las de siempre sin foco; con foco, las de la etapa más las del core
        que el turno ya ofrecía (las declarativas del negocio no: fail-closed). Activo, además, los
        comandos del hilo que aplican al estado (HU-AOS-06)."""
        if self.modo != "activo":
            return list(self._base)
        if self._foco is None:
            # Una tool declarativa del negocio con el nombre de un comando no se ofrece, ni las
            # puertas de los procedimientos disponibles: sin foco no hay camino de siempre para
            # lo que compromete (adenda 2026-10-12 de ADR-0113).
            ocultas = comandos.NOMBRES | self._puertas_sin_foco()
            salida = [t for t in self._base if t.name not in ocultas]
        else:
            permitidas = set(self._foco.stage.actions)
            salida = [t for t in self._candidatas if t.name in permitidas]
            nombres = {t.name for t in salida}
            # Una puerta del core (`sub_aplicar_cambio_plan`) solo se ofrece si es de la etapa.
            puertas = self._todas_las_puertas()
            salida += [t for t in self._base if t.name in self._core and t.name not in nombres and t.name not in puertas]
        return self._del_scope_y_del_agente(salida) + self.comandos()

    def _del_scope_y_del_agente(self, tools: list[Any]) -> list[Any]:
        """Fail-closed por scope y por agente con una release en el turno (HU-AOS-16)."""
        if self._packs is None and self._del_agente is None:
            return tools
        from core.kernel.oferta import ofrecidas_para_el_agente

        return ofrecidas_para_el_agente(
            tools,
            packs=self._packs,
            del_agente=self._del_agente,
            de_la_etapa=self._foco.stage.actions if self._foco is not None else (),
            comandos=comandos.NOMBRES,
        )

    def _fuera_del_agente(self, accion: str, etapa: Iterable[str]) -> bool:
        """¿La acción está fuera del scope de la release o de lo que el agente declara?"""
        if self._packs is None and self._del_agente is None:
            return False
        from core.kernel.oferta import permitida_para_el_agente

        if accion in self._plugin_de:
            plugin_id = self._plugin_de[accion]
        else:
            # No estaba en lo que armó el loop: su dueño sale del registro; si no existe, se niega.
            from core.mcp.host import mcp_host

            tool = mcp_host.get_tool(accion)
            if tool is None and accion not in comandos.NOMBRES:
                return True
            plugin_id = tool.plugin_id if tool is not None else CORE_OWNER
        return not permitida_para_el_agente(
            accion,
            plugin_id,
            packs=self._packs,
            del_agente=self._del_agente,
            de_la_etapa=etapa,
            comandos=comandos.NOMBRES,
        )

    # -- sin foco no hay camino de siempre (adenda 2026-10-12 de ADR-0113)
    def _todas_las_puertas(self) -> frozenset[str]:
        """Puertas de acción de los procedimientos disponibles del negocio (todos, no solo los
        candidatos: una tarea en pausa no abre el camino de siempre por otro lado)."""
        if self.modo != "activo":
            return frozenset()
        fuente = self._de_la_release if self._de_la_release is not None else self.procedimientos
        return frozenset().union(*(motor.acciones_de_puerta(p) for p in fuente.values()))

    def _liberadas(self, procedure_id: str | None) -> frozenset[str]:
        """Las puertas del procedimiento de una tarea sin gobierno (respaldo v2)."""
        fuente = self._de_la_release if self._de_la_release is not None else self.procedimientos
        proc = fuente.get(procedure_id) if procedure_id is not None else None
        return motor.acciones_de_puerta(proc) if proc is not None else frozenset()

    def _puertas_sin_foco(self) -> frozenset[str]:
        """Lo que no se ofrece sin foco: todas las puertas menos las del procedimiento de una tarea
        activa sin gobierno (respaldo v2). Solo para la oferta y la pista: la decisión de
        `antes_de_accion` se relee bajo el lock."""
        return self._todas_las_puertas() - self._liberadas(self._sin_gobierno)

    def _con_pista_sin_foco(self, tarjeta: str) -> str:
        """La tarjeta más la pista de iniciar el recorrido: activo, sin foco (y sin tarea sin
        gobierno) y con candidatos. Nombra las puertas que el negocio tiene como tools. Si los
        rótulos fallan, la tarjeta queda como estaba (la negación en `antes_de_accion` es la
        garantía)."""
        if self.modo != "activo" or self._foco is not None or self._sin_gobierno is not None or not self._candidatos():
            return tarjeta
        from core.kernel.tarjeta import pista_sin_foco

        existentes = {t.name for t in (*self._base, *self._candidatas)}
        try:
            return pista_sin_foco(tarjeta, sorted(self._puertas_sin_foco() & existentes))
        except Exception as exc:
            log.warning("motor_pista_sin_foco_fallo", error=type(exc).__name__)
            return tarjeta

    async def _negar_sin_foco(self, s: AsyncSession, accion: str, h: str) -> Previo:
        """Sin foco (bajo el lock): una puerta de un procedimiento disponible se niega y la
        respuesta dice qué recorrido iniciar; salvo con una tarea activa sin gobierno (respaldo v2)."""
        filas = await state.tareas_abiertas(s, tenant_id=self.tenant_id, conversation_id=self.conversation_id)
        activa = next((f for f in filas if f.status == "active" and f.stack_position == 0), None)
        if activa is not None and accion in self._liberadas(activa.procedure_id):
            return Previo()
        abiertas = {f.procedure_id for f in filas}
        ids = [
            pid for pid, p in sorted(self.procedimientos.items())
            if pid not in abiertas and accion in motor.acciones_de_puerta(p)
        ]
        texto = (
            mensaje("sin_recorrido", accion=accion, procedimientos=", ".join(ids))
            if ids else mensaje("tarea_en_curso")
        )
        log.info("motor_puerta_sin_foco", accion=accion, candidatos=len(ids))
        return Previo(Decision({"error": texto}, "denied", h))

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

    def afirmaciones(self) -> list[Any]:
        """Las `claims` con frases de los procedimientos disponibles, compiladas a `Afirmacion`
        para el verificador (ADR-0119, HU-AOS-07). Solo con el motor activo: en sombra o apagado
        no se verifica nada nuevo. Se respaldan con las acciones del turno y con el estado de las
        tareas (los datos que declara cada `claim`)."""
        if self.modo != "activo":
            return []
        from core.kernel.procedures.afirmaciones import compilar_afirmaciones

        salida: list[Any] = []
        for _pid, proc in sorted(self.procedimientos.items()):
            try:
                salida.extend(compilar_afirmaciones(proc))
            except Exception as exc:
                log.warning("motor_afirmaciones_fallo", procedure_id=proc.id, error=type(exc).__name__)
        return salida

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
        if self.modo != "activo":
            return Previo()
        # Una puerta de un procedimiento se decide siempre bajo el lock, aunque sea del core
        # (`sub_aplicar_cambio_plan`): sin foco se niega salvo el respaldo v2 (releído aquí, nunca
        # el del inicio del turno) y con foco sigue la etapa. Las demás del core pasan como siempre.
        puerta = accion in self._todas_las_puertas()
        if accion in self._core and not puerta:
            return Previo()
        h = motor.hash_args(args)
        try:
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                etapa = foco.stage.actions if foco is not None else ()
                if self._fuera_del_agente(accion, etapa):
                    # Fail-closed por agente y por scope (ADR-0118 §3): no la ofrece y no corre.
                    log.info("motor_accion_fuera_del_agente", accion=accion)
                    return Previo(Decision({"error": mensaje("fuera_del_agente", accion=accion)}, "denied", h))
                if foco is None:
                    return await self._negar_sin_foco(s, accion, h) if puerta else Previo()
                if accion not in foco.stage.actions:
                    return Previo(Decision({"error": _fuera_de_etapa(foco, accion)}, "denied", h))
                # Argumentos ligados a los datos de la tarea (HU-AOS-07): la cotización vigente, el
                # plan elegido, la dirección confirmada. Lo distinto se niega (salvo que el resolver
                # del dato lo lleve al mismo valor: un SKU por el uuid de la variante).
                args, distinto = await self._ligar(foco, accion, args)
                h = motor.hash_args(args)
                if distinto is not None:
                    return Previo(Decision({"error": mensaje("argumento_ligado", dato=distinto)}, "denied", h))
                gobernada = Previo(task_id=foco.fila.id, stage=foco.stage.id, args=args)
                if motor.requiere_confirmacion(foco.stage, accion) or accion in self._confirmar_tambien:
                    eventos = await _eventos(s, foco)
                    # HU-AOS-07: con resumen del pack, la confirmación vale para ESE resumen (el
                    # total leído del estado): si cambió con los mismos args, se pide otra vez.
                    huella = await self._huella_del_pack(foco, accion, args)
                    confirmada = motor.accion_confirmada(eventos, accion, h) and (
                        huella is None or (huella != "" and huella == motor.huella_pedida(eventos, accion, h))
                    )
                    if not confirmada:
                        pendiente = foco.tarea.pending_confirmation or {}
                        peticion = _evento_por_seq(eventos, pendiente.get("seq"))
                        misma = (
                            pendiente.get("kind") == "action"
                            and pendiente.get("name") == accion
                            and peticion is not None
                            and peticion.data.get("args_hash") == h
                            and (peticion.data.get("resumen") or None) == (huella or None)
                            and not motor.accion_confirmada(eventos, accion, h)
                        )
                        if misma and int(pendiente.get("seq") or 0) <= self._seq_al_empezar.get(foco.fila.id, 0):
                            # Adenda 2026-10-12: una petición de un turno anterior que el cliente ya
                            # tuvo para contestar (vencida) se vuelve a pedir, así sale otra vez el
                            # mensaje del kernel y el "confirmo" siguiente vale. La del turno
                            # inmediatamente anterior sigue sirviendo (acción → confirmar_tarea →
                            # acción).
                            respondido = await _ultimo_inbound_respondido(s, self.tenant_id, self.conversation_id)
                            misma = _fresca(peticion, respondido)
                        if not misma:
                            datos_peticion: dict[str, Any] = {"target": {"kind": "action", "name": accion}, "args_hash": h}
                            if huella:
                                datos_peticion["resumen"] = huella
                            await _registrar(s, foco.fila, "confirmation_requested", datos_peticion, "kernel")
                        self._pedidas[h] = (accion, dict(args))  # el resumen de la confirmación (HU-AOS-09)
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

    async def _ligar(self, foco: _Foco, accion: str, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """(argumentos efectivos, nombre del primer dato distinto o None). Bajo el lock."""
        from core.mcp.host import mcp_host

        herramienta = mcp_host.get_tool(accion)
        esquema = (herramienta.parameters if herramienta is not None else {}) or {}
        ligadura = motor.ligar_argumentos(foco.proc, foco.tarea, esquema, args)
        efectivos = ligadura.args
        vals = motor.valores(foco.tarea)
        if ligadura.distintos:
            try:
                async with asyncio.timeout(_PRESUPUESTO_RESOLVERS_S):  # corren con la conversación bloqueada
                    return await self._normalizar_distintos(foco, ligadura.distintos, efectivos, vals)
            except TimeoutError:
                return efectivos, ligadura.distintos[0].dato
        return efectivos, None

    async def _normalizar_distintos(
        self, foco: _Foco, distintos: Sequence[motor.Distinto], efectivos: dict[str, Any], vals: Mapping[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        for d in distintos:
            slot = foco.proc.slots[d.dato]
            if slot.resolver is None:
                return efectivos, d.dato
            ctx = _contexto_de_herramienta(
                self.conversation_id, self.customer_id, self.channel, f"{self.conversation_id}:resolver"
            )
            v, error = await _validar_dato(foco.proc, d.dato, slot, d.valor, ctx, dict(vals), self._permitidos)
            if error is not None or v != vals.get(d.dato):
                return efectivos, d.dato
            destino = efectivos if d.padre is None else efectivos[d.padre]
            destino[d.dato] = vals[d.dato]
        return efectivos, None

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
                if ok and isinstance(resultado, Mapping):
                    self._resultados.setdefault(accion, []).append(resultado)  # montos del resumen (HU-AOS-09)
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
        """Datos con `setBy: action:<acción>`: el nombre del dato es la clave del resultado. Si uno
        cambia de valor (una cotización nueva), lo que dependía del viejo se invalida."""
        cambiados: dict[str, int] = {}
        escritos: set[str] = set()
        vals = motor.valores(foco.tarea)
        ajenos = [
            n for n, slot in foco.proc.slots.items()
            if slot.setBy != f"action:{accion}" and vals.get(n) is not None and resultado.get(n) is not None
            and not motor.mismo_valor(slot, vals[n], resultado[n])
        ]
        if ajenos:
            # El resultado es de otro objeto que el de la tarea (el link de otro pedido): sus
            # datos no se toman.
            log.warning("motor_resultado_de_otro_objeto", procedure_id=foco.proc.id, accion=accion, datos=ajenos[:5])
            return
        for nombre, slot in foco.proc.slots.items():
            if slot.setBy != f"action:{accion}" or resultado.get(nombre) is None:
                continue
            valor, rechazo = motor.validar_valor(slot, resultado[nombre])
            if rechazo is not None:
                log.warning("motor_dato_de_accion_invalido", procedure_id=foco.proc.id, dato=nombre, codigo=rechazo.codigo)
                continue
            previo = _previo(foco.tarea, nombre, valor)
            try:
                fila = await _registrar(
                    s, foco.fila, "slot_set",
                    {"slot": nombre, "value": valor, "source": f"action:{accion}", "data_class": slot.dataClass},
                    "tool",
                )
            except state.TransicionInvalida:
                log.warning("motor_dato_de_accion_rechazado", procedure_id=foco.proc.id, dato=nombre)
                continue
            escritos.add(nombre)
            if previo is not None:
                cambiados[nombre] = previo
            foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=foco.proc, stage=foco.stage)
        if cambiados:
            await self._tras_cambio(s, foco, cambiados, escritos)

    async def _tras_cambio(
        self, s: AsyncSession, foco: _Foco, cambiados: Mapping[str, int], escritos: set[str]
    ) -> _Foco:
        """Un dato cambió de valor (HU-AOS-07): se borran los datos que una acción fijó con el valor
        viejo y, si una etapa anterior ya no cumple sus datos, la tarea vuelve a ella. Único punto
        para los tres caminos de escritura (comando, botón y resultado de una acción)."""
        foco = await _invalidar_y_retroceder(s, foco, cambiados, escritos)
        self._cambio = True
        return foco

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
                tarjeta = renderizar_tarjeta(
                    [state.proyeccion_de(f) for f in filas], procedimientos=registro.definicion, rotulos_de=rotulos_de
                )
                self._foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                self._sin_gobierno = _sin_gobierno(filas, self._foco)
                self.tarjeta = self._con_pista_sin_foco(tarjeta)
            return True
        except Exception as exc:
            log.warning("motor_releer_foco_fallo", error=type(exc).__name__)
            self.tarjeta = ""
            self._sin_gobierno = None  # fail-closed: las puertas siguen ocultas
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
        try:
            await self._pedir_dato_debido(task_id)
        except Exception as exc:
            log.warning("motor_pedir_dato_fallo", task_id=str(task_id), error=type(exc).__name__)

    async def _pedir_dato_debido(self, task_id: uuid.UUID) -> None:
        """Punto de reposo (adenda 2026-10-12 de ADR-0113): si lo único que le falta a la etapa
        vigente es que el cliente confirme un dato y no hay otra confirmación pendiente, el kernel
        la pide (`confirmation_requested` de tipo dato). Así ninguna confirmación que exige la
        salida queda sin pedir: ni la que venció con un cambio de etapa (`stage_entered` vacía la
        pendiente) ni la de un dato que llegó por otro camino. Sale el mensaje del kernel al
        cerrar el turno (`confirmacion_del_turno`)."""
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            if foco is None or foco.fila.id != task_id:
                return
            nombre = motor.dato_por_confirmar(foco.proc, foco.stage, foco.tarea)
            if nombre is None:
                return
            await _registrar(s, foco.fila, "confirmation_requested", {"target": {"kind": "slot", "name": nombre}}, "kernel")
            self._cambio = True
            log.info("motor_dato_por_confirmar", procedure_id=foco.proc.id, etapa=foco.stage.id)

    # -- al empezar el turno (adenda 2026-10-12 de ADR-0113)
    async def al_empezar_turno(self) -> None:
        """Confirmaciones de DATOS al empezar el turno, antes de que el modelo vea la tarjeta.

        - Pendiente de un dato pedida en el turno inmediatamente anterior y el cliente asiente
          ("sí", "ok", "sí, el correo está bien"): el kernel la confirma (la evidencia la pone el
          kernel, como el botón) y la tarea avanza.
        - Pendiente de un dato vencida y el cliente asiente: se vuelve a pedir (sale el mensaje del
          kernel; un sí a una pregunta vieja no vale).
        - Sin pendiente y con un dato que es lo único que bloquea la etapa (una tarea que quedó
          así, p. ej. por un dato que llegó con un botón): se pide.

        Acciones y etapas no cambian: siguen con su confirmación estricta. Sin nada de eso en la
        proyección que ya se leyó, no toca la base. Best-effort: un fallo se loguea y el turno sigue.

        Antes, una regla `start` del router (HU-AOS-17) inicia su procedimiento por `_iniciar`
        (salud del pack, una sola tarea en foco), después de las políticas del turno."""
        if self.modo != "activo":
            return
        await self._iniciar_por_regla()
        if self._foco is None:
            return
        tarea, proc, stage = self._foco.tarea, self._foco.proc, self._foco.stage
        pendiente = tarea.pending_confirmation
        de_dato = isinstance(pendiente, Mapping) and pendiente.get("kind") == "slot"
        if not de_dato and motor.dato_por_confirmar(proc, stage, tarea) is None:
            return
        task_id = self._foco.fila.id
        try:
            confirmo = await self._confirmaciones_al_empezar(task_id)
            if confirmo:
                await self._avanzar_seguro(task_id)
            await self._releer_seguro()
        except Exception as exc:
            log.warning("motor_al_empezar_fallo", error=type(exc).__name__)

    async def _iniciar_por_regla(self) -> None:
        from core.shared import release_vigente

        procedure_id = release_vigente.tomar_inicio()
        if procedure_id is None or self._foco is not None:
            return
        # Defensa en profundidad: solo un procedimiento que el agente del turno declara.
        del_agente = release_vigente.procedimientos_del_agente()
        if del_agente is not None and procedure_id not in del_agente:
            log.warning("motor_inicio_por_regla_ajeno", procedure_id=procedure_id)
            return
        try:
            resultado = await self._iniciar(procedure_id)
        except Exception as exc:
            log.warning("motor_inicio_por_regla_fallo", error=type(exc).__name__)
            return
        log.info("motor_inicio_por_regla", procedure_id=procedure_id, ok=resultado.ok)

    async def _confirmaciones_al_empezar(self, task_id: uuid.UUID) -> bool:
        """True si confirmó un dato. Todo se relee bajo el lock (el botón del consumer compite)."""
        async with _con_lock(self.tenant_id, self.conversation_id) as s:
            foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
            if foco is None or foco.fila.id != task_id:
                return False
            pendiente = foco.tarea.pending_confirmation
            if pendiente is None:
                nombre = motor.dato_por_confirmar(foco.proc, foco.stage, foco.tarea)
                if nombre is not None:
                    await _registrar(s, foco.fila, "confirmation_requested", {"target": {"kind": "slot", "name": nombre}}, "kernel")
                    log.info("motor_dato_por_confirmar", procedure_id=foco.proc.id, etapa=foco.stage.id)
                return False
            if pendiente.get("kind") != "slot":
                return False
            slot = foco.proc.slots.get(str(pendiente.get("name") or ""))
            clase = slot.dataClass if slot is not None else "payment"
            if not confirmacion_suficiente(self.texto_cliente, kind="slot", data_class=clase):
                return False  # el cliente dijo otra cosa: el modelo responde (o corrige el dato)
            peticion = _evento_por_seq(await _eventos(s, foco), pendiente.get("seq"))
            if not await _contesta_el_mensaje_del_kernel(s, self.tenant_id, self.conversation_id, peticion):
                await _registrar(s, foco.fila, "confirmation_requested", _datos_de_peticion(peticion, pendiente), "kernel")
                log.info("motor_confirmacion_de_dato_repedida", procedure_id=foco.proc.id)
                return False
            await _confirmar_pendiente(s, foco)
            log.info("motor_dato_confirmado_al_empezar", procedure_id=foco.proc.id)
            return True

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
                    # Salió la última etapa: el recorrido terminó, declare o no eventos de cierre
                    # (HU-AOS-07). Solo una última etapa SIN `exitWhen` espera su evento (arriba).
                    await _completar(s, foco.fila, foco.proc, "etapa_final")
                    self._cambio = True
                    return
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
                turn_id=turno_vigente.turno_id(), owner_instance_id=_agente_del_turno_id(),
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
            no_dichos = await self._no_dichos(s, proc, slots, validos)
            if no_dichos:  # todo o nada, como los demás errores
                errores = [mensaje("dato_no_dicho", dato=n) for n in no_dichos]
                return ResultadoComando(False, errores, list(foco.stage.actions))
            cambian = {n: p for n, v in validos.items() if (p := _previo(foco.tarea, n, v)) is not None}
            if cambian and motor.datos_congelados(foco.proc, await _eventos(s, foco)):
                errores = [mensaje("dato_bloqueado", dato=n) for n in cambian]
                return ResultadoComando(False, errores, list(foco.stage.actions))
            por_confirmar: str | None = None
            for nombre, v in validos.items():
                fila = await _registrar(
                    s, foco.fila, "slot_set",
                    {"slot": nombre, "value": v, "source": "llm", "data_class": proc.slots[nombre].dataClass}, "llm",
                )
                # La confirmación de un dato que exige la salida de una etapa la pide el punto de
                # reposo cuando es lo único que falta (`_pedir_dato_debido`, adenda 2026-10-12);
                # aquí solo la de un dato `confirm` que ninguna etapa exige (como antes).
                if proc.slots[nombre].confirm and por_confirmar is None and nombre not in _confirmados_por_salida(proc):
                    por_confirmar = nombre
                foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=proc, stage=foco.stage)
            self.datos_fijados.update(str(n) for n in validos)
            if cambian:
                foco = await self._tras_cambio(s, foco, cambian, set(validos))
            if por_confirmar is not None and foco.tarea.pending_confirmation is None:
                await _registrar(
                    s, foco.fila, "confirmation_requested", {"target": {"kind": "slot", "name": por_confirmar}}, "kernel"
                )
            task_id = foco.fila.id
        return await self._terminar_comando(task_id)

    async def _no_dichos(
        self, s: AsyncSession, proc: Procedure, crudos: Mapping[str, Any], validos: Mapping[str, Any]
    ) -> list[str]:
        """Datos personales (`pii`/`payment`) que el modelo quiere fijar y el cliente NO escribió en
        la conversación (adenda 2026-10-12 de ADR-0113): se rechazan. El caso real: el nombre del
        perfil de WhatsApp fijado como `full_name` sin que el cliente lo diera. El teléfono que
        verifica el canal sí vale. Una consulta por comando, solo si hay un dato personal."""
        personales = [n for n in validos if proc.slots[n].dataClass in ("pii", "payment")]
        if not personales:
            return []
        textos, telefonos = await _lo_dicho_por_el_cliente(s, self.tenant_id, self.conversation_id, self.customer_id)
        if self.texto_cliente:
            textos.append(self.texto_cliente)
        # El teléfono del canal solo respalda un dato que ES un teléfono (no un documento).
        return [
            n for n in personales
            if not motor.dicho_por_el_cliente(
                [crudos.get(n), validos[n]], textos, telefonos if proc.slots[n].format == "phone" else ()
            )
        ]

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
            del_turno_anterior = _fresca(peticion, respondido)
            kind = pendiente.get("kind")
            slot = foco.proc.slots.get(str(pendiente.get("name") or "")) if kind == "slot" else None
            # Un dato acepta un asentimiento; una acción o una etapa, solo el sí explícito
            # (adenda 2026-10-12 de ADR-0113, el mismo criterio que al empezar el turno).
            # Un dato que ya no está en el procedimiento cuenta como de pago: criterio estricto.
            suficiente = confirmacion_suficiente(
                self.texto_cliente, kind=kind, data_class=slot.dataClass if slot is not None else "payment"
            )
            if kind == "slot" and slot is not None and slot.dataClass == "pii":
                # El sí a secas de un dato contesta solo el mensaje que llevó la petición.
                del_turno_anterior = del_turno_anterior and await _contesta_el_mensaje_del_kernel(
                    s, self.tenant_id, self.conversation_id, peticion
                )
            if pedida_en_este_turno or not suficiente:
                return ResultadoComando(False, [mensaje("falta_confirmacion")], list(foco.stage.actions))
            if not del_turno_anterior:
                # El cliente quiere confirmar, pero la petición venció: se vuelve a pedir y sale el
                # mensaje del kernel (adenda 2026-10-12). La de una acción la vuelve a pedir la
                # acción misma (con sus argumentos, para un resumen fiel).
                if kind == "action":
                    return ResultadoComando(
                        False, [mensaje("confirmacion_vencida", accion=str(pendiente.get("name") or ""))],
                        list(foco.stage.actions),
                    )
                await _registrar(s, foco.fila, "confirmation_requested", _datos_de_peticion(peticion, pendiente), "kernel")
                return ResultadoComando(False, [mensaje("confirmacion_repedida")], list(foco.stage.actions))
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


    # -- política de modelo y confirmación (HU-AOS-09)
    @property
    def riesgo_alto(self) -> bool:
        """La etapa en foco declara `risk: high` (con la proyección del inicio del turno)."""
        return self.en_foco and self._foco is not None and self._foco.stage.risk == "high"

    async def confirmacion_del_turno(self) -> ConfirmacionDelTurno | None:
        """El mensaje de confirmación que reemplaza el texto del turno, o None.

        Solo si la tarea en foco quedó esperando una confirmación pedida EN ESTE turno. El cuerpo
        sale de los argumentos exactos que firma el botón (acción, solo las propiedades de su
        schema), de los datos de la tarea (etapa o dato) y de los montos que devolvió en el turno
        la acción que el procedimiento declara como fuente (`claims` de tipo `amount`); nunca del
        texto del modelo. Fail-closed: si la petición es de este turno pero no hay un resumen fiel
        (faltan los args, un monto es ambiguo o choca con un argumento, o algo falla), sale el
        texto fijo `sin_resumen`, sin datos ni botón. Solo si no se pudo leer la tarea, None."""
        if self.modo != "activo":
            return None
        try:
            from core.db.session import get_session

            async with get_session() as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None:
                    return None
                pendiente = foco.tarea.pending_confirmation or {}
                seq = int(pendiente.get("seq") or 0)
                if not seq or seq <= self._seq_al_empezar.get(foco.fila.id, 0):
                    return None
                peticion = _evento_por_seq(await _eventos(s, foco), seq)
        except Exception as exc:
            log.warning("motor_confirmacion_del_turno_fallo", error=type(exc).__name__)
            return None
        from core.kernel.confirmacion import armar, sin_resumen

        try:
            args_hash = str((peticion.data.get("args_hash") if peticion is not None else "") or "")
            datos = await self._resumen_del_pack(foco, pendiente, args_hash, peticion)
            if datos is _SIN_RESUMEN_DEL_PACK:
                datos = self._datos_a_confirmar(foco, pendiente, args_hash)
            if datos is None:
                log.info("motor_confirmacion_sin_resumen", task_id=str(foco.fila.id))
                await self._anular_peticion(foco.fila.id, seq, pendiente)
                return ConfirmacionDelTurno(confirmacion=sin_resumen(), modo="explicit")
            boton = payload_de_confirmacion(self.tenant_id, foco.fila.id, seq, args_hash)
            if pendiente.get("kind") == "slot":
                # Un dato sale siempre como texto, y basta un sí (adenda 2026-10-12 de ADR-0113).
                return ConfirmacionDelTurno(
                    confirmacion=armar(foco.proc.title, datos, boton, es_dato=True, rotulos=rotulos_de(foco.proc)),
                    modo="explicit",
                )
            modo = "interactive_button" if foco.stage.confirmation == "interactive_button" else "explicit"
            # El resumen único (con los datos del cliente) no se recorta (adenda 2026-10-13).
            completo = pendiente.get("kind") == "action" and self._resumen_registrado(str(pendiente.get("name") or "")) is not None
            return ConfirmacionDelTurno(
                confirmacion=armar(foco.proc.title, datos, boton, rotulos=rotulos_de(foco.proc), completo=completo),
                modo=modo,
            )
        except Exception as exc:
            log.warning("motor_confirmacion_del_turno_fallo", error=type(exc).__name__)
            await self._anular_peticion(foco.fila.id, seq, pendiente)
            return ConfirmacionDelTurno(confirmacion=sin_resumen(), modo="explicit")

    async def _anular_peticion(self, task_id: uuid.UUID, seq: int, pendiente: Mapping[str, Any]) -> None:
        """Sale `sin_resumen` para una petición de ACCIÓN de este turno (revisión de seguridad,
        adenda 2026-10-13): el cliente no vio lo que confirmaría (ni los documentos que aceptaría).
        Se reemplaza por una petición anulada (`args_hash` que ninguna llamada produce), así un «Sí,
        confirmo» a `sin_resumen` no confirma nada y la acción, al volver a llamarse, pide otra vez
        con su resumen. Bajo el lock; si la petición ya no es la vigente, no hace nada."""
        if pendiente.get("kind") != "action":
            return
        try:
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                actual = foco.tarea.pending_confirmation if foco is not None else None
                if foco is None or foco.fila.id != task_id or not actual or int(actual.get("seq") or 0) != seq:
                    return
                await _registrar(
                    s, foco.fila, "confirmation_requested",
                    {"target": {"kind": "action", "name": str(pendiente.get("name") or "")}, "args_hash": "anulada"},
                    "kernel",
                )
            log.info("motor_peticion_anulada_sin_resumen", task_id=str(task_id))
        except Exception as exc:
            log.warning("motor_anular_peticion_fallo", error=type(exc).__name__)

    async def salida_al_agotar(self) -> tuple[str, ConfirmacionDelTurno | None] | None:
        """Se agotaron las vueltas del turno con el motor activo (adenda 2026-10-13 de ADR-0113):
        en vez de escalar, el kernel responde con lo que falta, si lo sabe.

        - el turno pidió una confirmación → su mensaje (como al cerrar un turno normal);
        - lo primero que falta son datos del cliente → la pregunta del kernel con todos los que
          faltan, en orden y como lista numerada;
        - lo único que falta es confirmar un dato → se pide y sale su mensaje.

        None (el loop escala como siempre) sin foco, si falta una acción o un dato del servidor, o
        si algo falla. Se audita sin valores."""
        if self.modo != "activo":
            return None
        try:
            confirmacion = await self.confirmacion_del_turno()
            if confirmacion is not None:
                return await self._auditar_sin_vueltas("confirmacion", confirmacion.confirmacion.texto, confirmacion)
            async with _con_lock(self.tenant_id, self.conversation_id) as s:
                foco = await _foco(s, self.tenant_id, self.conversation_id, self._permitidos)
                if foco is None:
                    return None
                datos = motor.siguientes_datos(foco.proc, foco.stage, foco.tarea)
                if not datos:
                    nombre = motor.dato_por_confirmar(foco.proc, foco.stage, foco.tarea)
                    if nombre is None:
                        return None
                    await _registrar(
                        s, foco.fila, "confirmation_requested", {"target": {"kind": "slot", "name": nombre}}, "kernel"
                    )
            if datos:
                from core.kernel.confirmacion import pregunta

                return await self._auditar_sin_vueltas("pregunta", pregunta(datos, rotulos_de(foco.proc)), None)
            confirmacion = await self.confirmacion_del_turno()
            if confirmacion is None:
                return None
            return await self._auditar_sin_vueltas("confirmacion_de_dato", confirmacion.confirmacion.texto, confirmacion)
        except Exception as exc:
            log.warning("motor_salida_al_agotar_fallo", error=type(exc).__name__)
            return None

    async def _auditar_sin_vueltas(
        self, salida: str, texto: str, confirmacion: ConfirmacionDelTurno | None
    ) -> tuple[str, ConfirmacionDelTurno | None]:
        from core.shared.audit import record_audit

        log.warning("motor_turno_sin_vueltas", salida=salida)
        await record_audit(
            actor="core:motor", action="sin_vueltas", conversation_id=self.conversation_id,
            input_data={"salida": salida},
        )
        return texto, confirmacion

    def _resumen_registrado(self, accion: str) -> Any:
        """La función de resumen de la acción, solo si su pack está permitido en el turno."""
        from core.mcp.host import mcp_host

        registrado = mcp_host.get_resumen(accion)
        return registrado[0] if registrado is not None and registrado[1] in self._permitidos else None

    async def _lineas_del_pack(self, accion: str, args: Mapping[str, Any]) -> list[Any] | None:
        """Las líneas del resumen del pack para esos args, o None si falla, tarda, viene vacío, se
        pasa de 20 líneas (se perdería el total) o trae un monto que no es número."""
        fn = self._resumen_registrado(accion)
        if fn is None:
            return None
        ctx = _contexto_de_herramienta(
            self.conversation_id, self.customer_id, self.channel, f"{self.conversation_id}:resumen"
        )
        try:
            async with asyncio.timeout(_TIMEOUT_RESUMEN_S):
                lineas = await fn(dict(args), ctx)
        except Exception as exc:
            log.warning("motor_resumen_del_pack_fallo", accion=accion, error=type(exc).__name__)
            return None
        if not lineas or len(lineas) > _MAX_LINEAS_RESUMEN:
            return None
        for linea in lineas:
            valor = getattr(linea, "valor", None)
            if getattr(linea, "es_monto", False) and (isinstance(valor, bool) or not isinstance(valor, int | float)):
                return None
        return list(lineas)

    async def _lineas_del_resumen(self, foco: _Foco, accion: str, args: Mapping[str, Any]) -> list[Any] | None:
        """El resumen ÚNICO que el cliente confirma (adenda 2026-10-13 de ADR-0113): las líneas del
        pack (plan, total…), después los datos que dio el cliente en esta tarea (en el orden del
        procedimiento: así el correo se confirma aquí, sin pregunta aparte) y al final los
        documentos que acepta. None si el pack no lo pudo armar."""
        from core.mcp.types import DatoDeResumen

        lineas = await self._lineas_del_pack(accion, args)
        if not lineas:
            return None
        propias = [x for x in lineas if not getattr(x, "documento_aceptado", False)]
        documentos = [x for x in lineas if getattr(x, "documento_aceptado", False)]
        ya = {str(getattr(x, "nombre", "")) for x in propias}
        del_cliente = [DatoDeResumen(n, v) for n, v in motor.datos_del_cliente(foco.proc, foco.tarea) if n not in ya]
        return [*propias, *del_cliente, *documentos]

    async def _huella_del_pack(self, foco: _Foco, accion: str, args: Mapping[str, Any]) -> str | None:
        """Huella del resumen que el cliente confirma (HU-AOS-07; con los datos del cliente desde
        la adenda 2026-10-13: si uno cambia después del sí, se pide otro): None si la acción no
        tiene resumen del pack; "" si lo tiene pero no se pudo armar (nunca coincide)."""
        if self._resumen_registrado(accion) is None:
            return None
        lineas = await self._lineas_del_resumen(foco, accion, args)
        return motor.huella_de_resumen(lineas) if lineas else ""

    async def _resumen_del_pack(
        self, foco: _Foco, pendiente: Mapping[str, Any], args_hash: str, peticion: Evento | None
    ) -> Any:
        """El resumen que provee la habilidad de la acción pendiente (HU-AOS-07), leído de su
        estado con los argumentos firmados; `_SIN_RESUMEN_DEL_PACK` si la acción no tiene uno.
        Fail-closed (None → `sin_resumen`): si no se puede armar, si la habilidad ya no está sana o
        si no es el mismo que se ató a la petición (`resumen` de `confirmation_requested`). Con
        resumen del pack no se mezclan los montos de los `claims` del turno: el pack ya leyó el
        total vigente."""
        from core.kernel.confirmacion import Dato

        if pendiente.get("kind") != "action":
            return _SIN_RESUMEN_DEL_PACK
        accion = str(pendiente.get("name") or "")
        if self._resumen_registrado(accion) is None:
            return _SIN_RESUMEN_DEL_PACK
        pedida = self._pedidas.get(args_hash)
        if pedida is None or pedida[0] != accion or not await self._sigue_sano(foco.proc):
            return None
        lineas = await self._lineas_del_resumen(foco, accion, pedida[1])
        atada = str((peticion.data.get("resumen") if peticion is not None else "") or "")
        if not lineas or motor.huella_de_resumen(lineas) != atada:
            return None
        de_pago = frozenset(n for n, slot in foco.proc.slots.items() if slot.dataClass == "payment")
        datos = [
            Dato(
                str(linea.nombre), linea.valor, es_monto=bool(linea.es_monto),
                documento_aceptado=bool(getattr(linea, "documento_aceptado", False)),
            )
            for linea in lineas
            if str(getattr(linea, "nombre", "") or "") and str(linea.nombre) not in de_pago
        ]
        return datos or None

    def _datos_a_confirmar(self, foco: _Foco, pendiente: Mapping[str, Any], args_hash: str) -> list[Any] | None:
        """Los datos del resumen, o None si no hay uno fiel (el turno manda `sin_resumen`)."""
        from core.kernel.confirmacion import Dato, datos_de_args
        from core.mcp.host import mcp_host

        proc, kind, nombre = foco.proc, pendiente.get("kind"), str(pendiente.get("name") or "")
        vals = motor.valores(foco.tarea)
        de_pago = frozenset(n for n, slot in proc.slots.items() if slot.dataClass == "payment")

        def de_la_tarea(nombres: Sequence[str]) -> list[Dato]:
            return [Dato(n, vals.get(n)) for n in nombres if n in proc.slots and n not in de_pago and vals.get(n) is not None]

        args: dict[str, Any] = {}
        if kind == "action":
            pedida = self._pedidas.get(args_hash)
            if pedida is None:
                return None  # sin los args exactos no hay resumen fiel
            accion, args = pedida
            herramienta = mcp_host.get_tool(accion)
            propiedades = ((herramienta.parameters if herramienta is not None else {}) or {}).get("properties") or {}
            datos = datos_de_args(args, list(propiedades), de_pago)
        elif kind == "stage":
            salida = foco.stage.exitWhen
            datos = de_la_tarea([*(salida.slots if salida else []), *(salida.confirmed if salida else [])])
        elif kind == "slot":
            # Un dato no tiene monto: el resumen es solo ese dato (adenda 2026-10-12).
            return de_la_tarea([nombre]) or None
        else:
            return None
        for claim in proc.claims:
            if claim.kind != "amount":
                continue
            for fuente in claim.mustMatch:
                accion_fuente, _, campo = fuente.removeprefix("action:").partition(".")
                if not fuente.startswith("action:") or not campo or accion_fuente not in foco.stage.actions:
                    continue
                montos = {
                    r.get(campo) for r in self._resultados.get(accion_fuente, [])
                    if isinstance(r.get(campo), int | float) and not isinstance(r.get(campo), bool)
                }
                if len(montos) > 1:
                    return None  # dos cotizaciones distintas en el turno: ¿cuál se confirma?
                if not montos:
                    continue  # la fuente no corrió en este turno: el resumen va sin ese monto
                monto = montos.pop()
                if campo in args and args[campo] != monto:
                    return None  # el argumento firmado dice otra cosa que el servidor
                datos = [d for d in datos if d.nombre != campo]
                datos.append(Dato(campo, monto, es_monto=True))
        return datos


def _fuera_de_etapa(foco: _Foco, accion: str) -> str:
    """El error de una acción fuera de la etapa, con lo que falta para salir de ella si el motor lo
    sabe (adenda 2026-10-12): el modelo no tiene que adivinar ni decir que le falta una herramienta."""
    from core.kernel.tarjeta import describir_faltas

    faltas = describir_faltas(foco.proc, foco.stage, foco.tarea)
    if faltas:
        return mensaje("fuera_de_etapa_falta", accion=accion, etapa=foco.stage.id, faltas=faltas)
    return mensaje("fuera_de_etapa", accion=accion, etapa=foco.stage.id)


def _confirmados_por_salida(proc: Procedure) -> frozenset[str]:
    """Datos que alguna etapa exige confirmados para salir (`exitWhen.confirmed`)."""
    return frozenset(n for st in proc.stages if st.exitWhen is not None for n in st.exitWhen.confirmed)


@dataclass(frozen=True)
class ConfirmacionDelTurno:
    """Lo que el turno envía en vez del texto del modelo (HU-AOS-09). `modo` es el de la etapa:
    `interactive_button` va con botón donde el canal lo soporta; `explicit`, solo como texto."""

    confirmacion: Any  # core.kernel.confirmacion.Confirmacion
    modo: str


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


def _sin_gobierno(filas: Sequence[ConversationTask], foco: _Foco | None) -> str | None:
    """El procedimiento de la tarea activa en foco que el motor no gobierna, o None (respaldo v2,
    adenda HU-AOS-05)."""
    if foco is not None:
        return None
    return next((f.procedure_id for f in filas if f.status == "active" and f.stack_position == 0), None)


def _nombre_seguro(nombre: Any) -> str:
    """Un nombre de dato que viene del modelo, acotado y sin saltos (va dentro de un mensaje)."""
    return " ".join(str(nombre).split())[:40] or "?"


def _agente_del_turno_id() -> uuid.UUID | None:
    """El especialista que atiende el turno (HU-AOS-15/17): dueño de la tarea que se inicia, así
    el router sabe quién la sigue. None sin release (legacy)."""
    from core.shared import release_vigente

    agente = release_vigente.agente_del_turno()
    return agente.instance_id if agente is not None else None


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
    release = release_vigente.vigente()
    if release is not None and release.procedimientos_por_agente is not None:
        # HU-AOS-16: con una release `release/2`, los packs permitidos son los de la release
        # (scope de la conexión) y la oferta es fail-closed por el agente en foco.
        from core.kernel.oferta import declaradas_por

        packs = frozenset((*release.skills, *release.tenant_plugins))
        comunes["permitidos"] = frozenset((packs | {CORE_OWNER}) - set(disabled))
        comunes["de_la_release"] = {
            pid: p for pid, p in release.procedimientos.items() if p.version != "0.0.0"
        }
        comunes["packs_de_la_release"] = packs
        # Sin agente en foco (release con solo el Coordinador): fail-closed, solo core y comandos.
        comunes["declaradas_por_el_agente"] = declaradas_por(release_vigente.agente_del_turno()) or frozenset()
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
            tarjeta = renderizar_tarjeta(tareas, procedimientos=registro.definicion, rotulos_de=rotulos_de)
            foco = await _foco(session, tenant_id, conversation_id, comunes["permitidos"])
    if banderas.procedimientos:
        # Si el inbound fue un botón, su título no es un sí escrito: el turno no confirma por texto.
        if await _tomar_marca_interactivo(tenant_id, conversation_id):
            texto_cliente = ""
        return ContextoMotor(
            modo="activo", procedimientos=registro.disponibles(enabled, disabled), tarjeta=tarjeta,
            foco=foco, texto_cliente=texto_cliente, seq_al_empezar={f.id: f.last_seq for f in filas},
            confirmar_tambien=confirmar_tambien, abiertas=frozenset(f.procedure_id for f in filas),
            sin_gobierno=_sin_gobierno(filas, foco), **comunes,
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
        banderas = await banderas_del_negocio(s, tenant_id)
    if interactivo and banderas.ruteo != "off" and banderas.runtime == "v3":
        # El payload del botón para las reglas del router (HU-AOS-17): solo con el ruteo encendido.
        from core.kernel.router import guardar_payload

        await guardar_payload(tenant_id, conversation_id, interactivo)
    if not banderas.procedimientos:
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
    previo = _previo(foco.tarea, nombre, v)
    if previo is not None and motor.datos_congelados(foco.proc, await _eventos(s, foco)):
        log.info("motor_dato_interactivo_congelado", procedure_id=foco.proc.id, dato=nombre)
        return
    fila = await _registrar(
        s, foco.fila, "slot_set",
        {"slot": nombre, "value": v, "source": slot.setBy, "data_class": slot.dataClass}, "kernel",
    )
    if previo is not None:
        foco = _Foco(fila=fila, tarea=state.proyeccion_de(fila), proc=foco.proc, stage=foco.stage)
        await _invalidar_y_retroceder(s, foco, {nombre: previo}, {nombre})


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
    # Solo las tareas con el MISMO valor en alguna clave (HU-AOS-07): las que esperan un pago
    # viven horas y muchas abiertas del negocio no pueden tapar la del evento. `correlaciona`
    # sigue siendo el filtro final (dato del procedimiento fijado por una acción).
    por_valor = [
        ConversationTask.slots[nombre]["value"].astext == str(payload[nombre])
        for nombre in claves
        if payload.get(nombre) is not None and len(str(payload[nombre])) <= 200
    ]
    if not por_valor:
        return 0
    from core.kernel.banderas import banderas_del_negocio

    # La bandera primero: con el motor apagado no se consulta ninguna tarea (una lectura por PK).
    if not (await banderas_del_negocio(session, tenant_id)).procedimientos:
        return 0
    filas = (
        await session.execute(
            select(ConversationTask)
            .where(
                ConversationTask.tenant_id == tenant_id,
                ConversationTask.status.in_(state.ABIERTOS),
                ConversationTask.procedure_id.in_(list(procs)),
                or_(*por_valor),
            )
            .order_by(ConversationTask.conversation_id, ConversationTask.id)
            .limit(50)
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
