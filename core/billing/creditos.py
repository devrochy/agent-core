"""Medición del crédito mensual (HU-BILL-04/06, D-35, ADR-0144).

El consumo en créditos de un negocio en una ventana se **deriva** de sus tablas fuente (ADR-0045,
sin contador paralelo):

- **IA** (`usage_ledger`): la suma de `credits`, que el loop congela al escribir cada turno con el
  tarifario y la TRM vigentes. Las filas anteriores a la migración 0096 (`credits` NULL) se
  calculan al leer con la misma fórmula y la TRM actual, como plataforma (interino: ADR-0144).
  Cuentan todas las filas salvo `kind = 'eval'` (a cargo de la plataforma, ADR-0117).
- **Notas de voz, imágenes y plantillas** (`messages`): por ahora solo el fee de plataforma del
  tarifario (0,5 · 1,0 · 0,3). La ingesta de WhatsApp registra STT y visión en el ledger en sombra
  (`credits = 0`, ADR-0144), así que no cobra dos veces.
- **Desglose por `kind`** (HU-BILL-16): las filas `vision` y `stt` del ledger suman a su concepto
  y el resto (`turn`, `agent_run`, `embedding`, `generacion`) a `llm_turn`. Las fotos de catálogo
  (`kind = 'vision'` con créditos reales, ADR-0194) aparecen así como «Imágenes» y no como «IA».

Acciones de IA cotizadas (HU-BILL-16, ADR-0194): `cotizar` dice cuánto cuesta antes de gastar,
`alcanza` verifica el saldo del mes y `imagen_ya_cobrada` evita cobrar dos veces la misma foto.

Se excluye el playground del panel (conversaciones del canal `test`), como en `costo_llm_usd`.
Todo corre dentro de `tenant_scope` (RLS) y además filtra `tenant_id` explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from sqlalchemy import and_, case, func, literal, or_, select, text

from core.billing.tarifario import (
    TARIFARIO_VIGENTE,
    Concepto,
    KeyMode,
    Tarifario,
    creditos_por_evento,
    trm_vigente,
)
from core.db.models import AgentInstanceRecord, AgentRecord, Conversation, Message, UsageLedger
from core.shared.logger import get_logger
from core.shared.test_mode import TEST_CHANNEL

log = get_logger(__name__)

# Estados de una plantilla que de verdad salió (Meta la aceptó). `queued` y `failed` no cobran.
ESTADOS_PLANTILLA_ENVIADA = ("sent", "delivered", "read")
# Filas del ledger que tienen concepto propio en el desglose; el resto es «IA» (`llm_turn`).
_KINDS_CON_CONCEPTO = ("vision", "stt")


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


def _sin_playground(tenant_id: uuid.UUID, columna):  # type: ignore[no-untyped-def]
    """Excluye las filas del playground; las que no tienen conversación se conservan
    (`NOT IN` con NULL descartaría filas legítimas)."""
    de_prueba = select(Conversation.id).where(
        Conversation.tenant_id == tenant_id, Conversation.channel == TEST_CHANNEL
    )
    return or_(columna.is_(None), ~columna.in_(de_prueba))


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
    se rechaza."""
    from core.shared.tenant import get_current_tenant_id

    if get_current_tenant_id() != tenant_id:
        raise RuntimeError("medir_creditos fuera del tenant_scope del negocio medido")
    tarifario = TARIFARIO_VIGENTE
    trm = trm_vigente()
    creditos = _creditos_de_la_fila(trm, tarifario)

    filtros_ledger = [
        UsageLedger.tenant_id == tenant_id,
        UsageLedger.kind != "eval",
        _sin_playground(tenant_id, UsageLedger.conversation_id),
    ]
    if desde is not None:
        filtros_ledger.append(UsageLedger.created_at >= desde)
    if hasta is not None:
        filtros_ledger.append(UsageLedger.created_at < hasta)

    concepto_de_la_fila = case(
        (UsageLedger.kind.in_(_KINDS_CON_CONCEPTO), UsageLedger.kind), else_=literal("llm_turn")
    )
    del_ledger: dict[str, Decimal] = {"llm_turn": Decimal(0), "stt": Decimal(0), "vision": Decimal(0)}
    for concepto, usado in (
        await session.execute(
            select(concepto_de_la_fila, func.coalesce(func.sum(creditos), 0))
            .where(*filtros_ledger)
            .group_by(concepto_de_la_fila)
        )
    ).all():
        del_ledger[concepto] = Decimal(usado)

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
                .where(*filtros_ledger)
                .group_by(AgentRecord.id, AgentRecord.name)
            )
        ).all()
        agentes = [CreditoPorAgente(agent_id=a, name=n, used=Decimal(u)) for a, n, u in filas]

    # Fees de medios y plantillas. Sin filtrar `deleted_at`: borrar un mensaje (retención) no
    # devuelve un consumo que ya ocurrió.
    filtros_msg = [Message.tenant_id == tenant_id, _sin_playground(tenant_id, Message.conversation_id)]
    if desde is not None:
        filtros_msg.append(Message.created_at >= desde)
    if hasta is not None:
        filtros_msg.append(Message.created_at < hasta)
    entrante = Message.direction == "inbound"
    audios, imagenes, plantillas = (
        await session.execute(
            select(
                func.count(Message.id).filter(entrante, Message.media_type.like("audio/%")),
                func.count(Message.id).filter(entrante, Message.media_type.like("image/%")),
                func.count(Message.id).filter(
                    Message.direction == "outbound",
                    Message.template_name.is_not(None),
                    Message.status.in_(ESTADOS_PLANTILLA_ENVIADA),
                ),
            ).where(*filtros_msg)
        )
    ).one()

    fees_stt = tarifario.fees["stt"] * int(audios or 0)
    fees_vision = tarifario.fees["vision"] * int(imagenes or 0)
    fees_plantillas = tarifario.fees["template_sent"] * int(plantillas or 0)
    por_concepto = {
        "llm_turn": del_ledger["llm_turn"],
        "stt": del_ledger["stt"] + fees_stt,
        "vision": del_ledger["vision"] + fees_vision,
        "template_sent": fees_plantillas,
    }
    total = sum(por_concepto.values(), Decimal(0))
    if por_agente:
        # Lo del ledger ya está en `agentes`: solo se agregan los fees que salen de `messages`.
        medios = fees_stt + fees_vision + fees_plantillas
        if medios:
            sin_agente = next((a for a in agentes if a.agent_id is None), None)
            agentes = [a for a in agentes if a.agent_id is not None]
            agentes.append(
                CreditoPorAgente(None, None, (sin_agente.used if sin_agente else Decimal(0)) + medios)
            )
        agentes.sort(key=lambda a: (a.agent_id is None, -a.used))
    return ConsumoDeCreditos(total=total, por_concepto=por_concepto, por_agente=agentes)


