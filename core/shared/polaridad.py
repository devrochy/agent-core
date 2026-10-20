"""Polaridad de una afirmación en español: preguntas, negaciones y condicionales (ADR-0119).

Lo usan el verificador del core y los guardrails de los packs que buscan afirmaciones en el
texto del asistente: "No encontré una suscripción activa" o "¿Quieres que active tu
suscripción?" no afirman nada (smoke de test 2026-10-20: el guardrail de suscripciones de
pack-cobranza tomaba la negación como afirmación y el turno salía con la respuesta neutral).
Es español genérico y vive en el core, no en cada pack. Puro: sin E/S.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

_PREGUNTA = re.compile(r"¿[^¿?]*\?")
_CORTE_ORACION = re.compile(r"(?<=[.!?…])\s+|\n+")
_CORTE_CLAUSULA = re.compile("[,;:\u2014\u2013]")  # coma, punto y coma, dos puntos y rayas
# La negación se busca en la cláusula de la afirmación ("No te preocupes, tu pedido
# quedó confirmado" sí afirma); el condicional, en toda la oración ("Con tu sí, te
# envío el link de pago" no afirma).
_NEGACION = re.compile(r"\b(?:no|nunca|tampoco|ning[úu]n\w*|sin)\b", re.IGNORECASE)
_CONDICION = re.compile(
    r"\b(?:para\s+(?:que|poder|\w+(?:ar|er|ir)(?:te|le|les|lo|la|los|las|nos|me)?)|"
    r"con\s+(?:tu|tus|eso|esto|esos|estos|esa|esta|ese|este)\b|"
    r"cuando|apenas|en\s+cuanto|una\s+vez\s+(?:que|confirm\w*)|"
    r"si\s+(?:me\s+)?(?:confirm|quer|dese|prefer|est[áa]s|necesit)\w*|"
    r"despu[ée]s\s+de|antes\s+de|en\s+caso\s+de)\b",
    re.IGNORECASE,
)
# "Me faltan dos datos para cerrar el pedido": el verbo de la afirmación en infinitivo.
_PROPOSITO_AL_FINAL = re.compile(r"\b(?:para|a\s+fin\s+de)\s*$", re.IGNORECASE)
# Tope de coincidencias por oración: el costo de `afirma` crece con el prefijo y un texto
# armado a propósito no debe bloquear el event loop.
MAX_COINCIDENCIAS = 20


def oraciones_afirmativas(texto: str) -> list[str]:
    """Oraciones del texto sin las preguntas (`¿…?` o terminadas en `?`), con los
    espacios repetidos colapsados (algunos patrones de los packs son cuadráticos
    ante rachas de espacios)."""
    sin_preguntas = _PREGUNTA.sub(" ", texto or "")
    oraciones = []
    for oracion in _CORTE_ORACION.split(sin_preguntas):
        oracion = re.sub(r"[ \t]{2,}", " ", oracion).strip()
        if oracion and not oracion.endswith("?"):
            oraciones.append(oracion)
    return oraciones


def afirma(oracion: str, match: re.Match[str]) -> bool:
    """¿La coincidencia `match` dentro de `oracion` afirma (sin condición ni negación)?"""
    antes = oracion[: match.start()]
    if _CONDICION.search(antes) or _PROPOSITO_AL_FINAL.search(antes):
        return False
    # La negación cuenta en la cláusula previa y dentro de la propia frase
    # ("el pedido todavía no quedó creado").
    clausula = _CORTE_CLAUSULA.split(antes)[-1]
    return not _NEGACION.search(clausula + " " + match.group(0))


def coincidencias_afirmadas(texto: str, patron: re.Pattern[str]) -> Iterator[re.Match[str]]:
    """Las coincidencias de `patron` que el texto afirma, oración por oración."""
    for oracion in oraciones_afirmativas(texto):
        for _n, match in zip(range(MAX_COINCIDENCIAS), patron.finditer(oracion), strict=False):
            if afirma(oracion, match):
                yield match


def afirma_alguna(texto: str, patron: re.Pattern[str]) -> bool:
    """¿El texto afirma alguna coincidencia de `patron`?"""
    return next(coincidencias_afirmadas(texto, patron), None) is not None
