"""Medición del crédito mensual (HU-BILL-04/06, D-35, ADR-0144).

El consumo en créditos de un negocio en una ventana se **deriva** de sus tablas fuente (ADR-0045,
sin contador paralelo):

- **IA** (`usage_ledger`): la suma de `credits`, que el loop congela al escribir cada turno con el
  tarifario y la TRM vigentes. Las filas anteriores a la migración 0096 (`credits` NULL) se
  calculan al leer con la misma fórmula y la TRM actual, como plataforma (interino: ADR-0144).
  Cuentan todas las filas salvo `kind = 'eval'` (a cargo de la plataforma, ADR-0117).
- **Notas de voz, imágenes y plantillas** (`messages`): por ahora solo el fee de plataforma del
  tarifario (0,5 · 1,0 · 0,3). STT y visión no registran su costo en USD todavía; pasarlos a
  filas del ledger es el siguiente paso de HU-BILL-04 (ADR-0144, «modo sombra»).

Se excluye el playground del panel (conversaciones del canal `test`), como en `costo_llm_usd`.
Todo corre dentro de `tenant_scope` (RLS) y además filtra `tenant_id` explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import and_, case, func, literal, or_, select

from core.billing.tarifario import TARIFARIO_VIGENTE, Tarifario, trm_vigente
from core.db.models import AgentInstanceRecord, AgentRecord, Conversation, Message, UsageLedger
from core.shared.test_mode import TEST_CHANNEL

# Estados de una plantilla que de verdad salió (Meta la aceptó). `queued` y `failed` no cobran.
ESTADOS_PLANTILLA_ENVIADA = ("sent", "delivered", "read")


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
        llm = sum((a.used for a in agentes), Decimal(0))
    else:
        llm = Decimal(
            (await session.execute(select(func.coalesce(func.sum(creditos), 0)).where(*filtros_ledger))).scalar()
            or 0
        )

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

    por_concepto = {
        "llm_turn": llm,
        "stt": tarifario.fees["stt"] * int(audios or 0),
        "vision": tarifario.fees["vision"] * int(imagenes or 0),
        "template_sent": tarifario.fees["template_sent"] * int(plantillas or 0),
    }
    total = sum(por_concepto.values(), Decimal(0))
    if por_agente:
        medios = por_concepto["stt"] + por_concepto["vision"] + por_concepto["template_sent"]
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


__all__ = [
    "ESTADOS_PLANTILLA_ENVIADA",
    "ConsumoDeCreditos",
    "CreditoPorAgente",
    "creditos_enteros",
    "medir_creditos",
]
