"""Evaluador de la política de datos del cliente (HU-CRM-13, ADR-0179 decisiones 1 y 2).

Función pura: dados los datos que ya se conocen y los niveles efectivos, dice qué falta. Es el
único lugar que decide «falta»: los módulos solo traducen su respuesta legada al sobre unificado.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from core.datos_cliente.modelo import AccionDatos, Nivel
from core.memory.perfil import normalizar_clave


def _con_valor(valor: Any) -> bool:
    return bool(str(valor if valor is not None else "").strip())


def conocidos_canonicos(datos: Mapping[str, Any] | None) -> dict[str, Any]:
    """Los datos con claves canónicas del perfil, sin valores vacíos (gana el primero)."""
    salida: dict[str, Any] = {}
    for clave, valor in (datos or {}).items():
        canonica = normalizar_clave(str(clave))
        if _con_valor(valor) and canonica not in salida:
            salida[canonica] = valor
    return salida


def niveles_efectivos(
    accion: AccionDatos,
    ajustes: Mapping[str, str] | None = None,
    base: Mapping[str, Nivel] | None = None,
) -> dict[str, Nivel]:
    """`{clave: Nivel}` de la acción. Precedencia: ajuste > base del módulo > defecto.

    Un campo con `ajustable=False` ignora el ajuste y la base (piso técnico). Las claves de la
    base que la acción no declara se descartan: la superficie de la política es la del registro.
    """
    niveles: dict[str, Nivel] = {}
    for campo in accion.campos:
        nivel = campo.nivel
        if campo.ajustable:
            if base and campo.clave in base:
                nivel = base[campo.clave]
            elif base is not None and nivel is Nivel.REQUERIDO:
                # El módulo exigía el dato por defecto y su configuración ya no lo exige.
                nivel = Nivel.NO_PEDIR
            if ajustes and campo.clave in ajustes:
                try:
                    nivel = Nivel(ajustes[campo.clave])
                except ValueError:
                    pass
        niveles[campo.clave] = nivel
    return niveles


@dataclass
class Evaluacion:
    accion: str
    faltan: list[str] = field(default_factory=list)
    recomendados_pendientes: list[str] = field(default_factory=list)
    ya_registrados: list[str] = field(default_factory=list)  # solo claves, nunca valores

    @property
    def puede_continuar(self) -> bool:
        return not self.faltan

    def como_sobre(self) -> dict[str, Any]:
        """Sobre `faltan_datos` aditivo de las respuestas de las tools (ADR-0179 decisión 2)."""
        return {
            "accion": self.accion,
            "faltan": list(self.faltan),
            "recomendados_pendientes": list(self.recomendados_pendientes),
            "ya_registrados": list(self.ya_registrados),
        }


def evaluar(
    accion: AccionDatos,
    conocidos: Mapping[str, Any] | None,
    ajustes: Mapping[str, str] | None = None,
    base: Mapping[str, Nivel] | None = None,
) -> Evaluacion:
    """Qué falta para ejecutar `accion` con los datos `conocidos` (claves en cualquier alias)."""
    datos = conocidos_canonicos(conocidos)
    niveles = niveles_efectivos(accion, ajustes, base)
    ev = Evaluacion(accion=accion.id)
    for campo in accion.campos:
        nivel = niveles[campo.clave]
        if nivel is Nivel.NO_PEDIR:
            continue
        if campo.clave in datos:
            ev.ya_registrados.append(campo.clave)
        elif nivel is Nivel.REQUERIDO:
            ev.faltan.append(campo.clave)
        else:
            ev.recomendados_pendientes.append(campo.clave)
    return ev
