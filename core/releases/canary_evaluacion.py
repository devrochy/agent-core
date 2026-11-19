"""Evaluación del canary (HU-AOS-32, adenda de ADR-0117, enmienda de §5). Pura: solo decide.

Las lecturas son de `core/metricas/fiabilidad.py` (el servicio único de HU-AOS-29); aquí se comparan
los dos carriles de la conexión en la misma ventana (desde `canary_started`): el canary es la
candidata y el estable la `base_release_id`.

- **Muestra:** `muestra_conversaciones` en la candidata **y** `muestra_horas` desde el inicio.
- **Veredicto:** `sin_muestra`; `peor` si la completitud cae o el escalamiento por conversación sube
  más de `margen_pp`; `listo` si no. El costo por conversación y el p95 fuera de margen son
  advertencias, no revierten. La completitud solo cuenta con `min_tareas` por carril.
- **Señal dura:** de las últimas `dura_ventana` conversaciones de la candidata (sin las que no tienen
  resultado), la fracción que llegó mal frente a la del estable en el mismo tramo de tiempo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

UMBRALES: dict[str, Any] = {
    "muestra_conversaciones": 200,
    "muestra_horas": 72,
    "margen_pp": 0.02,
    "min_tareas": 100,
    "costo_relativo": 0.20,
    "p95_relativo": 0.30,
    "dura_ventana": 20,
    "dura_tasa": 0.25,
    "dura_factor": 2.0,
    "dias_manual": 7,
}


@dataclass(frozen=True)
class Carril:
    conversaciones: int = 0
    conversaciones_escaladas: int = 0
    tareas_terminadas: int = 0
    tareas_completadas: int = 0
    costo_usd: float = 0.0
    p95_ms: int | None = None

    @property
    def escalamiento(self) -> float | None:
        return self.conversaciones_escaladas / self.conversaciones if self.conversaciones else None

    @property
    def completitud(self) -> float | None:
        return self.tareas_completadas / self.tareas_terminadas if self.tareas_terminadas else None

    @property
    def costo_por_conversacion(self) -> float | None:
        return self.costo_usd / self.conversaciones if self.conversaciones else None


@dataclass
class Veredicto:
    estado: str  # sin_muestra | peor | listo
    motivos: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    detalle: dict[str, Any] = field(default_factory=dict)


def umbrales(guardados: Any) -> dict[str, Any]:
    """Los del evento `canary_started` (congelados al iniciar), completados con los de hoy."""
    base = dict(UMBRALES)
    if isinstance(guardados, dict):
        base.update({k: v for k, v in guardados.items() if k in UMBRALES and isinstance(v, int | float)})
    return base


def decidir(canary: Carril, estable: Carril, horas: float, u: dict[str, Any] | None = None) -> Veredicto:
    u = umbrales(u)
    detalle: dict[str, Any] = {
        "conversaciones": canary.conversaciones,
        "horas": round(horas, 1),
        "escalamiento": [estable.escalamiento, canary.escalamiento],
        "completitud": [estable.completitud, canary.completitud],
        "costo_por_conversacion": [estable.costo_por_conversacion, canary.costo_por_conversacion],
        "p95_ms": [estable.p95_ms, canary.p95_ms],
    }
    if canary.conversaciones < u["muestra_conversaciones"] or horas < u["muestra_horas"]:
        return Veredicto("sin_muestra", detalle=detalle)
    motivos: list[str] = []
    margen = float(u["margen_pp"])
    if canary.escalamiento is not None and estable.escalamiento is not None:
        if canary.escalamiento - estable.escalamiento > margen:
            motivos.append("escalamiento")
    if (
        canary.tareas_terminadas >= u["min_tareas"]
        and estable.tareas_terminadas >= u["min_tareas"]
        and canary.completitud is not None
        and estable.completitud is not None
        and estable.completitud - canary.completitud > margen
    ):
        motivos.append("completitud")
    advertencias: list[str] = []
    ce, cc = estable.costo_por_conversacion, canary.costo_por_conversacion
    if ce and cc is not None and cc > ce * (1 + float(u["costo_relativo"])):
        advertencias.append("costo")
    if estable.p95_ms and canary.p95_ms is not None and canary.p95_ms > estable.p95_ms * (1 + float(u["p95_relativo"])):
        advertencias.append("lentitud")
    return Veredicto("peor" if motivos else "listo", motivos, advertencias, detalle)


@dataclass(frozen=True)
class SenalDura:
    activa: bool
    evaluadas: int
    llegaron_mal: int
    tasa: float | None
    tasa_estable: float | None
    sin_resultado: int


def senal_dura(canary: Sequence[Any], estable: Sequence[Any], u: dict[str, Any] | None = None) -> SenalDura:
    """`canary`: las conversaciones recientes de la candidata (`ConversacionReciente`, de la más
    nueva a la más vieja). `estable`: las del estable desde el último turno más viejo de esas
    `dura_ventana`. Sin un estable comparable no cuentan los `action_failed` (una caída de la
    pasarela no retira la versión)."""
    u = umbrales(u)
    ventana = int(u["dura_ventana"])
    con_resultado = [c for c in canary if not c.sin_resultado]
    sin_resultado = len(canary) - len(con_resultado)
    if len(con_resultado) < ventana:
        return SenalDura(False, len(con_resultado), 0, None, None, sin_resultado)
    ultimas = con_resultado[:ventana]
    estable_ok = [c for c in estable if not c.sin_resultado]
    comparable = len(estable_ok) >= ventana

    def mal(c: Any) -> bool:
        return bool(c.llego_mal or (comparable and c.accion_fallida))

    llegaron_mal = sum(1 for c in ultimas if mal(c))
    tasa = llegaron_mal / ventana
    tasa_estable = sum(1 for c in estable_ok if mal(c)) / len(estable_ok) if comparable else None
    activa = tasa >= float(u["dura_tasa"]) and (
        tasa_estable is None or tasa >= float(u["dura_factor"]) * tasa_estable
    )
    return SenalDura(activa, ventana, llegaron_mal, round(tasa, 3), None if tasa_estable is None else round(tasa_estable, 3), sin_resultado)


__all__ = ["UMBRALES", "Carril", "SenalDura", "Veredicto", "decidir", "senal_dura", "umbrales"]
