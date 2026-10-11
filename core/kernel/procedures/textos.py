"""Textos del motor: errores de negocio y léxico de confirmación (invariante 3, HU-AOS-05).

Viven en los playbooks del kernel `motor` y `confirmacion` (`core/playbooks/kernel/`), que
viajan con el binario y ningún negocio reemplaza. Fail-closed: un playbook roto lanza (lo cubre
un test de CI), en vez de dejar al motor sin sus mensajes.

El clasificador de confirmación es conservador a propósito: confianza alta solo si la respuesta
del cliente, normalizada, coincide exacta con una frase del léxico. "dale", "ok" o "listo" no
confirman. HU-AOS-09 trae el clasificador de alta precisión y el botón.
"""

from __future__ import annotations

import re
import unicodedata
from functools import cache

from core.kernel.tarjeta import RotulosInvalidos

MENSAJES = {
    "fuera_de_etapa": ("accion", "etapa"),
    "confirmacion_requerida": ("accion",),
    "falta_confirmacion": (),
    "sin_confirmacion_pendiente": (),
    "sin_tarea": (),
    "dato_desconocido": ("dato",),
    "dato_del_servidor": ("dato",),
    "tipo_invalido": ("dato", "esperado"),
    "enum_invalido": ("dato",),
    "formato_invalido": ("dato", "esperado"),
    "resolver_rechazo": ("dato", "error"),
    "resolver_no_disponible": ("dato",),
    "procedimiento_no_disponible": (),
    "tarea_en_curso": (),
    "error_interno": (),
}
_SECCION = re.compile(r"^## ([a-z_]+)$")
_MAX_ERROR_RESOLVER = 300


def _secciones(contenido: str) -> dict[str, list[str]]:
    salida: dict[str, list[str]] = {}
    actual: str | None = None
    for linea in contenido.splitlines():
        m = _SECCION.match(linea)
        if m:
            actual = str(m.group(1))
            salida[actual] = []
        elif actual is not None and linea.strip():
            salida[actual].append(linea.strip())
    return salida


def parsear_mensajes(contenido: str) -> dict[str, str]:
    secciones = _secciones(contenido)
    faltan = sorted(set(MENSAJES) - set(secciones))
    if faltan:
        raise RotulosInvalidos(f"faltan mensajes del motor: {faltan}")
    mensajes: dict[str, str] = {}
    for clave, campos in MENSAJES.items():
        lineas = secciones[clave]
        if len(lineas) != 1:
            raise RotulosInvalidos(f"el mensaje {clave} debe ser una sola línea")
        try:
            lineas[0].format(**{c: "x" for c in campos})
        except (KeyError, IndexError, ValueError) as exc:
            raise RotulosInvalidos(f"el mensaje {clave} usa un campo desconocido: {exc}") from exc
        mensajes[clave] = lineas[0]
    return mensajes


@cache
def mensajes() -> dict[str, str]:
    from core.agents.playbooks import load_core_playbook

    return parsear_mensajes(load_core_playbook("motor"))


def mensaje(clave: str, **campos: str) -> str:
    return mensajes()[clave].format(**campos)


def error_de_resolver(texto: str) -> str:
    """El error de un resolver lo escribe la habilidad: una línea, acotada."""
    limpio = " ".join(str(texto or "").split())
    return limpio[:_MAX_ERROR_RESOLVER]


# ---------------------------------------------------------------- confirmación


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes, sin signos ni emojis, espacios simples."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFKD", texto or "") if unicodedata.category(c) != "Mn"
    )
    solo = re.sub(r"[^a-z0-9ñ ]+", " ", sin_tildes.lower())
    return " ".join(solo.split())


def parsear_lexico(contenido: str) -> frozenset[str]:
    frases = frozenset(normalizar(f) for f in _secciones(contenido).get("si", []) if normalizar(f))
    if not frases:
        raise RotulosInvalidos("el léxico de confirmación no tiene frases")
    return frases


@cache
def lexico_si() -> frozenset[str]:
    from core.agents.playbooks import load_core_playbook

    return parsear_lexico(load_core_playbook("confirmacion"))


def confirmacion_alta(texto: str, lexico: frozenset[str] | None = None) -> bool:
    """¿El texto del cliente es un "sí" explícito con confianza alta?"""
    return normalizar(texto) in (lexico if lexico is not None else lexico_si())
