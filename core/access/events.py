"""Registro de eventos de acceso (`access_events`, ADR-0174 §4).

`registrar_evento` agrega la fila a la sesión del llamador: el evento nace en la misma
transacción que la decisión que lo provoca (si esta falla, el evento tampoco queda).
El `detalle` nunca debe llevar contraseñas ni tokens; el `device_id` completo no se loguea.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.db.models import AccessEvent
from core.shared.logger import get_logger

log = get_logger(__name__)

SEVERIDADES: tuple[str, ...] = ("info", "aviso", "critico")
CANALES: tuple[str, ...] = ("campo", "panel_apk", "campo_v1")

# Tipo → severidad por defecto (ADR-0174 §4). Los tipos son los del CHECK de la migración 0113.
SEVERIDAD_POR_TIPO: dict[str, str] = {
    "login_fallido": "aviso",
    "login_bloqueado": "critico",
    "dispositivo_nuevo": "info",
    "dispositivo_pendiente": "aviso",
    "dispositivo_aprobado": "info",
    "dispositivo_bloqueado_usado": "critico",
    "acceso_denegado_politica": "aviso",
    "dispositivo_sin_id": "aviso",
    "dispositivo_compartido": "info",
    "politica_cambiada": "aviso",
    "acceso_master": "info",
    "legado_pack": "info",
}
TIPOS: tuple[str, ...] = tuple(SEVERIDAD_POR_TIPO)


def sufijo(device_id: str | None) -> str | None:
    """Últimos 6 caracteres del equipo: lo único que se muestra en listados y se escribe en logs."""
    return device_id[-6:] if device_id else None


def registrar_evento(
    session: Any,
    *,
    tenant_id: uuid.UUID,
    tipo: str,
    severidad: str | None = None,
    admin_id: uuid.UUID | None = None,
    device_row_id: uuid.UUID | None = None,
    device_id: str | None = None,
    rol: str | None = None,
    canal: str = "campo",
    ip: str | None = None,
    detalle: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> AccessEvent:
    if tipo not in SEVERIDAD_POR_TIPO:
        raise ValueError(f"tipo de evento de acceso desconocido: {tipo}")
    severidad = severidad or SEVERIDAD_POR_TIPO[tipo]
    if severidad not in SEVERIDADES:
        raise ValueError(f"severidad desconocida: {severidad}")
    if canal not in CANALES:
        raise ValueError(f"canal desconocido: {canal}")
    evento = AccessEvent(
        tenant_id=tenant_id,
        admin_id=admin_id,
        device_row_id=device_row_id,
        device_id=device_id,
        rol=rol,
        canal=canal,
        tipo=tipo,
        severidad=severidad,
        ip=ip,
        detalle=detalle,
        request_id=request_id,
    )
    session.add(evento)
    log.info(
        "access_event",
        tenant_id=str(tenant_id),
        admin_id=str(admin_id) if admin_id else None,
        tipo=tipo,
        severidad=severidad,
        canal=canal,
        rol=rol,
        device=sufijo(device_id),
    )
    return evento
