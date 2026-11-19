"""URLs firmadas del controlador de almacenamiento (ADR-0085 enmendado).

El archivo no se sirve por su ruta: `/api/v1/files/{token}` recibe un token de vida corta con
la `key` y su expiración, firmado con `APP_SECRET_KEY` (mismo patrón que el enlace de
invitación). Así el dueño no comparte rutas internas y WhatsApp puede descargar la imagen
mientras el token siga vigente.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from core.config import get_settings

_MAX_TOKEN = 1024


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).rstrip(b"=").decode()


def _de_b64(texto: str) -> bytes:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode(texto + relleno)


def _firma(key: str, exp: int) -> str:
    clave = get_settings().app_secret_key.encode()
    return _b64(hmac.new(clave, f"{key}:{exp}".encode(), hashlib.sha256).digest())


def firmar(key: str, ttl_s: int, *, ahora: float | None = None) -> str:
    """Token `{key}.{exp}.{firma}` para servir un archivo durante `ttl_s` segundos."""
    exp = int((ahora if ahora is not None else time.time()) + ttl_s)
    return f"{_b64(key.encode())}.{exp}.{_firma(key, exp)}"


def verificar(token: str, *, ahora: float | None = None) -> str | None:
    """Devuelve la `key` si el token es válido y no venció; `None` si no."""
    crudo = (token or "").strip()
    if not crudo or len(crudo) > _MAX_TOKEN:
        return None
    partes = crudo.split(".")
    if len(partes) != 3:
        return None
    key_b64, exp_txt, firma = partes
    try:
        exp = int(exp_txt)
        key = _de_b64(key_b64).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    if exp <= (ahora if ahora is not None else time.time()):
        return None
    if not hmac.compare_digest(firma.encode(), _firma(key, exp).encode()):
        return None
    return key
