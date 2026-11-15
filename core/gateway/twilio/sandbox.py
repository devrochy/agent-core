"""Gate del modo Sandbox de WhatsApp vía Twilio (HU-CONN-20, adenda del ADR-0186).

El Sandbox de Twilio usa un número compartido por todas las cuentas (`+14155238886`) al que solo le llegan los
mensajes de quien mandó `join <código>`. Sirve para probar sin un número propio, así que se permite SOLO en test:
`TWILIO_SANDBOX_PERMITIDO` viene apagada y solo se enciende en las apps de test (API, worker y beat).

Este es el ÚNICO módulo que lee esa variable (una prueba de arquitectura lo vigila). El gate se revisa en la ruta y
otra vez en el servicio, el webhook y la salida: con la variable apagada, las rutas del sandbox responden 404 y las
conexiones sandbox que existan quedan inertes (no entran ni salen mensajes), pero se pueden leer y desconectar.
"""

from __future__ import annotations

import re

from core.config import get_settings

MODO_SENDER = "sender"
MODO_SANDBOX = "sandbox"
# Lo mismo que el CHECK de la migración 0136: minúsculas, números y guiones, hasta 64.
FORMATO_JOIN_CODE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
_JOIN_CODE = re.compile(FORMATO_JOIN_CODE)


def permitido() -> bool:
    """¿Este ambiente permite el sandbox? Solo el `True` de la configuración lo enciende."""
    return get_settings().twilio_sandbox_permitido is True


def e164() -> str:
    """El número compartido del sandbox, que va como `From` de cada mensaje."""
    return get_settings().twilio_sandbox_e164


def external_id(account_sid: str) -> str:
    """`channel_connections.external_id` de un sandbox: el número es de todos, la cuenta no."""
    return f"sandbox:{account_sid}"


def join_code_valido(codigo: str) -> bool:
    return len(codigo) <= 64 and bool(_JOIN_CODE.match(codigo))


def inerte(modo: str | None) -> bool:
    """¿La conexión no debe recibir ni enviar? Una sandbox en un ambiente que no permite el sandbox."""
    return modo == MODO_SANDBOX and not permitido()
