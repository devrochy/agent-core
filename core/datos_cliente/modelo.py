"""Modelo de la política de datos del cliente (HU-CRM-13, ADR-0179).

Cada módulo declara las acciones en las que pide datos al cliente y, por acción, qué claves del
perfil pide y con qué nivel. Las claves son las canónicas del perfil (`core.memory.perfil`): el
módulo traduce a las suyas al validar, el contrato hacia el agente y el panel es uno solo.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class Nivel(StrEnum):
    """Qué tanto necesita la acción un dato del cliente."""

    REQUERIDO = "requerido"  # sin él la acción no se ejecuta
    RECOMENDADO = "recomendado"  # se pide con naturalidad, pero la acción sigue sin él
    NO_PEDIR = "no_pedir"  # el agente no lo pide


@dataclass(frozen=True)
class CampoPolitica:
    """Un dato que una acción puede pedir. `ajustable=False` = piso técnico: el negocio no lo
    puede bajar (p. ej. el correo para confirmar una cita)."""

    clave: str
    etiqueta: str
    nivel: Nivel
    ajustable: bool = True


# Base por negocio: lo que ya configura el módulo (p. ej. `checkout_fields`). Se llama con el
# tenant activo y devuelve `{clave: Nivel}` que reemplaza los niveles por defecto de la acción.
BaseDelNegocio = Callable[[], Awaitable[dict[str, Nivel]]]


@dataclass(frozen=True)
class AccionDatos:
    """Una acción de un módulo que puede pedir datos del cliente."""

    id: str  # «ventas.pedido_domicilio»
    modulo: str  # «ventas», «agenda», «suscripciones», «cobranza»
    pack: str  # id del plugin que la registra («pack_commerce»)
    etiqueta: str
    campos: tuple[CampoPolitica, ...]
    base: BaseDelNegocio | None = None

    def campo(self, clave: str) -> CampoPolitica | None:
        return next((c for c in self.campos if c.clave == clave), None)
