"""Corredor de las pruebas del agente en el sandbox (HU-AOS-103, ADR-0212 §2-§5, SPEC-53).

`correr(tenant_id, run_id)` toma una corrida `en_cola` y, por cada escenario aprobado y cada intento (`k`):

1. abre una conversación de prueba PROPIA de la corrida (canal `test`, `state.prueba_corrida`, cliente sintético
   `panel-prueba-<run_id>`) sobre el BORRADOR del agente, con el sandbox de HU-ARCH-14: las tools con efectos se
   simulan o fallan cerradas (`herramienta_sin_simulador`), así que no se crea nada real;
2. hace hablar al `ClienteSimulado` contra el loop real (`process_conversation`) hasta `[OBJETIVO_CUMPLIDO]`,
   `[ME_RINDO]`, un relevo a una persona o `TOPE_TURNOS` (12) turnos;
3. **consolida el gasto y purga** en UNA transacción: suma los créditos de los turnos de la conversación, suma el
   costo del cliente simulado y escribe una sola fila `prueba_agente` sin conversación (brutos, ADR-0212 §4), y recién
   entonces llama a `purge_test_conversation`. Si la transacción falla no se pierde ni se cobra dos veces: o están las
   dos cosas o ninguna, y el barrido (`barrido.py`) repite la consolidación;
4. juzga (`core.pruebas.juez`): primero lo determinista sobre las tools que el agente llamó, sin LLM; después el juez.

Estado de la corrida: el reclamo `en_cola → corriendo` es el candado (el índice único parcial deja UNA activa por
negocio, y quien no gana el `UPDATE` sale sin hacer nada, así que un reenvío de la tarea no corre dos veces). Termina
`terminada`, `cancelada` (el dueño; se relee entre turnos), `cortada_por_tope` (gastó más del tope = estimado × 1,5,
se mide tras cada intento), `cortada_por_saldo` (un turno saldría `agotado`) o `fallo`. Siempre se cobra solo lo gastado.

**Versión que se prueba:** el borrador del agente. Todo especialista tiene su `agent_drafts` desde que se crea (no se
borra al publicar), así que no hace falta armar otra release; un agente sin borrador (el Coordinador) no es probable y
la corrida termina en `fallo` con `agente_sin_borrador`, sin gastar nada.

Todo corre dentro del `tenant_scope` del negocio y filtra `tenant_id` explícito además de la RLS (invariante 1). Los
prompts del simulador y del juez reciben solo la configuración de su agente y la transcripción de esta corrida.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import cast, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

from core.db.models import (
    AgentDraftRecord,
    AgentTestCase,
    AgentTestResult,
    AgentTestRun,
    Conversation,
    Customer,
    Tenant,
    UsageLedger,
)
from core.db.session import get_session
from core.shared.logger import get_logger
from core.shared.tenant import tenant_scope
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

TOPE_TURNOS = 12  # techo de turnos del cliente por escenario (ADR-0212 §3)
PREFIJO_CLIENTE = "panel-prueba-"
ESTILO = "cordial y breve"

# Estados finales y por qué cortó una corrida (`agent_test_runs.estado`).
TERMINADA = "terminada"
CANCELADA = "cancelada"
POR_TOPE = "cortada_por_tope"
POR_SALDO = "cortada_por_saldo"
FALLO = "fallo"
ACTIVAS = ("en_cola", "corriendo")


class ConversacionAusente(Exception):
    """La conversación no existe, es de otro negocio o de otra corrida: no se consolidó ni se purgó nada."""


class _Corte(Exception):
    """El intento no sigue: la corrida se cortó (`estado` final) o la cancelaron."""

    def __init__(self, estado: str) -> None:
        super().__init__(estado)
        self.estado = estado


@dataclass
class _Caso:
    id: uuid.UUID
    titulo: str
    objetivo: str
    datos: dict[str, Any]
    criterios: list[str]
    esperadas: list[str]
    prohibidas: list[str]


@dataclass
class _Corrida:
    tenant_id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID
    k: int
    tope: Decimal
    casos: list[_Caso]
    conexion_id: uuid.UUID
    version: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Gasto:
    """Lo que gastó el cliente simulado en un intento. Vive fuera de la función que lo llena para sobrevivir a una
    excepción: lo gastado se cobra aunque el intento se corte."""

    creditos: Decimal = Decimal(0)
    usd: Decimal = Decimal(0)
    entrada: int = 0
    salida: int = 0


@dataclass
class _Intento:
    estado: str  # pasa | falla | no_se_pudo_probar
    fin: str | None = None
    transcripcion: list[dict[str, Any]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    criterio: str | None = None
    razon: str | None = None
    creditos: Decimal = Decimal(0)  # todo lo gastado por este intento (turnos, simulador y juez)


# ----------------------------------------------------------------------------------------------- estado de la corrida
async def _reclamar(tenant_id: uuid.UUID, run_id: uuid.UUID) -> AgentTestRun | None:
    """`en_cola → corriendo` con un UPDATE atómico. None si otra tarea ya la tomó, la cancelaron o no existe."""
    ahora = datetime.now(UTC)
    async with get_session() as session:
        fila = (
            await session.execute(
                update(AgentTestRun)
                .where(
                    AgentTestRun.tenant_id == tenant_id,
                    AgentTestRun.id == run_id,
                    AgentTestRun.estado == "en_cola",
                    AgentTestRun.deleted_at.is_(None),
                )
                .values(estado="corriendo", iniciada_en=ahora, updated_at=ahora)
                .returning(AgentTestRun.id)
            )
        ).first()
        if fila is None:
            return None
        corrida = (
            await session.execute(
                select(AgentTestRun).where(AgentTestRun.tenant_id == tenant_id, AgentTestRun.id == run_id)
            )
        ).scalar_one()
        session.expunge(corrida)
        return corrida


async def _estado(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(AgentTestRun.estado).where(AgentTestRun.tenant_id == tenant_id, AgentTestRun.id == run_id)
            )
        ).scalar_one_or_none()


async def cerrar(tenant_id: uuid.UUID, run_id: uuid.UUID, estado: str, *, codigo: str | None = None) -> bool:
    """Estado final de una corrida ACTIVA. No pisa una `cancelada` ni una ya cerrada (devuelve False). El motivo de un
    `fallo` queda en `version_probada.fallo` (la tabla no tiene otra columna) y en el log."""
    ahora = datetime.now(UTC)
    valores: dict[str, Any] = {"estado": estado, "terminada_en": ahora, "updated_at": ahora}
    if codigo is not None:
        valores["version_probada"] = AgentTestRun.version_probada.op("||")(cast(literal(json.dumps({"fallo": codigo})), JSONB))
    async with get_session() as session:
        fila = (
            await session.execute(
                update(AgentTestRun)
                .where(
                    AgentTestRun.tenant_id == tenant_id,
                    AgentTestRun.id == run_id,
                    AgentTestRun.estado.in_(ACTIVAS),
                )
                .values(**valores)
                .returning(AgentTestRun.id)
            )
        ).first()
    log.info("prueba_corrida_cerrada", tenant_id=str(tenant_id), run_id=str(run_id), estado=estado, codigo=codigo, cerrada=fila is not None)
    return fila is not None


async def _sumar_gasto(tenant_id: uuid.UUID, run_id: uuid.UUID, creditos: Decimal) -> Decimal:
    """Suma lo gastado al total de la corrida y devuelve el total (un `UPDATE` atómico: sin carrera con el cierre)."""
    async with get_session() as session:
        total = (
            await session.execute(
                update(AgentTestRun)
                .where(AgentTestRun.tenant_id == tenant_id, AgentTestRun.id == run_id)
                .values(creditos_reales=AgentTestRun.creditos_reales + creditos, updated_at=datetime.now(UTC))
                .returning(AgentTestRun.creditos_reales)
            )
        ).scalar_one_or_none()
    return total if total is not None else Decimal(0)


# ------------------------------------------------------------------------------------------------- contexto de la corrida
def _hash(contenido: Any) -> str:
    return hashlib.sha256(json.dumps(contenido, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def _cargar(corrida: AgentTestRun) -> _Corrida | None:
    """Escenarios, conexión de pruebas y versión (el borrador). None si el agente no se puede probar."""
    from core.admin.playground import _exigir_borrador
    from core.gateway.conexion_pruebas import asegurar_conexion_de_pruebas
    from core.releases.efimera import BorradorNoEncontrado

    tenant_id, agent_id = corrida.tenant_id, corrida.agent_id
    pedidos = [uuid.UUID(str(i)) for i in (corrida.version_probada or {}).get("escenarios", [])]
    async with get_session() as session:
        try:
            await _exigir_borrador(session, tenant_id, agent_id)
        except BorradorNoEncontrado:
            return None
        borrador = (
            await session.execute(
                select(AgentDraftRecord.content, AgentDraftRecord.lock_version).where(
                    AgentDraftRecord.tenant_id == tenant_id,
                    AgentDraftRecord.agent_id == agent_id,
                    AgentDraftRecord.deleted_at.is_(None),
                )
            )
        ).first()
        if borrador is None:
            return None
        filas = {
            c.id: c
            for c in (
                await session.execute(
                    select(AgentTestCase).where(
                        AgentTestCase.tenant_id == tenant_id,
                        AgentTestCase.agent_id == agent_id,
                        AgentTestCase.deleted_at.is_(None),
                        AgentTestCase.estado == "aprobado",
                        AgentTestCase.id.in_(pedidos),
                    )
                )
            ).scalars()
        }
        conexion = await asegurar_conexion_de_pruebas(session, tenant_id)
    # Un escenario que dejó de estar aprobado o se borró entre la aprobación y la corrida se omite, no se corre.
    casos = [
        _Caso(
            c.id,
            c.titulo,
            c.objetivo_cliente,
            dict(c.datos_cliente or {}),
            list(c.criterios or []),
            list(c.tools_esperadas or []),
            list(c.tools_prohibidas or []),
        )
        for i in pedidos
        if (c := filas.get(i)) is not None
    ]
    return _Corrida(
        tenant_id=tenant_id,
        run_id=corrida.id,
        agent_id=agent_id,
        k=int(corrida.k),
        tope=Decimal(corrida.tope_creditos),
        casos=casos,
        conexion_id=conexion,
        version={"tipo": "borrador", "lock_version": borrador[1], "hash": _hash(borrador[0])},
    )


# ------------------------------------------------------------------------------------- conversación de la corrida
async def crear_conversacion_de_corrida(
    tenant_id: uuid.UUID, run_id: uuid.UUID, case_id: uuid.UUID, agent_id: uuid.UUID, conexion_id: uuid.UUID
) -> uuid.UUID:
    """Conversación `test` propia de la corrida (nunca la viva del playground, ADR-0212 §2). El cliente sintético es
    de la corrida: `purge_test_conversation` lo borra cuando se queda sin conversaciones."""
    from core.releases.efimera import CLAVE_BORRADOR

    externo = f"{PREFIJO_CLIENTE}{run_id}"
    async with get_session() as session:
        cliente = (
            await session.execute(
                select(Customer).where(
                    Customer.tenant_id == tenant_id,
                    Customer.channel == TEST_CHANNEL,
                    Customer.external_id == externo,
                    Customer.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if cliente is None:
            cliente = Customer(
                tenant_id=tenant_id, channel=TEST_CHANNEL, external_id=externo, name="Prueba del agente"
            )
            session.add(cliente)
            await session.flush()
        conversacion = Conversation(
            tenant_id=tenant_id,
            customer_id=cliente.id,
            channel=TEST_CHANNEL,
            status="open",
            state={
                "is_test": True,
                "panel": True,  # sandbox de tools (HU-ARCH-14)
                "prueba_corrida": str(run_id),
                "prueba_caso": str(case_id),
                CLAVE_BORRADOR: str(agent_id),
            },
            connection_id=conexion_id,
        )
        session.add(conversacion)
        await session.flush()
        return conversacion.id


async def consolidar_y_purgar(
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    case_id: uuid.UUID | None = None,
    intento: int | None = None,
    extra_creditos: Decimal = Decimal(0),
    extra_usd: Decimal = Decimal(0),
    extra_input: int = 0,
    extra_output: int = 0,
) -> tuple[Decimal, list[str]]:
    """UNA transacción: suma los turnos de la conversación, le suma `extra_*` (el cliente simulado, que llama al
    proveedor directo y no escribe en el ledger), escribe la fila `prueba_agente` sin conversación y purga.
    Devuelve `(créditos brutos consolidados, tools que el agente llamó)`. Si la conversación ya no existe o no es de
    esta corrida y negocio (otra purga la ganó) lanza `ConversacionAusente` sin escribir nada.

    Los créditos de cada turno son los del propio ledger; en filas sin `credits` se usa la fórmula de siempre
    (`medir_creditos`), así que no se cobra distinto de lo que mediría el cubo.
    """
    from core.billing.creditos import _creditos_de_la_fila
    from core.billing.tarifario import TARIFARIO_V1, TARIFARIO_VIGENTE, trm_vigente
    from core.mcp.sandbox import SandboxPrueba
    from core.memory import working as working_memory

    trm = trm_vigente()
    async with get_session() as session:
        # La conversación debe ser de ESTE negocio y de ESTA corrida: nada se consolida ni se purga por un id ajeno.
        dueno = (
            await session.execute(
                select(Conversation.id).where(
                    Conversation.tenant_id == tenant_id,
                    Conversation.id == conversation_id,
                    Conversation.channel == TEST_CHANNEL,
                    Conversation.state["prueba_corrida"].astext == str(run_id),
                )
            )
        ).scalar_one_or_none()
        if dueno is None:
            raise ConversacionAusente(str(conversation_id))
        creditos_fila = _creditos_de_la_fila(trm, TARIFARIO_V1)
        totales = (
            await session.execute(
                select(
                    func.coalesce(func.sum(creditos_fila), 0),
                    func.coalesce(func.sum(UsageLedger.cost_usd), 0),
                    func.coalesce(func.sum(UsageLedger.input_tokens), 0),
                    func.coalesce(func.sum(UsageLedger.output_tokens), 0),
                    func.count(UsageLedger.id),
                ).where(
                    UsageLedger.tenant_id == tenant_id,
                    UsageLedger.conversation_id == conversation_id,
                    UsageLedger.kind != "eval",
                )
            )
        ).one()
        filas = (
            await session.execute(
                select(UsageLedger.tool_calls, UsageLedger.agent_instance_id).where(
                    UsageLedger.tenant_id == tenant_id, UsageLedger.conversation_id == conversation_id
                )
            )
        ).all()
        tools = sorted(
            {str(t["tool"]) for llamadas, _ in filas for t in (llamadas or []) if isinstance(t, dict) and t.get("tool")}
        )
        instancia = next((i for _, i in filas if i is not None), None)
        creditos = Decimal(totales[0]) + extra_creditos
        if creditos > 0 or int(totales[4]) or extra_usd > 0:
            session.add(
                UsageLedger(
                    tenant_id=tenant_id,
                    conversation_id=None,
                    kind="prueba_agente",
                    model="prueba-consolidada",
                    input_tokens=int(totales[2]) + extra_input,
                    output_tokens=int(totales[3]) + extra_output,
                    cost_usd=Decimal(totales[1]) + extra_usd,
                    credits=creditos.quantize(Decimal("0.1")),
                    key_mode="platform",
                    tarifario_version=TARIFARIO_VIGENTE.version,
                    trm_cop=trm,
                    agent_instance_id=instancia,  # NULL en el borrador: su instancia no existe en la base
                    result={
                        "concepto": "turnos",
                        "run_id": str(run_id),
                        "case_id": str(case_id) if case_id else None,
                        "intento": intento,
                        "turnos_del_agente": int(totales[4]),
                    },
                )
            )
        await session.execute(
            text("select purge_test_conversation(:t, :c)"), {"t": str(tenant_id), "c": str(conversation_id)}
        )
    # Fuera de la transacción: Redis no es transaccional y borrar de más/menos no cambia el cobro.
    await working_memory.clear(conversation_id)
    await SandboxPrueba(tenant_id, conversation_id).limpiar()
    return creditos.quantize(Decimal("0.1")), tools


# ------------------------------------------------------------------------------------------------------ un intento
def _credito_del_simulador(proveedor: Any, respuesta: Any) -> tuple[Decimal, Decimal, int, int]:
    """`(créditos, USD, tokens de entrada, de salida)` de una llamada del cliente simulado. Siempre con la llave de la
    plataforma. Un proveedor sin tarifas cuenta 0 (como `generar_estructurado`)."""
    from core.billing.tarifario import creditos_por_evento, trm_vigente
    from core.llm.base import costo_de_respuesta

    uso = getattr(respuesta, "usage", None)
    entrada = int(getattr(uso, "input_tokens", 0) or 0)
    salida = int(getattr(uso, "output_tokens", 0) or 0)
    try:
        usd = Decimal(str(costo_de_respuesta(proveedor, uso))) if hasattr(proveedor, "estimate_cost_usd") else Decimal(0)
    except Exception:
        usd = Decimal(0)
    creditos = creditos_por_evento(usd, "agent_run", "platform", trm_vigente()) if usd > 0 else Decimal(0)
    return Decimal(creditos), usd, entrada, salida


def _proveedor_del_simulador() -> Any:
    from core.config import get_settings
    from core.llm import resolve_provider

    return resolve_provider(model=get_settings().pruebas_modelo or None, thinking=False)


def _semilla(run_id: uuid.UUID, case_id: uuid.UUID, intento: int) -> int:
    """Determinista por (corrida, escenario, intento): los k intentos usan variantes distintas del cliente."""
    return int(hashlib.sha256(f"{run_id}:{case_id}:{intento}".encode()).hexdigest()[:8], 16)


async def _turno(ctx: _Corrida, conversation_id: uuid.UUID, texto: str) -> Any:
    from core.admin.playground import PlaygroundError, _guardar_mensaje
    from core.orchestrator.loop import process_conversation

    await _guardar_mensaje(ctx.tenant_id, conversation_id, "inbound", texto)
    try:
        resultado = await process_conversation(conversation_id, texto)
    except ValueError as exc:
        raise PlaygroundError(str(exc)) from exc
    if resultado is not None and resultado.text:
        await _guardar_mensaje(ctx.tenant_id, conversation_id, "outbound", resultado.text)
    return resultado


async def _chequear_corte(ctx: _Corrida, conversation_id: uuid.UUID) -> None:
    """Antes de cada turno: ¿la cancelaron o se cerró por otro lado? ¿el turno saldría `agotado` por saldo?"""
    from core.billing.enforcement import turn_quota_metric

    estado = await _estado(ctx.tenant_id, ctx.run_id)
    if estado != "corriendo":
        raise _Corte(estado or FALLO)
    decision = await turn_quota_metric(ctx.tenant_id, conversation_id)
    if decision.salida == "agotado":
        raise _Corte(POR_SALDO)


async def _correr_conversacion(
    ctx: _Corrida, caso: _Caso, intento: int, conversation_id: uuid.UUID, parcial: _Intento, gasto: _Gasto,
    sin_simulador: list[str],
) -> None:
    """La conversación entre el cliente simulado y el agente. Rellena `parcial` (transcripción y cómo terminó),
    `gasto` (el cliente simulado) y `sin_simulador` (tools con efectos que el sandbox no pudo simular)."""
    from core.admin.playground import PlaygroundError
    from core.pruebas.simulador import CUMPLIDO, RINDE, ClienteSimulado
    from core.shared.test_mode import capturar_sin_simulador

    cliente = ClienteSimulado(caso.objetivo, caso.datos, estilo=ESTILO, semilla=_semilla(ctx.run_id, caso.id, intento))
    proveedor = _proveedor_del_simulador()

    def _cobrar(respuesta: Any) -> None:
        c, usd, i, o = _credito_del_simulador(proveedor, respuesta)
        gasto.creditos += c
        gasto.usd += usd
        gasto.entrada += i
        gasto.salida += o

    with capturar_sin_simulador() as vistas:
        try:
            turnos = 0
            while True:
                await _chequear_corte(ctx, conversation_id)
                texto = await cliente.siguiente(proveedor)
                _cobrar(cliente.ultima_respuesta)
                if texto == CUMPLIDO:
                    parcial.fin = "objetivo"
                    break
                if texto == RINDE or not texto:
                    parcial.fin = "se_rindio"
                    break
                cliente.anotar("cliente", texto)
                parcial.transcripcion.append({"quien": "cliente", "texto": texto})
                resultado = await _turno(ctx, conversation_id, texto)
                if resultado is None or not resultado.text:
                    # Sin respuesta (agente en pausa, sin release, turno cortado): no es una falla de lo que se probó.
                    parcial.razon = "El agente no respondió en la prueba."
                    break
                cliente.anotar("agente", resultado.text)
                parcial.transcripcion.append({"quien": "agente", "texto": resultado.text})
                turnos += 1
                if resultado.escalated:
                    # El agente pasó la conversación a una persona: ya no hay más turnos. La tabla solo distingue
                    # objetivo | se_rindio | tope, y esto es lo más cercano a «no siguió».
                    parcial.fin = "se_rindio"
                    break
                if turnos >= TOPE_TURNOS:
                    parcial.fin = "tope"
                    break
        except _Corte:
            raise
        except Exception as exc:  # el simulador o el turno fallaron: ese intento no se pudo probar, la corrida sigue
            log.warning("prueba_intento_fallo", run_id=str(ctx.run_id), error=type(exc).__name__)
            parcial.fin = None
            # Texto fijo: el mensaje de la excepción es interno y no debe llegar al panel.
            parcial.razon = (
                "El turno no se pudo procesar."
                if isinstance(exc, PlaygroundError)
                else "La prueba se interrumpió por un error interno."
            )
        finally:
            sin_simulador.extend(vistas)


async def _juzgar(caso: _Caso, parcial: _Intento, sin_simulador: list[str], ctx: _Corrida, intento: int) -> None:
    """Rellena `estado`, criterio y razón del intento. Orden: tool prohibida → tool sin simulador → tool esperada
    ausente → juez LLM. Lo determinista no llama al juez; lo que no se pudo probar tampoco."""
    from core.ia.estructurado import ErrorFormato, ErrorIA
    from core.pruebas import juez

    if parcial.fin is None:  # el agente no respondió o el turno no se pudo procesar: no hay nada que juzgar
        parcial.estado = "no_se_pudo_probar"
        return
    prohibida = juez.chequeo_de_tools(parcial.tools, [], caso.prohibidas)
    if prohibida is not None:
        parcial.estado, parcial.criterio, parcial.razon = "falla", prohibida.criterio_incumplido, prohibida.razon
        return
    if sin_simulador:
        nombres = ", ".join(sorted(set(sin_simulador)))
        parcial.estado = "no_se_pudo_probar"
        parcial.razon = (
            f"La acción «{nombres}» hace cambios reales y no tiene simulación: no se pudo probar sin ejecutarla."
        )
        return
    esperada = juez.chequeo_de_tools(parcial.tools, caso.esperadas, [])
    if esperada is not None:
        parcial.estado, parcial.criterio, parcial.razon = "falla", esperada.criterio_incumplido, esperada.razon
        return
    if not caso.criterios:  # solo había chequeo de tools y pasó
        parcial.estado, parcial.razon = "pasa", "El agente usó las tools como se esperaba."
        return
    try:
        v = await juez.juzgar(
            ctx.tenant_id,
            run_id=ctx.run_id,
            case_id=caso.id,
            transcripcion=parcial.transcripcion,
            tools_llamadas=parcial.tools,
            criterios=caso.criterios,
        )
    except ErrorIA as exc:
        # Un juez caído o una salida que no valida no es una falla del agente. El proveedor ya cobró si respondió.
        if isinstance(exc, ErrorFormato) and exc.consumo.credits:
            parcial.creditos += Decimal(exc.consumo.credits)
        parcial.estado, parcial.razon = "no_se_pudo_probar", "El juez no pudo evaluar la conversación."
        log.warning("prueba_juez_sin_veredicto", run_id=str(ctx.run_id), motivo=exc.motivo, causa=exc.causa)
        return
    parcial.creditos += v.creditos
    parcial.estado = "pasa" if v.pasa else "falla"
    parcial.criterio, parcial.razon = v.criterio_incumplido, v.razon


async def _guardar_resultado(ctx: _Corrida, caso: _Caso, intento: int, r: _Intento) -> None:
    async with get_session() as session:
        session.add(
            AgentTestResult(
                tenant_id=ctx.tenant_id,
                run_id=ctx.run_id,
                case_id=caso.id,
                intento=intento,
                estado=r.estado,
                fin=r.fin,
                transcripcion=r.transcripcion,
                tools_llamadas=r.tools,
                criterio_incumplido=(r.criterio or None),
                razon=(r.razon or None),
            )
        )


async def _correr_intento(ctx: _Corrida, caso: _Caso, intento: int) -> _Intento:
    """Un intento completo: conversación, consolidación + purga (siempre, aunque falle o la corten) y juicio."""
    conversation_id = await crear_conversacion_de_corrida(
        ctx.tenant_id, ctx.run_id, caso.id, ctx.agent_id, ctx.conexion_id
    )
    parcial = _Intento("no_se_pudo_probar")
    gasto = _Gasto()
    sin_simulador: list[str] = []
    corte: _Corte | None = None
    try:
        await _correr_conversacion(ctx, caso, intento, conversation_id, parcial, gasto, sin_simulador)
    except _Corte as exc:
        corte = exc
    finally:
        # Siempre: sin esto se perdería el cobro de lo gastado (la purga borra los turnos).
        try:
            consolidado, tools = await consolidar_y_purgar(
                ctx.tenant_id,
                conversation_id,
                run_id=ctx.run_id,
                case_id=caso.id,
                intento=intento,
                extra_creditos=gasto.creditos,
                extra_usd=gasto.usd,
                extra_input=gasto.entrada,
                extra_output=gasto.salida,
            )
        except ConversacionAusente:
            # Otro proceso (el barrido) la purgó: no hay tools ni gasto que medir, y juzgar sin ellas daría falsos
            # verdes o rojos. El intento no se pudo probar.
            consolidado, tools = Decimal(0), []
            parcial.fin = None
            parcial.razon = "La conversación de la prueba se cerró antes de terminar."
    parcial.creditos += consolidado
    parcial.tools = tools
    if corte is not None:
        # Cortada a mitad: el gasto ya se cobró, pero no se juzga ni se guarda un resultado a medias.
        await _sumar_gasto(ctx.tenant_id, ctx.run_id, parcial.creditos)
        raise corte
    await _juzgar(caso, parcial, sin_simulador, ctx, intento)
    await _guardar_resultado(ctx, caso, intento, parcial)
    return parcial


# ----------------------------------------------------------------------------------------------------------- la tarea
async def _negocio_activo(tenant_id: uuid.UUID) -> bool:
    async with get_session() as session:
        estado = (
            await session.execute(select(Tenant.status).where(Tenant.id == tenant_id, Tenant.deleted_at.is_(None)))
        ).scalar_one_or_none()
    return estado is not None and estado != "suspended"


async def correr(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str:
    """El trabajo de la tarea. Devuelve el estado en el que quedó la corrida (`no_tomada` si no era `en_cola`)."""
    from core.config import get_settings

    async with tenant_scope(tenant_id):
        corrida = await _reclamar(tenant_id, run_id)
        if corrida is None:
            log.info("prueba_corrida_no_tomada", tenant_id=str(tenant_id), run_id=str(run_id))
            return "no_tomada"
        if not get_settings().pruebas_agente_enabled:
            await cerrar(tenant_id, run_id, FALLO, codigo="apagada")
            return FALLO
        if not await _negocio_activo(tenant_id):
            await cerrar(tenant_id, run_id, FALLO, codigo="negocio_inactivo")
            return FALLO
        try:
            ctx = await _cargar(corrida)
            if ctx is None or not ctx.casos:
                await cerrar(tenant_id, run_id, FALLO, codigo="agente_sin_borrador" if ctx is None else "sin_escenarios")
                return FALLO
            await _guardar_version(ctx)
            final = await _recorrer(ctx)
        except Exception as exc:  # cualquier fallo del corredor deja la corrida cerrada, no colgada
            # Sin traza: un error de SQL trae los parámetros de la fila (la transcripción).
            log.error("prueba_corrida_fallo", tenant_id=str(tenant_id), run_id=str(run_id), error=type(exc).__name__)
            await cerrar(tenant_id, run_id, FALLO, codigo=type(exc).__name__)
            return FALLO
        await cerrar(tenant_id, run_id, final)
        return (await _estado(tenant_id, run_id)) or final


async def _guardar_version(ctx: _Corrida) -> None:
    async with get_session() as session:
        await session.execute(
            update(AgentTestRun)
            .where(AgentTestRun.tenant_id == ctx.tenant_id, AgentTestRun.id == ctx.run_id)
            .values(version_probada=AgentTestRun.version_probada.op("||")(cast(literal(json.dumps({"version": ctx.version})), JSONB)))
        )


async def _recorrer(ctx: _Corrida) -> str:
    """Todos los escenarios y sus intentos, en orden. Devuelve el estado final; corta entre intentos por tope."""
    for caso in ctx.casos:
        for intento in range(1, ctx.k + 1):
            try:
                resultado = await _correr_intento(ctx, caso, intento)
            except _Corte as corte:
                return corte.estado
            total = await _sumar_gasto(ctx.tenant_id, ctx.run_id, resultado.creditos)
            # Un tope en 0 es un estimado de 0 (nada que cobrar): no corta nada, `estimar_corrida` nunca lo deja así.
            if ctx.tope > 0 and total > ctx.tope:
                return POR_TOPE
    return TERMINADA


__all__ = [
    "ACTIVAS",
    "CANCELADA",
    "FALLO",
    "POR_SALDO",
    "POR_TOPE",
    "TERMINADA",
    "TOPE_TURNOS",
    "ConversacionAusente",
    "cerrar",
    "consolidar_y_purgar",
    "correr",
    "crear_conversacion_de_corrida",
]
