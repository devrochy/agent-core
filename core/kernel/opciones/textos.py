"""Textos fijos del resolver del toque (SPEC-40, ADR-0172 §4; invariante 3).

Viven en el playbook del kernel `opciones_directas` (`core/playbooks/kernel/`), que viaja con el binario y
ningún negocio reemplaza. Son las respuestas del SISTEMA cuando un toque no llega a ejecutar (vencido,
retirado, ya atendido…); la respuesta de cada opción la escribe el dueño en la definición. Fail-closed: un
playbook roto lanza (lo cubre un test de CI) en vez de dejar al resolver sin sus mensajes.
"""

from __future__ import annotations

from functools import cache

from core.kernel.confirmacion import MAX_TITULO_BOTON
from core.kernel.procedures.textos import secciones
from core.kernel.tarjeta import RotulosInvalidos

CLAVES = (
    "envio_vencido",
    "opcion_retirada",
    "ya_atendido",
    "en_curso",
    "sin_confirmar",
    "confirmacion_pendiente",
    "confirmacion_vencida",
    "cancelado",
    "error_generico",
    "ok_generico",
    "confirmacion_no_disponible",
    "boton_confirmar",
    "boton_cancelar",
    "instruccion_boton",
    "instruccion_texto",
)
_BOTONES = ("boton_confirmar", "boton_cancelar")


def parsear(contenido: str) -> dict[str, str]:
    crudas = secciones(contenido)
    faltan = [c for c in CLAVES if len(crudas.get(c, [])) != 1]
    if faltan:
        raise RotulosInvalidos(f"textos de opciones directas incompletos o con varias líneas: {faltan}")
    textos = {c: crudas[c][0] for c in CLAVES}
    for clave in _BOTONES:
        if len(textos[clave]) > MAX_TITULO_BOTON:
            raise RotulosInvalidos(f"el título del botón {clave} pasa de {MAX_TITULO_BOTON} caracteres")
    return textos


@cache
def textos() -> dict[str, str]:
    from core.agents.playbooks import load_core_playbook

    return parsear(load_core_playbook("opciones_directas"))


def texto(clave: str) -> str:
    return textos()[clave]
