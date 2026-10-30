"""Contrato entre el core y los packs para los datos de prueba por agente (HU-QA-10, ADR-0146).

Un pack que sabe sembrar datos de prueba declara una o más `FormaDeDatos` (catálogo, planes,
agenda, cartera): el esquema que valida lo que genera el LLM, los datos de respaldo cuando no
hay LLM y la habilidad del agente que los usa. El core elige la forma según las habilidades del
agente, genera, valida y le pide al pack que siembre (`sembrar_datos_de_prueba`) y que retire
lo sembrado (`retirar_datos_de_prueba`, sin borrado físico: invariante 8).

Los escenarios de datos de prueba (HU-QA-13, adenda de ADR-0135) usan además
`sembrar_escenario`: el core crea clientes y conversaciones y le pide al pack la actividad de sus
tablas (pedidos, suscripciones, cobranza). `EntradaDeEscenario` lleva todo lo que el pack
necesita, para que ningún pack lea las tablas de otro (invariante 5).

Vive en el core y no en `agentcore_contracts`: lo usan solo los packs instalados con el
framework, como `ContenidoCargado` (ADR-0124).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from core.subscriptions import PlanSnapshot


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
class PlanParaPrueba:
    """Un plan del catálogo de Suscripciones que sembró el lote de datos de prueba."""

    plan_id: uuid.UUID
    plan: Mapping[str, Any]


@dataclass(frozen=True)
class ContextoDeNegocio:
    """Lo que el pack necesita del negocio para sembrar (moneda de la cartera, zona de las citas)."""

    moneda: str
    zona_horaria: str
    sufijo: str  # corto y único por lote: SKUs y nombres que no chocan con los del negocio
    # Planes que sembró Suscripciones para el mismo lote (los usa para inscribir a sus clientes).
    planes: tuple[PlanParaPrueba, ...] = ()


@dataclass(frozen=True)
class RegistroSembrado:
    """Un registro que creó la siembra, para poder retirarlo después."""

    kind: str
    ref: uuid.UUID
    # Datos que el pack necesita devolver al core para encadenar otra siembra (p. ej. el snapshot
    # del plan creado para que la siguiente forma arme sus clientes).
    metadata: Mapping[str, Any] | None = None


class EscenarioNoDisponible(Exception):
    """El pack no sabe crear ese escenario (o le falta algo); el mensaje es el motivo que ve el tester."""


@dataclass(frozen=True)
class PlanDeEscenario:
    """Un plan que sembró Suscripciones, con el snapshot que arma el servidor (nunca el LLM)."""

    plan_id: uuid.UUID
    plan: PlanSnapshot


@dataclass(frozen=True)
class ClienteDeEscenario:
    """Un cliente que creó el core para el escenario, con su conversación (canal `web`)."""

    customer_id: uuid.UUID
    conversation_id: uuid.UUID
    nombre: str


@dataclass(frozen=True)
class EntradaDeEscenario:
    """Lo que el core le pasa al pack para un escenario (HU-QA-13).

    - `sufijo`: corto y único por caso; hace únicas las claves de idempotencia y los nombres.
    - `clientes`: los clientes ficticios que creó el core (sin teléfono).
    - `planes`: los planes de prueba que sembró `pack_subscriptions` para el escenario de suscripciones.
    - `autor_admin_id`: el dueño de prueba, para lo que se registra «a nombre de» alguien.
    """

    sufijo: str
    moneda: str
    zona_horaria: str
    clientes: tuple[ClienteDeEscenario, ...] = ()
    planes: tuple[PlanDeEscenario, ...] = ()
    autor_admin_id: uuid.UUID | None = None


@dataclass
class ResumenDeEscenario:
    """Lo que creó el pack: conteos por tipo y líneas legibles para la tarjeta del escenario."""

    conteos: dict[str, int] = field(default_factory=dict)
    detalle: list[str] = field(default_factory=list)
    planes: list[PlanDeEscenario] = field(default_factory=list)


__all__ = [
    "ClienteDeEscenario",
    "ContextoDeNegocio",
    "EntradaDeEscenario",
    "EscenarioNoDisponible",
    "FormaDeDatos",
    "PlanDeEscenario",
    "PlanParaPrueba",
    "RegistroSembrado",
    "ResumenDeEscenario",
]
