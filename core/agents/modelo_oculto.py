"""El modelo de un agente no se le muestra al negocio (ADR-0210 §1, HU-BILL-24).

El negocio elige un nivel de IA (`agents.nivel_ia`); el modelo que corre por debajo solo lo ve el
master. Este módulo quita del contenido de un borrador o versión las claves que nombran un modelo y,
al guardar un borrador de quien no es master, las repone desde el borrador guardado: así el editor
del panel puede reenviar el contenido sin esas claves sin perderlas, y el gestor no puede cambiarlas.

Claves ocultas por contrato:
- `agentspec/3`: `model` (primario, router y respaldo).
- `kernelspec/1`: `modelPolicy` y `routing.coprocessor.model`.
"""

from __future__ import annotations

import copy
from typing import Any

_RUTAS: dict[str, tuple[tuple[str, ...], ...]] = {
    "agentspec/3": (("model",),),
    "kernelspec/1": (("modelPolicy",), ("routing", "coprocessor", "model")),
}
_FALTA = object()


def _rutas(contenido: Any) -> tuple[tuple[str, ...], ...]:
    if not isinstance(contenido, dict):
        return ()
    return _RUTAS.get(str(contenido.get("contract") or ""), ())


def _leer(contenido: dict[str, Any], ruta: tuple[str, ...]) -> Any:
    nodo: Any = contenido
    for clave in ruta:
        if not isinstance(nodo, dict) or clave not in nodo:
            return _FALTA
        nodo = nodo[clave]
    return nodo


def _quitar(contenido: dict[str, Any], ruta: tuple[str, ...]) -> None:
    nodo: Any = contenido
    for clave in ruta[:-1]:
        nodo = nodo.get(clave) if isinstance(nodo, dict) else None
    if isinstance(nodo, dict):
        nodo.pop(ruta[-1], None)


def _poner(contenido: dict[str, Any], ruta: tuple[str, ...], valor: Any) -> None:
    nodo = contenido
    for clave in ruta[:-1]:
        hijo = nodo.get(clave)
        if not isinstance(hijo, dict):
            return  # el padre no existe en lo enviado: el valor por defecto del contrato aplica
        nodo = hijo
    nodo[ruta[-1]] = copy.deepcopy(valor)


def sin_modelo(contenido: Any) -> Any:
    """Copia del contenido sin las claves de modelo. Un contrato desconocido sale igual."""
    rutas = _rutas(contenido)
    if not rutas:
        return contenido
    copia = copy.deepcopy(contenido)
    for ruta in rutas:
        _quitar(copia, ruta)
    return copia


def con_modelo_guardado(enviado: Any, guardado: Any) -> Any:
    """`enviado` con las claves de modelo de `guardado` (lo que mande quien no es master se ignora).

    Si el borrador guardado no tenía la clave, también se quita de lo enviado."""
    rutas = _rutas(enviado)
    if not rutas:
        return enviado
    resultado = copy.deepcopy(enviado)
    for ruta in rutas:
        _quitar(resultado, ruta)
        valor = _leer(guardado, ruta) if isinstance(guardado, dict) else _FALTA
        if valor is not _FALTA:
            _poner(resultado, ruta, valor)
    return resultado
