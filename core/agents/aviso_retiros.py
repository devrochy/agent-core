"""Aviso en palabras simples de las referencias retiradas (HU-AOS-59, ADR-0184). Puro.

Convierte lo que detecta `core.agents.referencias_retiradas` en un texto que un dueño de negocio
entiende: qué parte del agente ya no existe y qué hacer, sin códigos internos
(`accion_inexistente`, `procedimiento_inexistente`). Es el único lugar donde se redacta: el aviso del
editor, el de publicar y el del panel del master leen de aquí, así el texto es el mismo.

Solo avisa. No corrige nada: la corrección (`migrar-referencias`) es de una persona del equipo.

No lee base, red ni entorno.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from core.agents.referencias_retiradas import (
    CODIGO_ACCION,
    Binario,
    hallazgos_de_contenido,
)
from core.agents.retiros import TipoDeNombre, retiro_de
from core.kernel.procedures import Issue

# Código del `Issue` que lleva este aviso a publicar: no sale en el cuerpo de la respuesta
# (`_contrato_422` solo expone `path: message`), sirve para reconocerlo.
CODIGO_AVISO = "referencia_retirada"

Tipo = Literal["action", "procedure"]


def tipo_de(codigo: str) -> TipoDeNombre:
    """`accion_inexistente` → `accion`; todo lo demás es un procedimiento."""
    return "accion" if codigo == CODIGO_ACCION else "procedimiento"


def tipo_api(tipo: TipoDeNombre) -> Tipo:
    return "action" if tipo == "accion" else "procedure"


def _donde(ruta: str) -> str:
    """Dónde del agente está la referencia, en palabras del editor."""
    if ruta.startswith("tools["):
        return "las herramientas del agente"
    if ruta.startswith("guardrails["):
        return "las reglas del agente"
    if ruta.startswith("exposes.consultas["):
        return "las consultas que el agente ofrece"
    if ruta.startswith("procedures["):
        return "los procedimientos del agente"
    if ruta.startswith("routing.rules["):
        return "las reglas de ruteo del coordinador"
    return "el agente"


def texto_de(ruta: str, tipo: TipoDeNombre, nombre: str, *, en_uso: bool = False) -> str:
    """El aviso de UNA referencia: qué ya no existe y qué hacer. Una oración o dos, sin códigos.
    `en_uso`: es lo que corre hoy en una conexión (una versión o una release ya publicadas), no lo
    que el dueño está editando; las rutas de un manifiesto (`components[...]`) lo son siempre."""
    retiro = retiro_de(tipo, nombre)
    en_uso = (
        " Tu negocio todavía lo usa hoy en una de sus conexiones."
        if en_uso or ruta.startswith("components[")
        else ""
    )
    if tipo == "accion":
        que = f"La herramienta «{nombre}» ya no existe.{en_uso}"
        hacer = f"Quítala de {_donde(ruta)} y vuelve a publicar el agente."
        if retiro is None:
            hacer += " Si crees que es un error, avisa al equipo de SolversHub."
    elif retiro is not None and retiro.reemplazo is not None:
        que = f"El procedimiento «{nombre}» ya no existe: ahora se llama «{retiro.reemplazo.id}».{en_uso}"
        hacer = (
            "Quita el anterior y agrega el nuevo (tu negocio necesita tener esa habilidad activa), "
            "o solo quítalo. Después vuelve a publicar el agente."
        )
    elif retiro is not None:
        que = f"El procedimiento «{nombre}» ya no existe.{en_uso}"
        hacer = f"Quítalo de {_donde(ruta)} y vuelve a publicar el agente."
    else:
        que = f"El procedimiento «{nombre}» ya no existe y no tiene un reemplazo conocido.{en_uso}"
        hacer = f"Quítalo de {_donde(ruta)} o avisa al equipo de SolversHub."
    return f"{que} {hacer}"


def avisos_de_contenido(contenido: Any, binario: Binario) -> list[tuple[str, Tipo, str, str]]:
    """`(campo, tipo, nombre, texto)` de cada referencia retirada del contenido de un agente."""
    return [
        (h.ruta, tipo_api(h.tipo), h.nombre, texto_de(h.ruta, h.tipo, h.nombre))
        for h in hallazgos_de_contenido(contenido, binario)
    ]


def problemas_de_publicacion(contenido: Mapping[str, Any], binario: Binario) -> list[Issue]:
    """Lo que impide publicar el borrador: un `Issue` por referencia retirada, con el texto simple.
    Lista vacía = nada retirado. Va ANTES de publicar: un procedimiento retirado no lo detecta el
    linter del contrato y la versión saldría publicada para fallar después, en el build."""
    return [Issue(CODIGO_AVISO, campo, texto) for campo, _tipo, _nombre, texto in avisos_de_contenido(contenido, binario)]


__all__ = [
    "CODIGO_AVISO",
    "Tipo",
    "avisos_de_contenido",
    "problemas_de_publicacion",
    "texto_de",
    "tipo_api",
    "tipo_de",
]
