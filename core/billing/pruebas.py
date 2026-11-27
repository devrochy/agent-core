"""Estimado de una corrida de pruebas del agente (SPEC-53, ADR-0212 §4, HU-AOS-102).

`estimar_corrida` dice, ANTES de gastar, cuántos créditos cuesta correr N escenarios con cada `k` permitido
(intentos por escenario), cuánto sale de la cuota gratis compartida con el playground y cuánto del saldo. No
escribe nada ni toma candados: es lo que muestra el diálogo «Probar». `cotizar` no sirve aquí (solo cubre
`vision`), por eso es una función aparte.

- **Por escenario** = turnos esperados × (turno del agente + turno del cliente simulado) + el juez. El agente
  cuesta según el nivel de IA del agente (con llave propia, BYOK, solo el fee: la llave la paga el negocio); el
  cliente simulado y el juez son de la plataforma y se cobran siempre con la llave de la plataforma. Es un
  punto de partida con la calibración de HU-BILL-29 (11,1 créditos por conversación en Estándar y 45,1 en
  Avanzado); se recalibra con la medición real de HU-AOS-105. Es un tope por arriba: un escenario que falla
  por el chequeo determinista no llama al juez.
- **Por k**: el estimado de `k` es el de `k = 1` por `k` (cada intento repite turnos, cliente y juez), así que
  k = 2 cuesta exactamente el doble que k = 1.
- **Cuota gratis**: la misma que el playground, derivada del ledger y sin contador:
  `de_la_cuota = min(estimado, max(0, 300 − usado_en_el_ciclo))`, con `usado` = playground vivo + pruebas del
  ciclo (`medir_creditos().playground_usado`). El resto sale del saldo.
- El costo real en USD de cada `k` lo ve solo el master (`costo_usd`); la capa de API lo quita para los demás.

Corre dentro del `tenant_scope` del negocio (RLS) y filtra `tenant_id` explícito.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

from sqlalchemy import select

from core.billing.creditos import PLAYGROUND_GRATIS_POR_CICLO, _saldo_del_mes, medir_creditos
from core.billing.tarifario import KeyMode, creditos_por_evento, trm_vigente
from core.db.models import AgentRecord

# Turnos del agente por escenario: lo habitual es 6 (techo de 12 que aplica el corredor, HU-AOS-103).
TURNOS_ESPERADOS = 6
# Cuántas veces el estimado puede crecer antes de cortar la corrida (ADR-0212 §4, `tope_creditos`).
FACTOR_TOPE = Decimal("1.5")
MAX_ESCENARIOS = 20

# Costo de referencia en USD de UNA unidad, con la llave de la plataforma (HU-BILL-29, ver docstring). El nivel
# de IA del agente fija el costo de su turno; el cliente simulado y el juez no dependen de él.
_USD_TURNO_AGENTE = {"estandar": Decimal("0.00124"), "avanzado": Decimal("0.0057")}
_USD_TURNO_CLIENTE = Decimal("0.0003")
_USD_JUEZ = Decimal("0.0015")


class AgenteNoEncontrado(LookupError):
    pass


@dataclass(frozen=True)
class EstimadoDeK:
    k: int
    estimado: int
    de_la_cuota: int
    del_saldo: int
    saldo_despues: int | None  # None = el negocio no tiene tope de crédito
    alcanza: bool
    costo_usd: Decimal  # solo lo ve el master


@dataclass(frozen=True)
class EstimadoDeCorrida:
    escenarios: int
    nivel_ia: str
    key_mode: KeyMode
    k_por_defecto: int
    k_maximo: int
    saldo: int | None
    cuota_gratis_restante: int
    por_k: list[EstimadoDeK] = field(default_factory=list)

    def de(self, k: int) -> EstimadoDeK | None:
        return next((e for e in self.por_k if e.k == k), None)


def _creditos_de_un_escenario(nivel: str, key_mode: KeyMode) -> Decimal:
    trm = trm_vigente()
    agente = creditos_por_evento(_USD_TURNO_AGENTE.get(nivel, _USD_TURNO_AGENTE["estandar"]), "llm_turn", key_mode, trm)
    # Cliente simulado y juez: siempre la llave de la plataforma (ADR-0212 §4, BYOK).
    cliente = creditos_por_evento(_USD_TURNO_CLIENTE, "agent_run", "platform", trm)
    juez = creditos_por_evento(_USD_JUEZ, "agent_run", "platform", trm)
    return TURNOS_ESPERADOS * (agente + cliente) + juez


def _usd_de_un_escenario(nivel: str, key_mode: KeyMode) -> Decimal:
    agente = Decimal(0) if key_mode == "byok" else _USD_TURNO_AGENTE.get(nivel, _USD_TURNO_AGENTE["estandar"])
    return TURNOS_ESPERADOS * (agente + _USD_TURNO_CLIENTE) + _USD_JUEZ


def estimado_de_k1(escenarios: int, nivel: str, key_mode: KeyMode) -> int:
    """Créditos enteros de `escenarios` escenarios con k = 1, redondeados hacia arriba: lo estimado nunca queda
    por debajo de lo cotizado."""
    return int((_creditos_de_un_escenario(nivel, key_mode) * escenarios).to_integral_value(rounding=ROUND_CEILING))


def tope_de_creditos(estimado: int) -> Decimal:
    """Tope de la corrida: el estimado de su `k` por 1,5 (a 0,1)."""
    return (Decimal(estimado) * FACTOR_TOPE).quantize(Decimal("0.1"), rounding=ROUND_CEILING)


def _inicio_del_ciclo(inicio: datetime | None, ahora: datetime) -> datetime:
    """Sin suscripción no hay ciclo: la cuota se mide desde el primer día del mes (UTC)."""
    return inicio or ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def estimar_corrida(
    session,  # type: ignore[no-untyped-def]
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    escenarios: int,
    *,
    now: datetime | None = None,
) -> EstimadoDeCorrida:
    """Tabla `por_k` de 1 al tope del negocio para correr `escenarios` escenarios del agente. La sesión está dentro
    del `tenant_scope(tenant_id)`. `AgenteNoEncontrado` si el agente no es del negocio o está borrado."""
    from core.ia.estructurado import _app_settings
    from core.llm.niveles import normalizar, usa_llave_propia
    from core.pruebas.config import topes_de_k

    if escenarios < 1:
        raise ValueError("escenarios debe ser al menos 1")
    ahora = now or datetime.now(UTC)
    nivel_crudo = (
        await session.execute(
            select(AgentRecord.nivel_ia).where(AgentRecord.tenant_id == tenant_id, AgentRecord.id == agent_id)
        )
    ).scalar_one_or_none()
    if nivel_crudo is None:
        raise AgenteNoEncontrado(str(agent_id))
    nivel = normalizar(nivel_crudo)
    key_mode: KeyMode = "byok" if usa_llave_propia(await _app_settings(tenant_id)) else "platform"
    k_defecto, k_maximo = await topes_de_k(session, tenant_id)

    saldo = await _saldo_del_mes(session, tenant_id, now=ahora, con_lock=False)
    consumo = await medir_creditos(
        session, tenant_id, desde=_inicio_del_ciclo(saldo.inicio, ahora), hasta=saldo.fin
    )
    cuota_restante = max(Decimal(0), PLAYGROUND_GRATIS_POR_CICLO - consumo.playground_usado)
    cuota_entera = int(cuota_restante.to_integral_value(rounding=ROUND_DOWN))
    restante = (
        None
        if saldo.limite is None
        else int(max(Decimal(0), Decimal(saldo.limite) - saldo.usado).to_integral_value(rounding=ROUND_DOWN))
    )

    base = estimado_de_k1(escenarios, nivel, key_mode)
    usd_base = _usd_de_un_escenario(nivel, key_mode) * escenarios
    por_k: list[EstimadoDeK] = []
    for k in range(1, k_maximo + 1):
        estimado = base * k
        de_la_cuota = min(estimado, cuota_entera)
        del_saldo = estimado - de_la_cuota
        por_k.append(
            EstimadoDeK(
                k=k,
                estimado=estimado,
                de_la_cuota=de_la_cuota,
                del_saldo=del_saldo,
                saldo_despues=None if restante is None else restante - del_saldo,
                alcanza=restante is None or del_saldo <= restante,
                costo_usd=(usd_base * k).quantize(Decimal("0.0001"), rounding=ROUND_CEILING),
            )
        )
    return EstimadoDeCorrida(
        escenarios=escenarios,
        nivel_ia=nivel,
        key_mode=key_mode,
        k_por_defecto=k_defecto,
        k_maximo=k_maximo,
        saldo=restante,
        cuota_gratis_restante=cuota_entera,
        por_k=por_k,
    )


__all__ = [
    "FACTOR_TOPE",
    "MAX_ESCENARIOS",
    "TURNOS_ESPERADOS",
    "AgenteNoEncontrado",
    "EstimadoDeCorrida",
    "EstimadoDeK",
    "estimado_de_k1",
    "estimar_corrida",
    "tope_de_creditos",
]
