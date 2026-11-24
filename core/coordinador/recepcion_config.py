"""Lo que el dueño configura de la recepción del Coordinador y cómo viaja a la release (ADR-0211).

La fuente editable vive en `AppSettings.behavior_config` (sin migración): `recepcion_guardrails` y `recepcion_nivel_ia`.
Al construir la release se congela en la clave de primer nivel `recepcion` del manifiesto; el turno lee la release, nunca
la fila en caliente. Con la configuración por defecto la clave se omite y el hash de la release no cambia.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from agentcore_contracts import GuardrailRule
from pydantic import ValidationError

from core.agents.recepcion import TOOLS_RECEPCION
from core.llm import niveles

CLAVE_REGLAS = "recepcion_guardrails"
CLAVE_NIVEL = "recepcion_nivel_ia"
MAX_REGLAS = 20
MAX_FRASES_POR_REGLA = 10
# Una frase de una letra o un largo máximo de 1 harían fallar (y escalar) casi todas las respuestas.
MIN_LARGO_FRASE = 3
MIN_LARGO_MAXIMO = 50
MAX_BYTES = 20_000


class ReglasInvalidas(ValueError):
    """Las reglas no pasan el contrato; `problemas` es `[(ruta, mensaje)]` por campo."""

    def __init__(self, problemas: list[tuple[str, str]]) -> None:
        super().__init__("; ".join(f"{r}: {m}" for r, m in problemas))
        self.problemas = problemas


def validar_reglas(reglas: Sequence[Any]) -> list[dict[str, Any]]:
    """Las reglas validadas y normalizadas (`GuardrailRule`), o `ReglasInvalidas` con un problema por campo.

    `required_tool` solo acepta las tools de la vista de recepción: una regla que exige una tool que no tiene
    escalaría en cada turno."""
    problemas: list[tuple[str, str]] = []
    salida: list[dict[str, Any]] = []
    if len(reglas) > MAX_REGLAS:
        raise ReglasInvalidas([("guardrails", f"Máximo {MAX_REGLAS} reglas.")])
    nombres: set[str] = set()
    for i, cruda in enumerate(reglas):
        try:
            regla = GuardrailRule.model_validate(cruda)
        except ValidationError as exc:
            for e in exc.errors():
                ruta = ".".join(str(p) for p in e["loc"])
                problemas.append((f"guardrails[{i}]" + (f".{ruta}" if ruta else ""), str(e["msg"])))
            continue
        if regla.kind in ("forbidden_phrase", "required_tool"):
            if len(regla.phrases) > MAX_FRASES_POR_REGLA:
                problemas.append((f"guardrails[{i}].phrases", f"Máximo {MAX_FRASES_POR_REGLA} frases por regla."))
            if any(len(f) < MIN_LARGO_FRASE for f in regla.phrases):
                problemas.append(
                    (f"guardrails[{i}].phrases", f"Cada frase necesita al menos {MIN_LARGO_FRASE} caracteres.")
                )
        if regla.kind == "max_length" and (regla.value or 0) < MIN_LARGO_MAXIMO:
            problemas.append((f"guardrails[{i}].value", f"El largo máximo no puede ser menor de {MIN_LARGO_MAXIMO}."))
        if regla.name in nombres:
            problemas.append((f"guardrails[{i}].name", "Ya hay una regla con ese nombre."))
        nombres.add(regla.name)
        if regla.kind == "required_tool" and regla.tool not in TOOLS_RECEPCION:
            problemas.append((f"guardrails[{i}].tool", "La recepción no tiene esa herramienta."))
        salida.append(regla.model_dump(mode="json", exclude_none=True))
    if not problemas and len(json.dumps(salida, ensure_ascii=False).encode()) > MAX_BYTES:
        problemas.append(("guardrails", "Las reglas son demasiado largas en total."))
    if problemas:
        raise ReglasInvalidas(problemas)
    return salida


def reglas_guardadas(behavior_config: Mapping[str, Any] | None) -> list[Any]:
    crudo = (behavior_config or {}).get(CLAVE_REGLAS)
    return list(crudo) if isinstance(crudo, list) else []


def nivel_guardado(behavior_config: Mapping[str, Any] | None) -> niveles.Nivel:
    return niveles.normalizar((behavior_config or {}).get(CLAVE_NIVEL))


def para_release(behavior_config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Lo que congela la release: `{}` con la configuración por defecto (sin reglas y nivel Estándar). Lanza
    `ReglasInvalidas` si lo guardado ya no pasa el contrato (es dato: el build lo rechaza, el turno nunca lanza)."""
    salida: dict[str, Any] = {}
    reglas = validar_reglas(reglas_guardadas(behavior_config))
    if reglas:
        salida["guardrails"] = reglas
    nivel = nivel_guardado(behavior_config)
    if nivel != niveles.ESTANDAR:
        salida["nivel_ia"] = nivel
    return salida
