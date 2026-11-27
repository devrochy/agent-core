"""Juez de un escenario de prueba del agente (HU-AOS-103, ADR-0212 §3).

Dos pasos, en este orden y el primero sin LLM:

1. **Chequeo determinista** (`chequeo_de_tools`): con las tools que el agente llamó de verdad, una `tools_prohibidas`
   llamada o una `tools_esperadas` nunca llamada es una falla, y no se llama al juez (el escenario ahorra su costo).
2. **Juez LLM** (`juzgar`): `generar_estructurado` con el playbook del core `juez_de_pruebas` y un **modelo fijo de la
   plataforma** (`PRUEBAS_MODELO`), siempre con la llave de la plataforma y `kind='prueba_agente'`. La transcripción
   entra en un JSON (dato, invariante 7), no concatenada a las instrucciones. Una salida que no valida el esquema o un
   proveedor caído NO es una falla del agente: el escenario queda «no se pudo probar» (`ErrorIA` sube a quien llama,
   junto con lo que ya se cobró).

El resultado nunca incluye el modelo (ADR-0212 §7).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "juez_de_pruebas"
KIND = "prueba_agente"
MAX_TOKENS = 600
TEMPERATURA = 0.0
TIMEOUT_S = 60.0
MAX_TEXTO_TURNO = 1500  # un turno más largo se recorta: el juez lee, no necesita el volcado completo
MAX_RAZON = 600


class VeredictoDelJuez(BaseModel):
    """Lo que el LLM devuelve. Todo es dato no confiable: se recorta y se valida contra los criterios."""

    pasa: bool
    criterio_incumplido: str | None = Field(default=None, max_length=1000)
    razon: str = Field(default="", max_length=4000)


@dataclass(frozen=True)
class Veredicto:
    pasa: bool
    criterio_incumplido: str | None
    razon: str
    creditos: Decimal = Decimal(0)  # lo que costó juzgar (0 si fue determinista)
    por_llm: bool = False


def chequeo_de_tools(
    tools_llamadas: Sequence[str], esperadas: Sequence[str], prohibidas: Sequence[str]
) -> Veredicto | None:
    """`Veredicto` de falla si el agente rompió las reglas de tools; `None` si pasa este chequeo (y sigue el juez)."""
    llamadas = set(tools_llamadas)
    for tool in prohibidas:
        if tool in llamadas:
            return Veredicto(False, f"No debía usar la tool {tool}", f"El agente llamó a la tool prohibida «{tool}».")
    for tool in esperadas:
        if tool not in llamadas:
            return Veredicto(False, f"Debía usar la tool {tool}", f"El agente nunca llamó a la tool esperada «{tool}».")
    return None


def _entrada(transcripcion: Sequence[dict[str, str]], tools_llamadas: Sequence[str], criterios: Sequence[str]) -> str:
    turnos = [
        {"n": i, "quien": "cliente" if t.get("quien") == "cliente" else "agente", "texto": str(t.get("texto", ""))[:MAX_TEXTO_TURNO]}
        for i, t in enumerate(transcripcion, start=1)
    ]
    return json.dumps(
        {
            "criterios": [f"{i}. {c}" for i, c in enumerate(criterios, start=1)],
            "tools_llamadas": sorted(set(tools_llamadas)),
            "transcripcion": turnos,
        },
        ensure_ascii=False,
    )


def _proveedor() -> Any:
    """Proveedor de plataforma con el modelo fijo de las pruebas (no el del agente ni la llave del negocio)."""
    from core.config import get_settings
    from core.llm import resolve_provider

    return resolve_provider(model=get_settings().pruebas_modelo or None, thinking=False)


def _criterio_valido(texto: str | None, criterios: Sequence[str]) -> str | None:
    """El criterio incumplido solo cuenta si es uno de los del escenario; si el LLM lo parafrasea se guarda tal cual,
    recortado, pero nunca vacío si falló."""
    if texto is None:
        return criterios[0] if criterios else None
    limpio = texto.strip()[:1000]
    return limpio or (criterios[0] if criterios else None)


async def juzgar(
    tenant_id: uuid.UUID,
    *,
    run_id: uuid.UUID,
    case_id: uuid.UUID,
    transcripcion: Sequence[dict[str, str]],
    tools_llamadas: Sequence[str],
    criterios: Sequence[str],
) -> Veredicto:
    """Veredicto del juez LLM. `ErrorProveedor` / `ErrorFormato` (de `core.ia.estructurado`) suben tal cual: quien
    llama decide que el escenario queda «no se pudo probar» y suma `ErrorFormato.consumo` a lo gastado."""
    from core.ia.estructurado import generar_estructurado

    resultado = await generar_estructurado(
        tenant_id,
        playbook=PLAYBOOK,
        esquema=VeredictoDelJuez,
        entrada=_entrada(transcripcion, tools_llamadas, criterios),
        kind=KIND,
        result={"concepto": "juez", "run_id": str(run_id), "case_id": str(case_id)},
        proveedor=_proveedor,
        key_mode="platform",
        max_tokens=MAX_TOKENS,
        temperatura=TEMPERATURA,
        timeout_s=TIMEOUT_S,
    )
    v = resultado.datos
    pasa = bool(v.pasa)
    razon = (v.razon or "").strip()[:MAX_RAZON]
    return Veredicto(
        pasa,
        None if pasa else _criterio_valido(v.criterio_incumplido, criterios),
        razon,
        creditos=resultado.consumo.credits or Decimal(0),
        por_llm=True,
    )


__all__ = ["Veredicto", "VeredictoDelJuez", "chequeo_de_tools", "juzgar"]
