"""Troceado v1 (ADR-0145 §2): primero por estructura, después por tamaño.

- Un fragmento no mezcla secciones: al cambiar de sección se cierra.
- Objetivo de 400 tokens, máximo 600 y solape de 60 con el fragmento anterior de la misma sección,
  sin partir frases (salvo una frase que sola pase del máximo, que se corta por palabras).
- Los tokens se estiman en 4 caracteres por token, sin tokenizador: el troceado es determinista y
  no depende del modelo.

Subir cualquiera de estos valores es otro `CHUNKER_VERSION` (`models.py`) y obliga a reprocesar.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from pack_knowledge.extraccion import Bloque

CARACTERES_POR_TOKEN = 4
OBJETIVO_TOKENS = 400
MAX_TOKENS = 600
SOLAPE_TOKENS = 60
MAX_FRAGMENTOS = 2_000

_OBJETIVO = OBJETIVO_TOKENS * CARACTERES_POR_TOKEN
_MAXIMO = MAX_TOKENS * CARACTERES_POR_TOKEN
_SOLAPE = SOLAPE_TOKENS * CARACTERES_POR_TOKEN
_FIN_DE_FRASE = re.compile(r"(?<=[.!?…;:])\s+")


@dataclass(frozen=True)
class Fragmento:
    ordinal: int
    contenido: str
    seccion: str | None
    pagina_desde: int | None
    pagina_hasta: int | None
    tokens: int


class DemasiadosFragmentos(Exception):
    pass


def tokens_estimados(texto: str) -> int:
    return max(1, math.ceil(len(texto) / CARACTERES_POR_TOKEN))


def _frases(texto: str) -> list[str]:
    """Frases de un párrafo; una frase más larga que el máximo se corta por palabras."""
    salida: list[str] = []
    for frase in _FIN_DE_FRASE.split(texto):
        frase = frase.strip()
        if not frase:
            continue
        while len(frase) > _MAXIMO:
            corte = frase.rfind(" ", 0, _MAXIMO)
            corte = corte if corte > 0 else _MAXIMO
            salida.append(frase[:corte].strip())
            frase = frase[corte:].strip()
        if frase:
            salida.append(frase)
    return salida


def _cola(texto: str) -> str:
    """Las últimas frases enteras que caben en el solape."""
    cola: list[str] = []
    largo = 0
    for frase in reversed(_frases(texto)):
        if largo + len(frase) > _SOLAPE:
            break
        cola.insert(0, frase)
        largo += len(frase) + 1
    return " ".join(cola)


def trocear(bloques: list[Bloque]) -> list[Fragmento]:
    fragmentos: list[Fragmento] = []
    partes: list[str] = []
    paginas: list[int] = []
    seccion: str | None = None
    hay_contenido_nuevo = False

    def _cerrar(con_solape: bool) -> None:
        nonlocal partes, paginas, hay_contenido_nuevo
        if not hay_contenido_nuevo:
            partes, paginas = [], []
            return
        contenido = " ".join(partes).strip()
        if len(fragmentos) >= MAX_FRAGMENTOS:
            raise DemasiadosFragmentos
        fragmentos.append(
            Fragmento(
                ordinal=len(fragmentos),
                contenido=contenido,
                seccion=seccion,
                pagina_desde=min(paginas) if paginas else None,
                pagina_hasta=max(paginas) if paginas else None,
                tokens=tokens_estimados(contenido),
            )
        )
        cola = _cola(contenido) if con_solape else ""
        partes = [cola] if cola else []
        paginas = [paginas[-1]] if cola and paginas else []
        hay_contenido_nuevo = False

    for bloque in bloques:
        if bloque.seccion != seccion:
            _cerrar(con_solape=False)
            seccion = bloque.seccion
        unidades = [bloque.texto] if len(bloque.texto) <= _MAXIMO else _frases(bloque.texto)
        for unidad in unidades:
            actual = sum(len(p) + 1 for p in partes)
            if hay_contenido_nuevo and actual + len(unidad) > _OBJETIVO:
                _cerrar(con_solape=True)
                actual = sum(len(p) + 1 for p in partes)
                if actual + len(unidad) > _MAXIMO:
                    # El solape no deja lugar: la unidad empieza un fragmento limpio.
                    partes, paginas = [], []
            partes.append(unidad)
            if bloque.pagina is not None:
                paginas.append(bloque.pagina)
            hay_contenido_nuevo = True
    _cerrar(con_solape=False)
    return fragmentos
