"""Validación de un valor contra su opción del registro (SPEC-37, HU-CFG-03)."""

from __future__ import annotations

from typing import Any

from core.config_registro.opciones import POR_CLAVE


def validar_valor(clave: str, valor: Any) -> Any:
    """Devuelve `valor` si la opción lo acepta; si no, `ValueError` con el mensaje para la persona.

    Cubre las opciones de elegir una (lista cerrada de valores) y los números con rango. El resto
    de tipos los valida el modelo de entrada de cada endpoint.
    """
    opcion = POR_CLAVE[clave]
    if opcion.tipo == "elegir_una" and opcion.valores:
        validos = [v.valor for v in opcion.valores]
        if valor not in validos:
            raise ValueError(f"«{opcion.etiqueta}»: «{valor}» no es válido. Usa uno de: {', '.join(validos)}.")
    elif opcion.tipo == "entero":
        try:
            numero = int(valor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"«{opcion.etiqueta}» debe ser un número entero.") from exc
        if not (opcion.minimo <= numero <= opcion.maximo):
            raise ValueError(f"«{opcion.etiqueta}» debe estar entre {int(opcion.minimo)} y {int(opcion.maximo)}.")
    return valor
