"""Respuestas del guion de una plantilla de agente (HU-AOS-48, ADR-0171). Lógica pura.

El guion (`PlantillaAgente.preguntas`) lo sirve el servidor y lo pintan el panel y la APK. Aquí se
validan las respuestas del cliente y se traducen a lo único que pueden cambiar: los superpoderes que
conserva el agente, el largo máximo de su respuesta y su nombre. Nada llega a un prompt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.presets import PlantillaAgente

NOMBRE_MAX = 120


@dataclass(frozen=True)
class Ajustes:
    """Lo que el guion decide. `procedimientos` conserva el orden de la plantilla; `None` en el
    resto = no se pregunta o no se contestó algo que lo cambie."""

    procedimientos: list[str]
    largo_maximo: int | None = None
    nombre: str | None = None


class RespuestasInvalidas(ValueError):
    """Respuestas fuera del guion: un mensaje por pregunta (id de la pregunta → texto para el dueño)."""

    def __init__(self, por_pregunta: dict[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in por_pregunta.items()))
        self.por_pregunta = por_pregunta


def resolver(plantilla: PlantillaAgente, respuestas: Mapping[str, Any]) -> Ajustes:
    """Valida las `respuestas` (`id de la pregunta` → id de la opción, o texto) contra el guion de la
    plantilla y devuelve sus ajustes. Una pregunta sin respuesta usa su `defecto`; una de texto sin
    respuesta no cambia nada. Lanza `RespuestasInvalidas` sin aplicar nada si alguna no vale."""
    conocidas = {q.id for q in plantilla.preguntas}
    errores = {
        str(clave): "Esta pregunta no existe para este tipo de agente."
        for clave in respuestas
        if clave not in conocidas
    }
    elegidos: set[str] = set()
    declara = False
    largo: int | None = None
    nombre: str | None = None
    for q in plantilla.preguntas:
        valor = respuestas.get(q.id)
        if q.tipo == "texto":
            if valor is None:
                continue
            if not isinstance(valor, str) or not valor.strip():
                errores[q.id] = "Escribe una respuesta."
            elif len(valor.strip()) > NOMBRE_MAX:
                errores[q.id] = f"Máximo {NOMBRE_MAX} caracteres."
            else:
                nombre = valor.strip()
            continue
        opcion_id = q.defecto if valor is None else valor
        opcion = next((o for o in q.opciones if o.id == opcion_id), None)
        if opcion is None:
            errores[q.id] = "Elige una de las opciones."
            continue
        if opcion.procedimientos:
            declara = True
            elegidos.update(opcion.procedimientos)
        if opcion.largo_maximo is not None:
            largo = opcion.largo_maximo
    if errores:
        raise RespuestasInvalidas(errores)
    procedimientos = [p for p in plantilla.procedimientos if p in elegidos] if declara else list(plantilla.procedimientos)
    return Ajustes(procedimientos=procedimientos, largo_maximo=largo, nombre=nombre)
