"""Puente entre las claves legadas de un módulo y las canónicas de la política (HU-CRM-13).

Los módulos guardan su exigencia de datos como listas de claves propias (`checkout_fields`,
`campos_de_inscripcion`). `requeridos_efectivos` aplica sobre esa lista lo que el dueño ajustó,
sin tocar nada si no ajustó nada: la lista que sale es la misma que entró.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.datos_cliente.evaluar import niveles_efectivos
from core.datos_cliente.modelo import Nivel
from core.datos_cliente.registro import registro_datos
from core.datos_cliente.servicio import ajustes_de
from core.memory.perfil import normalizar_clave


def aplicar_niveles(
    legado: list[str], niveles: Mapping[str, Nivel], a_legado: Mapping[str, str] | None = None
) -> list[str]:
    """`legado` sin las claves que la política baja de «requerido», y con las que sube.

    Una clave legada que la política no conoce (p. ej. un campo propio del módulo) se conserva.
    `a_legado` dice cómo se llama en el módulo una clave canónica que se sube y no estaba.
    """
    salida = [k for k in legado if niveles.get(normalizar_clave(k), Nivel.REQUERIDO) is Nivel.REQUERIDO]
    presentes = {normalizar_clave(k) for k in salida}
    for canonica, nivel in niveles.items():
        if nivel is Nivel.REQUERIDO and canonica not in presentes:
            salida.append((a_legado or {}).get(canonica, canonica))
            presentes.add(canonica)
    return salida


def base_desde_lista(legado: list[str]) -> dict[str, Nivel]:
    """La base que sale de la lista de claves que el módulo ya exige: todas «requerido»."""
    return {normalizar_clave(k): Nivel.REQUERIDO for k in legado}


async def requeridos_efectivos(
    accion_id: str, legado: list[str], a_legado: Mapping[str, str] | None = None
) -> list[str]:
    """`legado` con el ajuste del dueño aplicado. La base es la propia lista: no se relee la
    configuración del módulo, así que lo que el módulo ya decidió para este turno no se contradice."""
    accion = registro_datos.obtener(accion_id)
    if accion is None:
        return list(legado)
    niveles = niveles_efectivos(accion, await ajustes_de(accion_id), base_desde_lista(legado))
    return aplicar_niveles(list(legado), niveles, a_legado)