def creditos_enteros(valor: Decimal) -> int:
    """Créditos para mostrar: redondeo al entero más cercano (la medición guarda décimas)."""
    return int(valor.quantize(Decimal(1), rounding=ROUND_HALF_UP))


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


@dataclass(frozen=True)
class SaldoDeCreditos:
    usado: Decimal
    limite: int | None  # None = el plan no define crédito (o no hay suscripción): no se limita

    @property
    def disponible(self) -> Decimal | None:
        return None if self.limite is None else max(Decimal(self.limite) - self.usado, Decimal(0))


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
    """Verifica que el crédito del mes alcance para gastar `creditos` más; si no, levanta
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
    from core.billing.enforcement import QuotaExceeded, subscription_overview
    from core.billing.periodos import ventana_mensual
    from core.billing.plans import get_plan
    from core.config import get_settings

    _exigir_scope(tenant_id)
    overview = await subscription_overview(tenant_id)
    plan = get_plan(overview.plan_id) if overview.plan_id else None
    if overview.period_start is None or plan is None or plan.monthly_credits is None:
        return SaldoDeCreditos(usado=Decimal(0), limite=None)
    inicio, fin = ventana_mensual(overview.period_start, overview.dia_ancla, now or datetime.now(UTC))

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"billing:creditos:{tenant_id}"}
    )
    usado = (await medir_creditos(session, tenant_id, desde=inicio, hasta=fin)).total
    saldo = SaldoDeCreditos(usado=usado, limite=plan.monthly_credits)
    if usado + Decimal(creditos) <= plan.monthly_credits:
        return saldo
    if not get_settings().billing_bloquear_por_creditos:
        log.warning(
            "billing_creditos_insuficientes_sin_bloqueo",
            tenant_id=str(tenant_id),
            usado=float(usado),
            limite=plan.monthly_credits,
            creditos=float(creditos),
        )
        return saldo
    raise QuotaExceeded("creditos", creditos_enteros(usado), plan.monthly_credits)


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
    "ESTADOS_PLANTILLA_ENVIADA",
    "ConsumoDeCreditos",
    "CreditoPorAgente",
    "SaldoDeCreditos",
    "alcanza",
    "cotizar",
    "creditos_enteros",
    "imagen_ya_cobrada",
    "medir_creditos",
]
