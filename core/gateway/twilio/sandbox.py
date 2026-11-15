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

from opentelemetry import metrics

from core.config import get_settings
from core.shared.logger import get_logger

log = get_logger(__name__)

# Observabilidad (adenda del ADR-0186 §6). Etiquetas cerradas: nunca negocio, cuenta ni teléfono.
EVENTOS = frozenset(
    {
        "alta_creada",
        "alta_repetida",
        "alta_rechazada",
        "gate_apagado",
        "webhook_confirmado",
        "firma_invalida",
        "no_unido",
        "reintento_ritmo",
        "desconectado",
    }
)
CODIGO_NO_UNIDO = 63015
_meter = metrics.get_meter("agent-core")
_eventos = _meter.create_counter(
    "agentcore_twilio_sandbox_events_total",
    description="Eventos del Sandbox de WhatsApp de Twilio (HU-CONN-20), por evento",
)
# Las conexiones sin confirmar van en el log `twilio_salud_ronda` (`sandbox_sin_confirmar`), no en un gauge: con el
# worker en prefork cada hijo exportaría el último valor que fijó y la serie nunca bajaría.

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


def contar(evento: str) -> None:
    """Suma un evento del sandbox. Nunca rompe el camino que lo registra."""
    try:
        _eventos.add(1, {"evento": evento if evento in EVENTOS else "otro"})
    except Exception as exc:
        log.warning("metrica_fallo", error_type=type(exc).__name__)


def es_from_del_sandbox(credenciales: dict[str, str]) -> bool:
    """¿Estas credenciales de envío salen del número compartido? (para medir el ritmo del sandbox)."""
    return credenciales.get("from") == f"whatsapp:{e164()}"
