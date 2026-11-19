"""Redacción al leer de lo que se muestra en el depurador del turno (HU-AOS-28, ADR-0169 §3, P-17).

Se ve la forma (claves, tipos, códigos, estados, montos) y se enmascara lo personal:
- por la clave (correo, teléfono, documento, dirección, titular, tarjeta, cuenta, credenciales);
- por la clase del dato del procedimiento (`pii`, y `payment` siempre);
- por el patrón del valor en cualquier texto (correos y secuencias de 7 dígitos o más).

Función pura y heurística: no cambia lo guardado (`audit_log` es inmutable) y un dato personal bajo
una clave neutra y sin forma reconocible puede verse (límite aceptado en el ADR).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from core.shared.texto_seguro import enmascarar_correo

OCULTO = "•••"
MAX_PROFUNDIDAD = 8

# Segmentos de clave sensibles (sin tildes, en minúsculas). Se comparan contra cada segmento de la
# clave partida por `_`, `-`, `.` y camelCase: `customer_email`, `datos.telefono`, `cardNumber`.
_SENSIBLES = frozenset(
    {
        "email", "correo", "mail",
        "phone", "telefono", "celular", "movil", "whatsapp", "tel",
        "documento", "document", "cedula", "nit", "dni", "rut", "pasaporte", "passport", "identificacion",
        "direccion", "address", "domicilio",
        "titular", "holder",
        "tarjeta", "card", "cvv", "cvc", "iban", "account",
        "token", "password", "contrasena", "secret", "secreto", "apikey",
    }
)
_CLASES = frozenset({"pii", "payment"})

_CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 7 o más dígitos, admitiendo espacios, puntos, guiones y paréntesis entre ellos (teléfonos,
# documentos, tarjetas). Un monto con decimales cortos o un año no llegan a 7 dígitos seguidos.
_DIGITOS = re.compile(r"\+?\d(?:[\s().-]?\d){6,}")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def clave_sensible(clave: object) -> bool:
    """¿La clave nombra un dato personal o una credencial?"""
    if not isinstance(clave, str) or not clave:
        return False
    partes = re.split(r"[_\-.\s]+", _CAMEL.sub("_", clave))
    return any(_sin_tildes(p).lower() in _SENSIBLES for p in partes if p)


def _ultimos(digitos: str) -> str:
    solo = re.sub(r"\D", "", digitos)
    return f"{OCULTO}{solo[-4:]}" if len(solo) >= 4 else OCULTO


def _valor_sensible(valor: Any) -> Any:
    """Un valor bajo una clave o clase sensible: se oculta conservando una pista mínima."""
    if valor is None or isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        cifras = str(abs(int(valor)))
        return _ultimos(cifras) if len(cifras) >= 7 else OCULTO
    if isinstance(valor, str):
        if _CORREO.fullmatch(valor.strip()):
            return enmascarar_correo(valor)
        if _DIGITOS.fullmatch(valor.strip()):
            return _ultimos(valor)
        return OCULTO
    return OCULTO


def enmascarar_texto(texto: str) -> str:
    """Enmascara correos y secuencias largas de dígitos dentro de un texto libre."""
    texto = _CORREO.sub(lambda m: enmascarar_correo(m.group(0)), texto)
    return _DIGITOS.sub(lambda m: _ultimos(m.group(0)), texto)


def enmascarar(valor: Any, clave: object = None, data_class: object = None, *, _nivel: int = 0) -> Any:
    """Copia redactada de `valor` (dict, list o escalar). No muta la entrada."""
    if data_class in _CLASES or clave_sensible(clave):
        if isinstance(valor, Mapping):
            return {k: _valor_sensible(v) if not isinstance(v, (Mapping, list)) else OCULTO for k, v in valor.items()}
        if isinstance(valor, list):
            return [OCULTO for _ in valor]
        return _valor_sensible(valor)
    if _nivel >= MAX_PROFUNDIDAD:
        return OCULTO
    if isinstance(valor, Mapping):
        return {k: enmascarar(v, k, _nivel=_nivel + 1) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [enmascarar(v, _nivel=_nivel + 1) for v in valor]
    if isinstance(valor, str):
        return enmascarar_texto(valor)
    return valor


__all__ = ["OCULTO", "clave_sensible", "enmascarar", "enmascarar_texto"]
