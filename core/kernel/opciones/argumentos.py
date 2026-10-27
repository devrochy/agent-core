"""Argumentos de la acción de una opción y respuesta fija al cliente (SPEC-40, ADR-0172 §4 y §5).

Los argumentos salen SOLO de la definición (`args_fijos`) y del contexto que el servidor guardó al enviar
(`option_sends.context`, `args_contexto`): nunca del texto del cliente ni del LLM (invariante 7, ADR-0108).
Se validan contra el JSON Schema de la tool con `esquema.errores` — otra vez al ejecutar, porque la tool
pudo cambiar de schema desde que se publicó la versión (`option_args_drift`).

La respuesta es un texto fijo con `{ctx.x}` y `{result.k}`: solo las claves de `result_keys`, solo cadenas o
números (un booleano, una lista o un objeto no se citan) y todo valor pasa por `clean_param` (sin saltos de
línea, acotado). Si algún valor citado no está, `renderizar_respuesta` devuelve None y quien llama usa un
texto genérico: un hueco en una frase que el cliente lee (`tu cita es el .`) es peor que una frase corta.

Puro: ni base, ni red, ni entorno.
"""

from __future__ import annotations

import copy
from collections.abc import Collection, Mapping
from typing import Any

from core.kernel.opciones import esquema
from core.kernel.opciones.definicion import LLAVES, Accion


class ArgumentosInvalidos(ValueError):
    """Los argumentos no se pueden armar o no cumplen el schema de la tool. `codigo`: `contexto_faltante`
    (una ruta de `args_contexto` que el envío no resolvió) o `schema` (deriva del schema de la tool)."""

    def __init__(self, codigo: str, rutas: list[str]) -> None:
        super().__init__(f"{codigo}: {', '.join(rutas[:5])}")
        self.codigo = codigo
        self.rutas = rutas


def resolver(accion: Accion, contexto: Mapping[str, Any]) -> dict[str, Any]:
    """Argumentos de la acción: `args_fijos` más `args_contexto` resueltos desde `contexto` (ruta → valor).
    `ArgumentosInvalidos("contexto_faltante")` si falta una ruta o un nombre está en las dos fuentes."""
    args: dict[str, Any] = copy.deepcopy(accion.args_fijos)
    faltan: list[str] = []
    for nombre, ruta in accion.args_contexto.items():
        if nombre in args or ruta not in contexto:
            faltan.append(f"args_contexto.{nombre}")
            continue
        args[nombre] = copy.deepcopy(contexto[ruta])
    if faltan:
        raise ArgumentosInvalidos("contexto_faltante", faltan)
    return args


def validar(args: Mapping[str, Any], parameters: Mapping[str, Any] | None) -> None:
    """Valida `args` contra el JSON Schema de la tool. Lanza `ArgumentosInvalidos("schema")` con las rutas
    (nunca los valores). Una clave que el schema no declara es deriva aunque el schema no cierre los
    adicionales: la tool la ignoraría y el cliente creería haber confirmado algo que no corre."""
    schema = dict(parameters or {})
    declaradas = schema.get("properties")
    props = declaradas if isinstance(declaradas, dict) else {}
    rutas = [f"args.{k}" for k in args if k not in props]
    rutas += [p or "args" for p, _ in esquema.errores(dict(args), {**schema, "type": "object"}, "args")]
    if rutas:
        raise ArgumentosInvalidos("schema", sorted(set(rutas)))


def _citable(valor: Any) -> str | None:
    if isinstance(valor, bool):
        return None
    if isinstance(valor, str | int | float):
        from core.gateway.event_notices import clean_param

        texto = clean_param(str(valor))
        return texto or None
    return None


def renderizar_respuesta(
    texto: str,
    contexto: Mapping[str, Any],
    resultado: Mapping[str, Any] | None = None,
    result_keys: Collection[str] = (),
) -> str | None:
    """`texto` con cada `{ctx.ruta}` y `{result.clave}` sustituido. None si algún valor citado no está o no
    es citable, o si el placeholder no es de los dos orígenes (no se interpretan llaves desconocidas)."""
    faltante = False

    def sustituir(m: Any) -> str:
        nonlocal faltante
        origen, _, resto = str(m.group(1)).partition(".")
        valor: Any = None
        if origen == "ctx":
            valor = contexto.get(resto)
        elif origen == "result" and resto in result_keys and resultado is not None:
            valor = resultado.get(resto)
        citado = _citable(valor)
        if citado is None:
            faltante = True
            return ""
        return citado

    salida = LLAVES.sub(sustituir, texto)
    return None if faltante else salida


def usa_resultado(texto: str) -> bool:
    """¿La respuesta cita algún `{result.k}`? (Un segundo toque solo puede repetirla si no.)"""
    return any(m.partition(".")[0] == "result" for m in LLAVES.findall(texto))
