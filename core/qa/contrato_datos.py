"""Contrato entre el core y los packs para los datos de prueba por agente (HU-QA-10, ADR-0146).

Un pack que sabe sembrar datos de prueba declara una o más `FormaDeDatos` (catálogo, planes,
agenda, cartera): el esquema que valida lo que genera el LLM, los datos de respaldo cuando no
hay LLM y la habilidad del agente que los usa. El core elige la forma según las habilidades del
agente, genera, valida y le pide al pack que siembre (`sembrar_datos_de_prueba`) y que retire
lo sembrado (`retirar_datos_de_prueba`, sin borrado físico: invariante 8).

Vive en el core y no en `agentcore_contracts`: lo usan solo los packs instalados con el
framework, como `ContenidoCargado` (ADR-0124).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class FormaDeDatos:
    """Un juego de datos de prueba que un pack sabe sembrar.

    - `modo`: id estable dentro del pack (`catalogo`, `planes`, `agenda`, `cartera`).
    - `habilidad`: el agente con esta habilidad (id de pack en sus superpoderes) usa esta forma.
      `None`: solo la usa una plantilla que la nombra (`datos_de_prueba = "<pack>.<modo>"`).
    - `prioridad`: si el agente tiene varias habilidades, gana la de menor número (la agenda
      sobre el comercio que usa solo para crear leads).
    - `esquema`: valida la salida del LLM; sus `Field(description=...)` y topes son el formato
      que ve el modelo (dato, no instrucción).
    - `respaldo`: datos válidos contra `esquema` para cuando el LLM falla.
    """

    modo: str
    titulo: str
    habilidad: str | None
    prioridad: int
    esquema: type[BaseModel]
    respaldo: Mapping[str, Any]


@dataclass(frozen=True)
class ContextoDeNegocio:
    """Lo que el pack necesita del negocio para sembrar (moneda de la cartera, zona de las citas)."""

    moneda: str
    zona_horaria: str
    sufijo: str  # corto y único por lote: SKUs y nombres que no chocan con los del negocio


@dataclass(frozen=True)
class RegistroSembrado:
    """Un registro que creó la siembra, para poder retirarlo después."""

    kind: str
    ref: uuid.UUID


__all__ = ["ContextoDeNegocio", "FormaDeDatos", "RegistroSembrado"]
