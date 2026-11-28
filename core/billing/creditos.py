"""Medición del crédito mensual (HU-BILL-04/06, D-35, ADR-0144).

El consumo en créditos de un negocio en una ventana se **deriva** de sus tablas fuente (ADR-0045,
sin contador paralelo):

- **IA y medios** (`usage_ledger`): la suma de `credits`, congelados al escribir cada fila con el
  tarifario y la TRM vigentes (ADR-0208 §11). Las filas anteriores a la migración 0096 (`credits`
  NULL, sin `tarifario_version`) se calculan al leer con el tarifario `v1` y la TRM actual, como
  plataforma (ADR-0208 §14). Cuentan todas las filas salvo `kind = 'eval'` (ADR-0117).
- **Notas de voz e imágenes** (`messages`): el fee de `v1` (0,5 · 1,0) solo para los mensajes
  anteriores a `vigente_desde` de `v2`, cuando la ingesta escribía en sombra (`credits = 0`). Desde
  esa fecha cobra su fila `stt`/`vision` del ledger (ADR-0208 §12). Las **plantillas** siguen con su
  fee desde `messages`. Ver `medir_creditos` (playground, sin cobro doble).
- **Desglose por `kind`** (HU-BILL-16): las filas `vision` y `stt` del ledger suman a su concepto
  y el resto (`turn`, `agent_run`, `embedding`, `generacion`) a `llm_turn`. Las fotos de catálogo
  (`kind = 'vision'` con créditos reales, ADR-0194) aparecen así como «Imágenes» y no como «IA».

Acciones de IA cotizadas (HU-BILL-16, ADR-0194): `cotizar` dice cuánto cuesta antes de gastar,
`alcanza` verifica el saldo del mes y `imagen_ya_cobrada` evita cobrar dos veces la misma foto.
`decidir_turno` es la lectura del turno (HU-BILL-18/20): el mismo cálculo, sin lock ni excepción, más el
colchón de las conversaciones abiertas (ADR-0208 §4-5).

El playground del panel (conversaciones del canal `test`) es gratis hasta
`PLAYGROUND_GRATIS_POR_CICLO` créditos por ciclo (D-79, ADR-0208 §15); lo que pasa de ahí suma
al consumo del negocio.
Todo corre dentro de `tenant_scope` (RLS) y además filtra `tenant_id` explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from sqlalchemy import and_, case, func, literal, or_, select, text

from core.billing.tarifario import (
    TARIFARIO_V1,
    TARIFARIO_V2,
    Concepto,
    KeyMode,
    Tarifario,
    creditos_por_evento,
    trm_vigente,
)
from core.billing.tipos_de_uso import PRUEBA_AGENTE
from core.db.models import AgentInstanceRecord, AgentRecord, Conversation, Message, UsageLedger
from core.shared.logger import get_logger
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

# Estados de una plantilla que de verdad salió (Meta la aceptó). `queued` y `failed` no cobran.
ESTADOS_PLANTILLA_ENVIADA = ("sent", "delivered", "read")
# Filas del ledger que tienen concepto propio en el desglose; el resto es «IA» (`llm_turn`).
_KINDS_CON_CONCEPTO = ("vision", "stt", "tts")
# Playground del panel (canal `test`): gratis hasta 300 créditos por ciclo (D-79, ADR-0208 §15). Lo
# que pasa de ahí suma al consumo del negocio: `max(0, consumo del playground − 300)`, en la misma
# ventana y sin contador.
PLAYGROUND_GRATIS_POR_CICLO = Decimal(300)


@dataclass(frozen=True)
class CreditoPorAgente:
    agent_id: uuid.UUID | None  # None = consumo sin agente (turnos legacy y fees de medios)
    name: str | None
    used: Decimal


@dataclass(frozen=True)
class ConsumoDeCreditos:
    total: Decimal
    por_concepto: dict[str, Decimal]
    por_agente: list[CreditoPorAgente] = field(default_factory=list)
    # Playground (ADR-0208 §15): consumo bruto en la ventana y lo que queda del gratuito del ciclo.
    # Lo cobrado (lo que pasa de 300) ya está en `total` y en `por_concepto["playground"]`.
    playground_usado: Decimal = Decimal(0)
    playground_gratis_restante: Decimal = PLAYGROUND_GRATIS_POR_CICLO


def _de_playground(tenant_id: uuid.UUID, columna):  # type: ignore[no-untyped-def]
    """Filas del playground VIVO del panel: conversaciones del canal `test` que NO son de una corrida de
    pruebas (`state.prueba_corrida`, ADR-0212 §2). Una fila sin conversación no es del playground (`IN`
    con NULL da NULL, que un `case` trata como falso)."""
    de_prueba = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id,
        Conversation.channel == TEST_CHANNEL,
        Conversation.state["prueba_corrida"].astext.is_(None),
    )
    return columna.in_(de_prueba)


def _de_corrida(tenant_id: uuid.UUID, columna):  # type: ignore[no-untyped-def]
    """Filas de la conversación de una corrida de pruebas EN CURSO. Su gasto se consolida en una fila
    `prueba_agente` antes de purgarla (ADR-0212 §4); mientras vive no suma a nada, para no contarse dos veces."""
    de_corrida = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id,
        Conversation.channel == TEST_CHANNEL,
        Conversation.state["prueba_corrida"].astext.is_not(None),
    )
    return columna.in_(de_corrida)


def _sin_playground(tenant_id: uuid.UUID, columna):  # type: ignore[no-untyped-def]
    """Excluye las filas del playground y de las corridas de pruebas; las que no tienen conversación se
    conservan (`NOT IN` con NULL descartaría filas legítimas). El playground no desaparece del crédito: se
    mide aparte y cobra lo que pasa de `PLAYGROUND_GRATIS_POR_CICLO` (`medir_creditos`)."""
    return and_(
        or_(columna.is_(None), ~_de_playground(tenant_id, columna)),
        or_(columna.is_(None), ~_de_corrida(tenant_id, columna)),
    )


def _creditos_de_la_fila(trm: Decimal, tarifario: Tarifario):  # type: ignore[no-untyped-def]
    """`credits` congelado o, en filas previas a 0096, la fórmula del tarifario en SQL:
    ⌈costo × TRM × (1 + margen)⌉ / 10 (= ⌈costo × TRM × (1+m) / 10⌉ a 0,1) + fee del turno.
    Sin tokens no hubo llamada al LLM: 0. El fee de turno solo aplica a `kind = 'turn'`."""
    factor = trm * (1 + tarifario.margen)
    fee_turno = case((UsageLedger.kind == "turn", literal(tarifario.fees["llm_turn"])), else_=literal(Decimal(0)))
    hubo_llamada = (UsageLedger.input_tokens + UsageLedger.output_tokens) > 0
    return case(
        (UsageLedger.credits.is_not(None), UsageLedger.credits),
        (hubo_llamada, func.ceil(UsageLedger.cost_usd * literal(factor)) / 10 + fee_turno),
        else_=literal(Decimal(0)),
    )


async def medir_creditos(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    *,
    desde: datetime | None,
    hasta: datetime | None = None,
    por_agente: bool = False,
) -> ConsumoDeCreditos:
    """Créditos consumidos por el negocio en `[desde, hasta)`. La sesión debe estar dentro del
    `tenant_scope(tenant_id)` del llamador: con otro scope la RLS devolvería 0 en silencio, así que
    se rechaza.

    - **Ledger:** `credits` de cada fila; las filas con `credits` NULL (previas a 0096, sin
      `tarifario_version`) se calculan con `v1`, nunca con el vigente (ADR-0208 §14).
    - **Sin cobro doble** (ADR-0208 §12): el fee de nota de voz e imagen se cuenta desde `messages`
      solo para los mensajes anteriores a `vigente_desde` de `v2`; desde ahí cobra su fila del
      ledger. Las plantillas cobran desde `messages` con el fee del tarifario de su fecha.
    - **Playground** (ADR-0208 §15): sus filas (ledger y medios del canal `test`) desde
      `vigente_desde` de `v2` suman aparte; al negocio solo se le cobra lo que pasa de 300 en la
      ventana, en `por_concepto["playground"]` y en el renglón sin agente. Lo anterior a `v2`
      queda fuera, como antes: los ciclos cerrados no cambian.
    - **Pruebas del agente** (ADR-0212 §4): las filas `prueba_agente` suman al mismo cubo que el
      playground, así que los 300 créditos gratis son compartidos. Las conversaciones de una corrida en
      curso no suman (se consolidan en una fila `prueba_agente` antes de purgarlas).
    - `eval` nunca cuenta (ADR-0117)."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise RuntimeError("medir_creditos fuera del tenant_scope del negocio medido")
    trm = trm_vigente()
    v2_desde = TARIFARIO_V2.vigente_desde
    creditos = _creditos_de_la_fila(trm, TARIFARIO_V1)

    filtros_ledger = [UsageLedger.tenant_id == tenant_id, UsageLedger.kind != "eval"]
    if desde is not None:
        filtros_ledger.append(UsageLedger.created_at >= desde)
    if hasta is not None:
        filtros_ledger.append(UsageLedger.created_at < hasta)
    filtros_ledger_negocio = [
        *filtros_ledger,
        _sin_playground(tenant_id, UsageLedger.conversation_id),
        UsageLedger.kind != PRUEBA_AGENTE,
    ]

    # Una sola lectura del ledger: concepto × «es del playground». El playground anterior a `v2`
    # sale como «excluido» y no suma a nada (antes de `v2` el playground no consumía).
    concepto_de_la_fila = case(
        (UsageLedger.kind.in_(_KINDS_CON_CONCEPTO), UsageLedger.kind), else_=literal("llm_turn")
    )
    # Las pruebas del agente (`prueba_agente`, ADR-0212 §4) comparten el cubo del playground: una sola
    # fórmula, `max(0, playground_vivo + pruebas − 300)`, derivada del ledger y sin contador aparte. Las
    # conversaciones de una corrida en curso quedan «excluidas» hasta que se consolidan.
    lugar = case(
        (UsageLedger.kind == PRUEBA_AGENTE, literal("playground")),
        (
            _de_playground(tenant_id, UsageLedger.conversation_id),
            case((UsageLedger.created_at >= v2_desde, literal("playground")), else_=literal("excluido")),
        ),
        (_de_corrida(tenant_id, UsageLedger.conversation_id), literal("excluido")),
        else_=literal("negocio"),
    )
    del_ledger: dict[str, Decimal] = {c: Decimal(0) for c in ("llm_turn", *_KINDS_CON_CONCEPTO)}
    playground = Decimal(0)
    for donde, concepto, usado in (
        await session.execute(
            select(lugar, concepto_de_la_fila, func.coalesce(func.sum(creditos), 0))
            .where(*filtros_ledger)
            .group_by(lugar, concepto_de_la_fila)
        )
    ).all():
        if donde == "negocio":
            del_ledger[concepto] = Decimal(usado)
        elif donde == "playground":
            playground += Decimal(usado)

    agentes: list[CreditoPorAgente] = []
    if por_agente:
        # Por agente, no por instancia: un agente en dos conexiones es uno (ADR-0142 §1).
        filas = (
            await session.execute(
                select(AgentRecord.id, AgentRecord.name, func.coalesce(func.sum(creditos), 0))
                .select_from(UsageLedger)
                .outerjoin(
                    AgentInstanceRecord,
                    and_(
                        AgentInstanceRecord.tenant_id == UsageLedger.tenant_id,
                        AgentInstanceRecord.id == UsageLedger.agent_instance_id,
                    ),
                )
                .outerjoin(
                    AgentRecord,
                    and_(
                        AgentRecord.tenant_id == AgentInstanceRecord.tenant_id,
                        AgentRecord.id == AgentInstanceRecord.agent_id,
                    ),
                )
                .where(*filtros_ledger_negocio)
                .group_by(AgentRecord.id, AgentRecord.name)
            )
        ).all()
        agentes = [CreditoPorAgente(agent_id=a, name=n, used=Decimal(u)) for a, n, u in filas]

    # Fees de medios y plantillas. Sin filtrar `deleted_at`: borrar un mensaje (retención) no
    # devuelve un consumo que ya ocurrió.
    filtros_msg = [Message.tenant_id == tenant_id]
    if desde is not None:
        filtros_msg.append(Message.created_at >= desde)
    if hasta is not None:
        filtros_msg.append(Message.created_at < hasta)
    negocio = _sin_playground(tenant_id, Message.conversation_id)
    del_playground = _de_playground(tenant_id, Message.conversation_id)
    entrante = Message.direction == "inbound"
    audio = and_(entrante, Message.media_type.like("audio/%"), Message.created_at < v2_desde)
    imagen = and_(entrante, Message.media_type.like("image/%"), Message.created_at < v2_desde)
    plantilla = and_(
        Message.direction == "outbound",
        Message.template_name.is_not(None),
        Message.status.in_(ESTADOS_PLANTILLA_ENVIADA),
    )
    v1, v2 = Message.created_at < v2_desde, Message.created_at >= v2_desde
    audios, imagenes, plantillas_v1, plantillas_v2, plantillas_playground = (
        await session.execute(
            select(
                func.count(Message.id).filter(negocio, audio),
                func.count(Message.id).filter(negocio, imagen),
                func.count(Message.id).filter(negocio, plantilla, v1),
                func.count(Message.id).filter(negocio, plantilla, v2),
                # Medios del playground: antes de `v2` no consumía, y desde `v2` voz e imagen van
                # en el ledger. Solo quedan sus plantillas desde `v2`.
                func.count(Message.id).filter(del_playground, plantilla, v2),
            ).where(*filtros_msg)
        )
    ).one()

    fees_stt = TARIFARIO_V1.fees["stt"] * int(audios or 0)
    fees_vision = TARIFARIO_V1.fees["vision"] * int(imagenes or 0)
    fees_plantillas = (
        TARIFARIO_V1.fees["template_sent"] * int(plantillas_v1 or 0)
        + TARIFARIO_V2.fees["template_sent"] * int(plantillas_v2 or 0)
    )
    playground += TARIFARIO_V2.fees["template_sent"] * int(plantillas_playground or 0)
    playground_cobrado = max(Decimal(0), playground - PLAYGROUND_GRATIS_POR_CICLO)
    por_concepto = {
        "llm_turn": del_ledger["llm_turn"],
        "stt": del_ledger["stt"] + fees_stt,
        "vision": del_ledger["vision"] + fees_vision,
        "tts": del_ledger["tts"],
        "template_sent": fees_plantillas,
        "playground": playground_cobrado,
    }
    total = sum(por_concepto.values(), Decimal(0))
    if por_agente:
        # Lo del ledger ya está en `agentes`: solo se agregan los fees que salen de `messages` y lo
        # cobrado del playground, en el renglón sin agente.
        medios = fees_stt + fees_vision + fees_plantillas + playground_cobrado
        if medios:
            sin_agente = next((a for a in agentes if a.agent_id is None), None)
            agentes = [a for a in agentes if a.agent_id is not None]
            agentes.append(
                CreditoPorAgente(None, None, (sin_agente.used if sin_agente else Decimal(0)) + medios)
            )
        agentes.sort(key=lambda a: (a.agent_id is None, -a.used))
    return ConsumoDeCreditos(
        total=total,
        por_concepto=por_concepto,
        por_agente=agentes,
        playground_usado=playground,
        playground_gratis_restante=max(Decimal(0), PLAYGROUND_GRATIS_POR_CICLO - playground),
    )


