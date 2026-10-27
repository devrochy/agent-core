"""Contexto de un envío de opciones directas (SPEC-40, ADR-0172 §4, ADR-0108, HU-OPC-02).

El contexto de un envío es el mapa plano `ruta → texto` (`cliente.nombre → Ana`) que el servidor guarda en
`option_sends.context` al enviar. De ahí salen, sin pasar nunca por el LLM ni por lo que escriba el cliente:

- las variables del cuerpo (`{cliente.nombre}`),
- los argumentos de la tool (`args_contexto`: argumento → ruta) cuando el cliente toca una opción,
- las citas `{ctx.ruta}` de las respuestas fijas.

Dos fuentes, las dos del SERVIDOR (mismo principio que `event_notices.resolve_values`):

1. `VARIABLES_BASE`: rutas que el servidor resuelve de la propia conversación (el nombre del cliente, el del
   negocio). El catálogo de variables del panel (PR de la API) parte de aquí.
2. `datos`: rutas que arma el código que dispara el envío (un aviso por evento, un segmento, el botón manual del
   panel) desde SU contexto de confianza. Cada ruta tiene la forma de una variable y no puede tapar a una de la
   base. Un valor que no es texto o número simple no se acepta.

Si el envío no puede resolver una ruta que la acción o el cuerpo NECESITAN, no se envía nada: un botón cuyo
toque va a fallar con `contexto_faltante` es peor que no mandarlo. Las citas `{ctx.x}` de las respuestas son
opcionales: si falta, la respuesta usa el texto genérico (`argumentos.renderizar_respuesta`).

Solo se guardan las rutas que la definición cita (mínimo necesario, sin datos de más). Todo valor pasa por
`clean_param` (sin saltos de línea, acotado) y se guarda como texto. `cliente.nombre` es el nombre de perfil del
canal (o lo que el agente haya guardado): es DATO del cliente, no instrucción, y se acota a 60.

Puro: ni base, ni red, ni entorno. Quien llama lee el cliente y el negocio y pasa `base`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.kernel.opciones.definicion import RUTA_VARIABLE, Definicion, llaves_de

# Rutas que el servidor resuelve de la conversación. El texto describe el dato para el panel (PR de la API).
VARIABLES_BASE: dict[str, str] = {
    "cliente.nombre": "Nombre del cliente",
    "negocio.nombre": "Nombre del negocio",
}
# `customer.name` lo puede escribir el LLM con `update_customer_profile` y viene del perfil del canal: el nombre
# de una persona se acota a 60 (ADR-0108, como `provisioning/owner.py` y `admin/envios_segmento.py`).
MAX_NOMBRE = 60
RUTAS_DE_NOMBRE = frozenset({"cliente.nombre"})


class ContextoIncompleto(ValueError):
    """Falta el valor de una ruta que el cuerpo o una acción necesitan. `rutas`: solo los nombres, nunca valores."""

    def __init__(self, rutas: list[str]) -> None:
        super().__init__(f"contexto incompleto: {', '.join(rutas[:5])}")
        self.rutas = rutas


class DatosInvalidos(ValueError):
    """Los `datos` del llamador no tienen la forma permitida (ruta mal escrita, valor no simple, o intenta
    tapar una ruta de la base). Es un error de programación de quien dispara el envío, no del cliente."""


def _texto(valor: Any, limite: int | None = None) -> str | None:
    """El valor como texto limpio; None si está vacío o no es un dato simple (un objeto, una lista, un booleano)."""
    if valor is None or isinstance(valor, bool) or not isinstance(valor, str | int | float):
        return None
    from core.gateway.event_notices import clean_param

    limpio = clean_param(str(valor)) if limite is None else clean_param(str(valor), limite)
    return limpio or None


def normalizar_datos(datos: Mapping[str, Any] | None) -> dict[str, str]:
    """`datos` del llamador a `ruta → texto`. Lanza `DatosInvalidos` si una ruta no tiene la forma de una
    variable o si tapa una de la base; los valores vacíos o no simples se descartan (la ruta queda sin resolver)."""
    salida: dict[str, str] = {}
    for ruta, valor in (datos or {}).items():
        if not isinstance(ruta, str) or not RUTA_VARIABLE.match(ruta):
            raise DatosInvalidos("ruta de dato con forma inválida")
        if ruta in VARIABLES_BASE:
            raise DatosInvalidos(f"«{ruta}» es una variable base: la resuelve el servidor")
        texto = _texto(valor)
        if texto is not None:
            salida[ruta] = texto
    return salida


def rutas_citadas(definicion: Definicion) -> tuple[list[str], list[str]]:
    """`(necesarias, opcionales)`, sin repetir y en orden de aparición.

    - Necesarias: las variables que usa el cuerpo y los datos de `args_contexto` de cada acción.
    - Opcionales: las `{ctx.ruta}` de las respuestas que no son necesarias por otra parte."""
    necesarias: list[str] = []

    def _sumar(destino: list[str], ruta: str) -> None:
        if ruta not in destino:
            destino.append(ruta)

    for ruta in llaves_de(definicion.body):
        _sumar(necesarias, ruta)
    for opcion in definicion.opciones:
        for ruta in opcion.accion.args_contexto.values():
            _sumar(necesarias, ruta)
    opcionales: list[str] = []
    for opcion in definicion.opciones:
        for texto in (opcion.respuesta_ok, opcion.respuesta_error):
            for llave in llaves_de(texto):
                origen, _, resto = llave.partition(".")
                if origen == "ctx" and resto and resto not in necesarias:
                    _sumar(opcionales, resto)
    return necesarias, opcionales


def armar(definicion: Definicion, base: Mapping[str, Any], datos: Mapping[str, Any] | None = None) -> dict[str, str]:
    """El contexto del envío: `ruta → texto` de las rutas que la definición cita. `ContextoIncompleto` si falta
    una necesaria. `base` solo se lee en las rutas de `VARIABLES_BASE`; `datos` pasa por `normalizar_datos`."""
    disponibles = {
        r: t
        for r, v in base.items()
        if r in VARIABLES_BASE and (t := _texto(v, MAX_NOMBRE if r in RUTAS_DE_NOMBRE else None)) is not None
    }
    disponibles.update(normalizar_datos(datos))
    necesarias, opcionales = rutas_citadas(definicion)
    faltan = [r for r in necesarias if r not in disponibles]
    if faltan:
        raise ContextoIncompleto(faltan)
    return {r: disponibles[r] for r in (*necesarias, *opcionales) if r in disponibles}
