"""Kill-switch del canal Telegram (ADR-0199, D-74): `TELEGRAM_HABILITADO`, ENCENDIDO por defecto.

Telegram está disponible para todos los negocios, sin bandera por negocio. Esta variable de entorno es la única
forma de cerrarlo entero (un incidente, un abuso, una caída de la Bot API): apagada, el tipo de conexión no está
disponible en el catálogo, las rutas de alta responden 404, el webhook 503 (Telegram reintenta), la salida no envía y
el cron de salud no corre. Fail-closed: cualquier valor que no sea el booleano `True` cuenta como apagado.

Este es el ÚNICO módulo que lee la variable (una prueba de arquitectura lo vigila).
"""

from __future__ import annotations

from core.config import get_settings


def habilitado() -> bool:
    return get_settings().telegram_habilitado is True
