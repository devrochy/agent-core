"""Resultado del último mensaje de prueba de una conexión de WhatsApp vía Twilio.

Twilio acepta el envío (201) y el rechazo llega después por el status callback: el 63015 del Sandbox (el número no
mandó `join`) o el 63016 de un sender real (fuera de la ventana de 24 h) llegan un segundo más tarde. El mensaje de
prueba no se guarda en `messages`, así que `apply_status_update` no lo ve; su estado vive aquí, en Redis:

- `twilio_prueba_ultima:{tenant_id}:{connection_id}` → JSON `{sid, rango, provider_status, error_code,
  error_message, enviado_at, actualizado_at}` con TTL de un día (lo mismo que el tope diario de pruebas).
- Solo la última prueba por conexión: una nueva la reemplaza y un estado de la anterior se descarta.
- El estado solo avanza (`queued < sent < delivered < read`; `failed` es final): el script Lua compara y escribe en
  un solo paso, así que dos callbacks simultáneos no se pisan.

Si el callback llegara antes de que `enviar_prueba` guarde el SID (Twilio tarda al menos el envío a WhatsApp), ese
estado se pierde y el panel muestra «sin confirmación», nunca un resultado falso.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

TTL_S = 86400
# Rango del estado de `parse_status` (`status`): uno menor o igual que el guardado no se escribe.
RANGOS = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "failed": 4}
ESTADOS = {v: k for k, v in RANGOS.items()}

# ARGV: sid, rango, provider_status, error_code, error_message, actualizado_at. Devuelve 1 si escribió.
_AVANZAR = """
local actual = redis.call('GET', KEYS[1])
if not actual then return 0 end
local d = cjson.decode(actual)
if d.sid ~= ARGV[1] then return 0 end
local rango = tonumber(ARGV[2])
if tonumber(d.rango) >= rango then return 0 end
d.rango = rango
d.provider_status = ARGV[3]
d.error_code = ARGV[4]
d.error_message = ARGV[5]
d.actualizado_at = ARGV[6]
redis.call('SET', KEYS[1], cjson.encode(d), 'KEEPTTL')
return 1
"""


@dataclass(frozen=True)
class EstadoPrueba:
    sid: str
    estado: str
    provider_status: str
    error_code: int | None
    error_message: str
    enviado_at: datetime
    actualizado_at: datetime


def _clave(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    return f"twilio_prueba_ultima:{tenant_id}:{connection_id}"


def _ahora() -> str:
    return datetime.now(UTC).isoformat()


async def registrar_envio(tenant_id: uuid.UUID, connection_id: uuid.UUID, sid: str) -> None:
    """Guarda el SID recién aceptado por Twilio como la última prueba de la conexión (en `queued`)."""
    from core.memory.working import get_redis

    ahora = _ahora()
    datos = {
        "sid": sid,
        "rango": RANGOS["queued"],
        "provider_status": "queued",
        "error_code": "",
        "error_message": "",
        "enviado_at": ahora,
        "actualizado_at": ahora,
    }
    await get_redis().set(_clave(tenant_id, connection_id), json.dumps(datos), ex=TTL_S)


async def aplicar_estado(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, update: dict[str, Any]
) -> bool:
    """Aplica un status callback (el dict de `parse_status`) si es de la última prueba y avanza su estado. Devuelve si
    escribió. `ErrorCode` llega en `update["errors"]` solo cuando el estado es `failed`."""
    from core.memory.working import get_redis

    rango = RANGOS.get(str(update.get("status") or ""))
    sid = str(update.get("id") or "")
    if rango is None or not sid:
        return False
    errores = update.get("errors") or [{}]
    codigo = errores[0].get("code")
    escrito = await get_redis().eval(
        _AVANZAR,
        1,
        _clave(tenant_id, connection_id),
        sid,
        rango,
        str(update.get("provider_status") or ""),
        "" if codigo is None else str(codigo),
        str(errores[0].get("title") or "")[:300],
        _ahora(),
    )
    return bool(escrito)


async def obtener(tenant_id: uuid.UUID, connection_id: uuid.UUID, sid: str) -> EstadoPrueba | None:
    """El estado de la prueba `sid`; `None` si no es la última de esta conexión o ya venció."""
    from core.memory.working import get_redis

    crudo = await get_redis().get(_clave(tenant_id, connection_id))
    if not crudo:
        return None
    d = json.loads(crudo)
    if d.get("sid") != sid:
        return None
    codigo = str(d.get("error_code") or "")
    return EstadoPrueba(
        sid=sid,
        estado=ESTADOS.get(int(d.get("rango", 0)), "queued"),
        provider_status=str(d.get("provider_status") or ""),
        error_code=int(codigo) if codigo.isdigit() else None,
        error_message=str(d.get("error_message") or ""),
        enviado_at=datetime.fromisoformat(d["enviado_at"]),
        actualizado_at=datetime.fromisoformat(d["actualizado_at"]),
    )