def creditos_enteros(valor: Decimal) -> int:
    """Créditos para mostrar: redondeo al entero más cercano (la medición guarda décimas)."""
    return int(valor.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def saldo_entero(limite: int | Decimal) -> int:
    """Saldo para mostrar, hacia abajo: con abonos lleva décimas y nunca se muestra más de lo que hay."""
    return int(Decimal(limite).to_integral_value(rounding=ROUND_DOWN))


# --------------------------------------------- acciones de IA cotizadas (HU-BILL-16, ADR-0194)


def _costo_de_referencia_usd(concepto: Concepto) -> Decimal:
    """Costo en USD de UNA unidad con la llave de la plataforma, para cotizar antes de gastar.
    Solo los conceptos que se cotizan tienen referencia; el resto se mide al escribir."""
    from core.config import get_settings

    if concepto == "vision":
        return Decimal(str(get_settings().billing_vision_costo_referencia_usd))
    raise ValueError(f"el concepto {concepto!r} no se cotiza")


def cotizar(concepto: Concepto, unidades: int, key_mode: KeyMode) -> int:
    """Créditos enteros que cuesta hacer `unidades` de `concepto`. Con llave propia (BYOK) solo
    el fee fijo; con la de la plataforma, el costo de referencia más margen y fee
    (`creditos_por_evento`). Se redondea hacia ARRIBA: lo cotizado nunca queda por debajo de lo
    que después se registra con el costo real de una llamada típica."""
    if unidades < 0:
        raise ValueError("unidades no puede ser negativo")
    por_unidad = creditos_por_evento(_costo_de_referencia_usd(concepto), concepto, key_mode, trm_vigente())
    return int((por_unidad * unidades).to_integral_value(rounding=ROUND_CEILING))


# ------------------------- saldo del ciclo: mes, acumulado, bonificaciones y recargas (ADR-0209, ADR-0213)

TIPOS_DE_ABONO = ("acumulado", "recarga", "bonificacion")
# Rango de cada tipo en el orden de gasto (ADR-0213 §2): lo regalado se gasta antes que lo pagado.
_RANGO_DE_GASTO = {"acumulado": 0, "bonificacion": 1, "recarga": 2}
_DECIMA = Decimal("0.1")


@dataclass(frozen=True)
class Abono:
    """Un `credit_grant` del ciclo (ADR-0209 §1, ADR-0213 §1): `acumulado` con `vence_en`, o `recarga` y
    `bonificacion` sin él."""

    tipo: str
    creditos: Decimal
    origen: str
    vence_en: datetime | None = None


@dataclass(frozen=True)
class UsoDeAbono:
    abono: Abono
    usado: Decimal

    @property
    def queda(self) -> Decimal:
        return max(self.abono.creditos - self.usado, Decimal(0))


@dataclass(frozen=True)
class RepartoDelSaldo:
    """Lo consumido en el ciclo repartido entre las fuentes del saldo, en el orden de gasto (ADR-0209 §2,
    enmendado por ADR-0213 §2): primero el mes, luego lo acumulado (el que vence antes primero), luego las
    bonificaciones (créditos de regalo) y al final las recargas. Lo que pasa del saldo es el colchón
    (`sobre_el_saldo`, ADR-0208 §5): no se le carga a ninguna fuente."""

    mes: Decimal
    usado_mes: Decimal
    abonos: tuple[UsoDeAbono, ...] = ()
    sobre_el_saldo: Decimal = Decimal(0)

    def _de(self, tipo: str) -> tuple[UsoDeAbono, ...]:
        return tuple(u for u in self.abonos if u.abono.tipo == tipo)

    @property
    def queda_mes(self) -> Decimal:
        return max(self.mes - self.usado_mes, Decimal(0))

    @property
    def acumulado(self) -> Decimal:
        return sum((u.abono.creditos for u in self._de("acumulado")), Decimal(0))

    @property
    def usado_acumulado(self) -> Decimal:
        return sum((u.usado for u in self._de("acumulado")), Decimal(0))

    @property
    def bonificaciones(self) -> Decimal:
        return sum((u.abono.creditos for u in self._de("bonificacion")), Decimal(0))

    @property
    def usado_bonificaciones(self) -> Decimal:
        return sum((u.usado for u in self._de("bonificacion")), Decimal(0))

    @property
    def recargas(self) -> Decimal:
        return sum((u.abono.creditos for u in self._de("recarga")), Decimal(0))

    @property
    def usado_recargas(self) -> Decimal:
        return sum((u.usado for u in self._de("recarga")), Decimal(0))

    @property
    def total(self) -> Decimal:
        return self.mes + self.acumulado + self.bonificaciones + self.recargas

    @property
    def acumulado_vence_en(self) -> datetime | None:
        """Vencimiento más próximo de lo acumulado que todavía queda (None si no queda nada)."""
        fechas = [u.abono.vence_en for u in self._de("acumulado") if u.queda > 0 and u.abono.vence_en]
        return min(fechas) if fechas else None


def orden_de_gasto(abonos: list[Abono] | tuple[Abono, ...]) -> list[Abono]:
    """Acumulado, luego bonificaciones y al final recargas (ADR-0213 §2); entre acumulados, el que vence
    antes; el `origen` desempata para que el reparto sea reproducible. Un tipo desconocido va al final."""
    lejano = datetime.max.replace(tzinfo=UTC)
    return sorted(
        abonos,
        key=lambda a: (_RANGO_DE_GASTO.get(a.tipo, len(_RANGO_DE_GASTO)), a.vence_en or lejano, a.origen),
    )


def repartir(usado: Decimal, mes: Decimal | int, abonos: list[Abono] | tuple[Abono, ...]) -> RepartoDelSaldo:
    """Reparte `usado` entre el crédito del mes y los abonos del ciclo (ADR-0209 §2). Función pura: el saldo
    del turno, `/admin/plan` y el cierre del ciclo usan la misma, así nunca discrepan."""
    resto = max(Decimal(usado), Decimal(0))
    mes = Decimal(mes)
    usado_mes = min(resto, mes)
    resto -= usado_mes
    usos: list[UsoDeAbono] = []
    for abono in orden_de_gasto(abonos):
        tomado = min(resto, abono.creditos)
        resto -= tomado
        usos.append(UsoDeAbono(abono, tomado))
    return RepartoDelSaldo(mes=mes, usado_mes=usado_mes, abonos=tuple(usos), sobre_el_saldo=resto)


def creditos_que_quedan(valor: Decimal) -> Decimal:
    """Lo que se arrastra al ciclo siguiente: a la décima y hacia abajo (nunca se crea crédito)."""
    return max(valor, Decimal(0)).quantize(_DECIMA, rounding=ROUND_DOWN)


async def leer_abonos(session, tenant_id: uuid.UUID, ciclo: datetime) -> list[Abono]:  # type: ignore[no-untyped-def]
    """Los `credit_grants` del negocio con `ciclo` = inicio de la ventana (RLS + `tenant_id` explícito)."""
    from core.db.models import CreditGrant

    _exigir_scope(tenant_id)
    filas = (
        await session.execute(
            select(CreditGrant.tipo, CreditGrant.creditos, CreditGrant.origen, CreditGrant.vence_en).where(
                CreditGrant.tenant_id == tenant_id, CreditGrant.ciclo == ciclo
            )
        )
    ).all()
    return [Abono(tipo=t, creditos=Decimal(c), origen=o, vence_en=v) for t, c, o, v in filas]


@dataclass(frozen=True)
class SaldoDeCreditos:
    usado: Decimal
    # Saldo del ciclo: crédito mensual del plan + abonos del ciclo (ADR-0209 §2). None = el plan no define
    # crédito (o no hay suscripción): no se limita. Decimal con abonos (llevan décimas).
    limite: int | Decimal | None
    # Ventana del ciclo medida (`ventana_mensual`); None cuando no se limita. El inicio fija la clave
    # idempotente de los avisos de consumo (HU-BILL-20) y es el `ciclo` de los abonos.
    inicio: datetime | None = None
    fin: datetime | None = None
    # Desglose por fuente (HU-BILL-22); None cuando no se limita (o en saldos armados a mano en tests).
    desglose: RepartoDelSaldo | None = None

    @property
    def disponible(self) -> Decimal | None:
        return None if self.limite is None else max(Decimal(self.limite) - self.usado, Decimal(0))

    @property
    def credito_mensual(self) -> Decimal | None:
        """El crédito del mes del plan, sin abonos: la base del colchón (ADR-0208 §5)."""
        if self.desglose is not None:
            return self.desglose.mes
        return None if self.limite is None else Decimal(self.limite)


def _exigir_scope(tenant_id: uuid.UUID) -> None:
    """Con otro `tenant_scope` la RLS devolvería vacío en silencio (0 créditos, «foto no cobrada»)."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise RuntimeError("acción de crédito fuera del tenant_scope del negocio")


async def alcanza(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    creditos: int | Decimal,
    *,
    now: datetime | None = None,
) -> SaldoDeCreditos:
    """Verifica que el saldo del ciclo (mes + acumulado + bonificaciones + recargas, ADR-0209/0213) alcance para gastar `creditos`
    más; si no, levanta
    `QuotaExceeded("creditos", usado, límite)`, que `core.api.plan_limits.plan_limit_error`
    convierte en 409 `plan_limit_creditos` con `{metric, used, limit}` (ADR-0142 §6).

    Mismo mes y mismo límite que `/admin/plan` (`plan_view.credito_del_mes`): la ventana de
    `ventana_mensual` desde el ciclo de la suscripción y `Plan.monthly_credits`. Sin suscripción
    o sin crédito en el plan no se limita (mismo criterio que `enforcement`).

    **Concurrencia.** Toma `pg_advisory_xact_lock('billing:creditos:<tenant>')` en `session`,
    que debe estar dentro del `tenant_scope(tenant_id)` y es la transacción en la que el llamador
    escribe después la fila del ledger. El lock vive hasta el commit; si la llamada al proveedor
    va fuera de esa transacción (lo normal: no se retiene un lock durante segundos), dos
    peticiones concurrentes pueden pasar la verificación con el mismo saldo. Se acepta: el ledger
    es solo de inserción (ADR-0008) y no hay reserva, así que lo máximo que se pasa es una acción
    por petición concurrente (una foto). ADR-0194.

    Con `BILLING_BLOQUEAR_POR_CREDITOS` apagado solo mide y deja pasar."""
    from core.billing.enforcement import QuotaExceeded
    from core.config import get_settings

    _exigir_scope(tenant_id)
    saldo = await _saldo_del_mes(session, tenant_id, now=now, con_lock=True)
    if saldo.limite is None or saldo.usado + Decimal(creditos) <= saldo.limite:
        return saldo
    if not get_settings().billing_bloquear_por_creditos:
        log.warning(
            "billing_creditos_insuficientes_sin_bloqueo",
            tenant_id=str(tenant_id),
            usado=float(saldo.usado),
            limite=saldo.limite,
            creditos=float(creditos),
        )
        return saldo
    raise QuotaExceeded("creditos", creditos_enteros(saldo.usado), saldo_entero(saldo.limite))


# Colchón para las conversaciones abiertas (ADR-0208 §5, D-79 con P-06 opción A): el 5 % del crédito
# mensual del plan por ciclo. Se deriva, sin columna ni contador: colchón usado = max(0, consumido − saldo).
# La plataforma lo absorbe (ADR-0209 §3). Un plan sin crédito mensual no tiene colchón.
COLCHON_FRACCION = Decimal("0.05")

SalidaDelTurno = Literal["normal", "colchon", "agotado"]


@dataclass(frozen=True)
class DecisionDelTurno:
    """Qué hace el turno con el saldo del negocio (ADR-0208 §2-6).

    - `normal`: hay saldo (o no se limita, o la bandera está apagada).
    - `colchon`: sin saldo, pero la conversación está abierta y el colchón alcanza: se atiende.
    - `agotado`: sin saldo. Con `abierta` el colchón también se agotó (pasa a una persona); sin ella
      es una conversación nueva (`quota_exhausted` una vez por episodio).

    `episodio`: inicio del episodio vigente releído en la decisión (None si no se miró o no hay)."""

    salida: SalidaDelTurno
    saldo: SaldoDeCreditos
    abierta: bool = False
    episodio: datetime | None = None
    colchon_limite: Decimal = Decimal(0)

    @property
    def colchon_usado(self) -> Decimal:
        if self.saldo.limite is None:
            return Decimal(0)
        return max(Decimal(0), self.saldo.usado - Decimal(self.saldo.limite))


def tamano_del_colchon(credito_mensual: int | Decimal | None) -> Decimal:
    """Créditos del colchón del ciclo: el 5 % del crédito MENSUAL del plan (ADR-0208 §5), sin contar lo
    acumulado, las bonificaciones ni las recargas; 0 sin crédito mensual."""
    return Decimal(0) if not credito_mensual else Decimal(credito_mensual) * COLCHON_FRACCION


async def conversacion_abierta(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> tuple[bool, datetime | None]:
    """`(abierta, inicio del episodio)` de la conversación (ADR-0208 §4).

    Abierta = episodio vigente (`conversations.episode_started_at`) con al menos una fila `turn` del
    ledger de esa conversación desde su inicio y con tokens (mismo criterio que `hubo_llamada`): un turno
    bloqueado escribe 0 tokens y no abre la conversación. El inicio se **relee** aquí, después de que
    `fijar_release_del_turno` abrió el episodio: la fila `conversation` del loop lo trae desactualizado."""
    _exigir_scope(tenant_id)
    fila = (
        await session.execute(
            text(
                "SELECT c.episode_started_at, EXISTS ("
                "  SELECT 1 FROM usage_ledger u "
                "  WHERE u.tenant_id = c.tenant_id AND u.conversation_id = c.id AND u.kind = 'turn' "
                "  AND u.created_at >= c.episode_started_at AND u.input_tokens + u.output_tokens > 0"
                ") FROM conversations c WHERE c.tenant_id = :t AND c.id = :c"
            ),
            {"t": tenant_id, "c": conversation_id},
        )
    ).first()
    if fila is None or fila[0] is None:
        return False, None
    return bool(fila[1]), fila[0]


async def decidir_turno(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> DecisionDelTurno:
    """Decisión del turno con el saldo del ciclo (`enforcement.turn_quota_metric`, ADR-0208 §2-6).

    Mismo mes, límite y consumo que `alcanza` (`_saldo_del_mes`), **sin** el lock consultivo: una carrera
    entre dos turnos se resuelve dentro del colchón. La conversación solo se consulta sin saldo. Con
    `BILLING_BLOQUEAR_POR_CREDITOS` apagado mide, registra un warning si habría cortado y devuelve
    `normal`. `session` debe estar dentro del `tenant_scope(tenant_id)`."""
    from core.config import get_settings

    _exigir_scope(tenant_id)
    saldo = await _saldo_del_mes(session, tenant_id, now=now, con_lock=False)
    # El colchón es el 5 % del crédito del mes; lo usado del colchón se mide contra el saldo total, así una
    # recarga del mismo ciclo cubre primero lo consumido en el colchón (ADR-0209 §3).
    colchon = tamano_del_colchon(saldo.credito_mensual)
    if saldo.limite is None or saldo.usado < saldo.limite:
        return DecisionDelTurno("normal", saldo, colchon_limite=colchon)
    if not get_settings().billing_bloquear_por_creditos:
        log.warning(
            "billing_credito_agotado_sin_bloqueo",
            tenant_id=str(tenant_id),
            usado=float(saldo.usado),
            limite=saldo.limite,
        )
        return DecisionDelTurno("normal", saldo, colchon_limite=colchon)
    abierta, episodio = await conversacion_abierta(session, tenant_id, conversation_id)
    decision = DecisionDelTurno("agotado", saldo, abierta=abierta, episodio=episodio, colchon_limite=colchon)
    if abierta and decision.colchon_usado < colchon:
        return DecisionDelTurno("colchon", saldo, abierta=True, episodio=episodio, colchon_limite=colchon)
    return decision


async def _saldo_del_mes(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    *,
    now: datetime | None,
    con_lock: bool,
) -> SaldoDeCreditos:
    """Consumido y saldo del ciclo, el cálculo común de `alcanza` y `decidir_turno`. Es el único lugar del
    saldo (ADR-0209 §2, HU-BILL-22).

    La ventana es la de `ventana_mensual` desde el ciclo de la suscripción. El límite es
    `Plan.monthly_credits` + Σ `credit_grants` con `ciclo` = inicio de esa ventana (lo acumulado, las
    bonificaciones y las recargas), igual que `/admin/plan` (`plan_view.credito_del_mes`). Sin contador ni columna de saldo
    (ADR-0045): lo consumido sale del ledger y se reparte con `repartir`. Sin suscripción, sin
    `period_start` o sin crédito en el plan, `limite=None` (no se limita). Con `con_lock`, toma
    `pg_advisory_xact_lock('billing:creditos:<tenant>')` antes de medir (ver `alcanza`)."""
    from core.billing.enforcement import subscription_overview
    from core.billing.periodos import ventana_mensual
    from core.billing.plans import get_plan

    overview = await subscription_overview(tenant_id)
    plan = get_plan(overview.plan_id) if overview.plan_id else None
    if overview.period_start is None or plan is None or plan.monthly_credits is None:
        return SaldoDeCreditos(usado=Decimal(0), limite=None)
    inicio, fin = ventana_mensual(overview.period_start, overview.dia_ancla, now or datetime.now(UTC))
    if con_lock:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"billing:creditos:{tenant_id}"}
        )
    usado = (await medir_creditos(session, tenant_id, desde=inicio, hasta=fin)).total
    desglose = repartir(usado, plan.monthly_credits, await leer_abonos(session, tenant_id, inicio))
    limite = plan.monthly_credits + desglose.acumulado + desglose.bonificaciones + desglose.recargas
    return SaldoDeCreditos(usado=usado, limite=limite, inicio=inicio, fin=fin, desglose=desglose)


async def imagen_ya_cobrada(session, tenant_id: uuid.UUID, image_key: str) -> bool:  # type: ignore[no-untyped-def]
    """Si ya hay una fila `vision` del negocio con `result->>'image_key' = image_key`: un
    reintento de la misma foto no se cobra dos veces. La ingesta de WhatsApp guarda `media_key`,
    no `image_key`, así que no se cruzan. Sin índice: recorre las filas `vision` del negocio
    (pocas por negocio; si crece, índice parcial sobre `(tenant_id, (result->>'image_key'))`)."""
    _exigir_scope(tenant_id)
    fila = (
        await session.execute(
            select(UsageLedger.id)
            .where(
                UsageLedger.tenant_id == tenant_id,
                UsageLedger.kind == "vision",
                UsageLedger.result["image_key"].astext == image_key,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return fila is not None


__all__ = [
    "COLCHON_FRACCION",
    "ESTADOS_PLANTILLA_ENVIADA",
    "TIPOS_DE_ABONO",
    "Abono",
    "ConsumoDeCreditos",
    "CreditoPorAgente",
    "DecisionDelTurno",
    "RepartoDelSaldo",
    "SaldoDeCreditos",
    "UsoDeAbono",
    "alcanza",
    "conversacion_abierta",
    "cotizar",
    "creditos_enteros",
    "creditos_que_quedan",
    "decidir_turno",
    "imagen_ya_cobrada",
    "leer_abonos",
    "medir_creditos",
    "orden_de_gasto",
    "repartir",
    "saldo_entero",
]
