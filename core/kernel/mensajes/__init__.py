"""Módulo común de variables y render de mensajes (ADR-0178, pista D1).

PURO: ni base, ni red, ni entorno, y sin importar del gateway, del orquestador ni de los packs (lo comprueba
`tests/test_mensajes_modulo_comun.py`). Reúne lo que antes vivía repartido entre `gateway/event_notices.py`
(ADR-0108) y `kernel/opciones/`: el saneo de parámetros, la resolución de rutas con lista permitida, el
render de cada dialecto y el conversor entre ellos. Los nombres antiguos siguen funcionando porque esos
módulos los reexportan.
"""

from core.kernel.mensajes.conversor import a_llaves, a_meta, nombre_de_parametro, variables_de
from core.kernel.mensajes.eventos import COBRANZA, EVENTOS
from core.kernel.mensajes.plantillas_meta import (
    componente,
    placeholders_del_cuerpo,
    tiene_variables_fuera_del_cuerpo,
)
from core.kernel.mensajes.render import (
    LLAVE,
    LLAVE_DE_RUTA,
    PLACEHOLDER_META,
    render_fallback,
    render_llaves,
    render_meta,
)
from core.kernel.mensajes.rutas import (
    RUTA_VARIABLE,
    DatoFaltante,
    RutaNoPermitida,
    resolver_ruta,
    valor_de_ruta,
)
from core.kernel.mensajes.saneo import MAX_PARAM, clean_param

__all__ = [
    "COBRANZA",
    "EVENTOS",
    "LLAVE",
    "LLAVE_DE_RUTA",
    "MAX_PARAM",
    "PLACEHOLDER_META",
    "RUTA_VARIABLE",
    "DatoFaltante",
    "RutaNoPermitida",
    "a_llaves",
    "a_meta",
    "clean_param",
    "componente",
    "nombre_de_parametro",
    "placeholders_del_cuerpo",
    "render_fallback",
    "render_llaves",
    "render_meta",
    "resolver_ruta",
    "tiene_variables_fuera_del_cuerpo",
    "valor_de_ruta",
    "variables_de",
]
