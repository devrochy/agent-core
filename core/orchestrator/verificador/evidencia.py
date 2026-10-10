"""Evidencia del turno para el verificador de afirmaciones (ADR-0119, HU-ARCH-29).

La evidencia sale de lo que el sistema hizo, nunca de lo que dijo el modelo:
- los resultados de tools del turno actual (con `_ok` y `_current_turn`, que
  estampa el loop) y de turnos anteriores (`audit_log`, `_current_turn=False`);
- el contexto que el negocio y el cliente ya pusieron en la conversación (prompt
  del sistema y mensajes del cliente), solo para las URLs: la web, el Instagram o
  el catálogo del negocio no salen de ninguna tool.

Los mensajes anteriores del propio asistente NO son evidencia: lavarían un dato
inventado en un turno previo.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from core.shared.dinero import extract_monetary_values, normalized_amount_key

TOOL_NAME_KEY = "_tool_name"

# Con esquema o empezando por `www.` (WhatsApp también enlaza esas). Los dominios
# sueltos ("tienda.co/pago") quedan como deuda: exigen validar el TLD.
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]{}\"'`]+", re.IGNORECASE)
# Puntuación que cierra una oración pegada a la URL ("…/abc." o "…/abc).").
_URL_COLA = ".,;:!?¡¿*_~"
# Referencias públicas tipo `OC-0003` (con la versión del link, ADR-0087: `OC-0003-r2`).
REFERENCIA_RE = re.compile(r"\b[A-Z]{2,5}-\d{3,}(?:-r\d+)?\b")


def urls_con_posicion(texto: str) -> list[tuple[int, int, str]]:
    """(inicio, fin, url) de cada URL del texto, sin la puntuación de cierre de la oración."""
    encontradas = []
    for match in URL_RE.finditer(texto or ""):
        url = match.group(0).rstrip(_URL_COLA)
        encontradas.append((match.start(), match.start() + len(url), url))
    return encontradas


def urls_en(texto: str) -> list[str]:
    return [url for _inicio, _fin, url in urls_con_posicion(texto)]


def clave_url(url: str) -> str:
    """Forma comparable de una URL: sin esquema ni `www.`, host en minúsculas y sin
    barra final (`HTTPS://Tienda.com/x/` y `www.tienda.com/x` son la misma)."""
    sin_esquema = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    host, separador, resto = sin_esquema.partition("/")
    host = host.lower().removeprefix("www.")
    return (host + separador + resto).rstrip("/")


def _claves(texto: str) -> set[str]:
    return {clave_url(url) for url in urls_en(texto)}


_CLAVE_IMAGEN = re.compile(r"image|imagen|foto|photo|thumbnail", re.IGNORECASE)
# Claves bajo las que una tool devuelve un link para el cliente (pago, checkout o el
# texto que el core agrega con `_safe_append`). La página de un producto o una imagen
# no respaldan "te envío el link de pago".
_CLAVE_ENLACE = re.compile(r"link|url|pago|payment|checkout|_safe_append", re.IGNORECASE)


# Claves con texto libre de una respuesta fallida (no datos del servidor).
_TEXTO_DEL_ERROR = frozenset({"error", "hint", "message", "mensaje"})


def _textos(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _textos(value)
    elif isinstance(node, list):
        for item in node:
            yield from _textos(item)


def _textos_de_enlace(node: Any, clave: str = "") -> Iterator[str]:
    if isinstance(node, str):
        if _CLAVE_ENLACE.search(clave) and not _CLAVE_IMAGEN.search(clave):
            yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if not _CLAVE_IMAGEN.search(str(key)):
                yield from _textos_de_enlace(value, str(key))
    elif isinstance(node, list):
        for item in node:
            yield from _textos_de_enlace(item, clave)


@dataclass
class Evidencia:
    # Montos verificados (clave normalizada), del turno actual y de los anteriores:
    # repetir un monto que ya confirmó una acción es legítimo.
    montos: set[str] = field(default_factory=set)
    # Montos del turno actual, en orden, para la instrucción de reintento.
    montos_turno: list[float] = field(default_factory=list)
    # Tools que terminaron bien en el turno actual.
    acciones_ok: set[str] = field(default_factory=set)
    # URLs devueltas por tools del turno actual (incluye `_safe_append`), también si la
    # tool terminó con error: la URL la dio el servidor, no la inventó el modelo (p. ej. los
    # enlaces legales de `terminos_pendientes`, ADR-0125). Un link de pago de un turno
    # anterior puede estar vencido (ADR-0087): no cuenta.
    urls_turno: set[str] = field(default_factory=set)
    # Las que la tool devolvió como link (pago, checkout, `_safe_append`) y terminó bien: son
    # las que respaldan "te envío el link de pago" (`Afirmacion.basta_url`).
    urls_enlace: set[str] = field(default_factory=set)
    # URLs del prompt del sistema (sin la memoria ni el perfil, que escribe el propio
    # asistente) y de los mensajes del cliente.
    # Todas las URLs se guardan con `clave_url`.
    urls_contexto: set[str] = field(default_factory=set)
    # Referencias públicas que aparecen en cualquier resultado.
    referencias: set[str] = field(default_factory=set)

    @classmethod
    def desde(cls, resultados: list[dict[str, Any]], contexto: str = "") -> Evidencia:
        ev = cls(urls_contexto=_claves(contexto))
        for resultado in resultados:
            if not isinstance(resultado, dict):
                continue
            # Sin las marcas (llamada directa en tests), se asume turno actual y éxito,
            # mismo criterio que los guardrails de HU-COM-02.
            actual = resultado.get("_current_turn", True)
            ok = resultado.get("_ok", True)
            for valor in extract_monetary_values(resultado):
                ev.montos.add(normalized_amount_key(valor))
                if actual and valor not in ev.montos_turno:
                    ev.montos_turno.append(valor)
            textos = list(_textos(resultado))
            for texto in textos:
                ev.referencias.update(REFERENCIA_RE.findall(texto))
            if actual:
                # De una tool fallida, no las del texto del error ni del `hint`: podrían repetir
                # un argumento con una URL que inventó el modelo.
                datos = resultado if ok else {k: v for k, v in resultado.items() if k not in _TEXTO_DEL_ERROR}
                for texto in _textos(datos):
                    ev.urls_turno.update(_claves(texto))
            if actual and ok:
                nombre = str(resultado.get(TOOL_NAME_KEY) or "")
                if nombre:
                    ev.acciones_ok.add(nombre)
                for texto in _textos_de_enlace(resultado):
                    ev.urls_enlace.update(_claves(texto))
        return ev
