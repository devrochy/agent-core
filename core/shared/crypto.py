"""Cifrado simétrico de secretos en BD.

Formato NUEVO (todo cifrado que se escribe hoy): AES-256-GCM con rotación de
claves por `key_id`, más AAD opcional para atar el ciphertext a una fila
concreta (tenant/conexión/tipo de token -- ver `core/gateway/meta_token_store.py`).

Formato LEGADO (solo lectura): Fernet, una sola clave (`MASTER_ENCRYPTION_KEY`).
`unseal()`/`SecretBox.decrypt()` siguen pudiendo leerlo -- retrocompatibilidad
con lo que ya está cifrado en `channel_connections`, `plugin_configs`, etc. Sin
esto, migrar el esquema de cifrado exigiría un backfill masivo bloqueante;
en cambio, cualquier fila se re-cifra sola (formato nuevo) la próxima vez que
se guarda por el flujo normal (`save_connection`, `save_config`, ...).

Serialización del formato nuevo (un string, mismo tipo TEXT que ya usan las
columnas `*_encrypted` -- cero cambio de columna):
    "gcm:v1:<key_id>:<iv_b64>:<ciphertext_b64>"
(el auth tag de GCM viaja concatenado al ciphertext -- comportamiento estándar
de `AESGCM.encrypt()`, no hace falta separarlo a mano).

Genera una clave nueva:
    python -c "import os, base64; print(base64.b64encode(os.urandom(32)).decode())"
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import get_settings

_FORMAT_PREFIX = "gcm:v1:"


class UnknownKeyIdError(RuntimeError):
    """El `key_id` embebido en el ciphertext no está en TOKEN_MASTER_KEYS.

    Nunca se cae a probar con otra clave -- eso ocultaría una rotación de
    claves mal hecha (clave vieja borrada del `.env` mientras aún había datos
    cifrados con ella) detrás de un error de descifrado genérico."""


def _load_keys() -> dict[str, bytes]:
    settings = get_settings()
    try:
        raw = json.loads(settings.token_master_keys or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("TOKEN_MASTER_KEYS no es JSON válido") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("TOKEN_MASTER_KEYS debe ser un objeto {key_id: clave_base64}")
    return {str(key_id): base64.b64decode(value) for key_id, value in raw.items()}


def _active_key() -> tuple[str, bytes]:
    settings = get_settings()
    key_id = settings.token_active_key_id
    if not key_id:
        raise RuntimeError("TOKEN_ACTIVE_KEY_ID no configurada: no se puede cifrar contenido nuevo")
    keys = _load_keys()
    if key_id not in keys:
        raise UnknownKeyIdError(f"unknown key_id: {key_id!r} (no está en TOKEN_MASTER_KEYS)")
    return key_id, keys[key_id]


def _key_for(key_id: str) -> bytes:
    keys = _load_keys()
    if key_id not in keys:
        raise UnknownKeyIdError(f"unknown key_id: {key_id!r} (no está en TOKEN_MASTER_KEYS)")
    return keys[key_id]


def seal_parts(plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes, bytes, str]:
    """Cifra y devuelve (ciphertext, iv, auth_tag, key_id) por separado -- para
    columnas `bytea` propias (`meta_private.meta_tokens`). `seal()` (más abajo)
    es un wrapper de conveniencia sobre esto para columnas TEXT únicas."""
    key_id, key = _active_key()
    iv = os.urandom(12)
    combined = AESGCM(key).encrypt(iv, plaintext, aad or None)
    ciphertext, auth_tag = combined[:-16], combined[-16:]
    return ciphertext, iv, auth_tag, key_id


def unseal_parts(ciphertext: bytes, iv: bytes, auth_tag: bytes, key_id: str, aad: bytes = b"") -> bytes:
    """Inverso de `seal_parts()`."""
    key = _key_for(key_id)
    try:
        return AESGCM(key).decrypt(iv, ciphertext + auth_tag, aad or None)
    except InvalidTag as exc:
        raise RuntimeError("Autenticación fallida al descifrar (AAD o clave incorrectos)") from exc


def seal(plaintext: str, aad: bytes = b"") -> str:
    """Cifra con AES-256-GCM usando la clave activa (`TOKEN_ACTIVE_KEY_ID`).

    Serializa todo en un solo string (columnas TEXT existentes); para columnas
    `bytea` separadas usar `seal_parts()`."""
    ciphertext, iv, auth_tag, key_id = seal_parts(plaintext.encode(), aad)
    return (
        f"{_FORMAT_PREFIX}{key_id}:"
        f"{base64.b64encode(iv).decode()}:"
        f"{base64.b64encode(ciphertext + auth_tag).decode()}"
    )


def unseal(value: str, aad: bytes = b"") -> str:
    """Descifra. Detecta formato nuevo (AES-256-GCM) vs legado (Fernet) por prefijo."""
    if not value.startswith(_FORMAT_PREFIX):
        return _unseal_legacy_fernet(value)

    try:
        key_id, iv_b64, ciphertext_b64 = value[len(_FORMAT_PREFIX) :].split(":", 2)
    except ValueError as exc:
        raise RuntimeError("Formato de cifrado gcm:v1 inválido") from exc

    combined = base64.b64decode(ciphertext_b64)
    ciphertext, auth_tag = combined[:-16], combined[-16:]
    plaintext = unseal_parts(ciphertext, base64.b64decode(iv_b64), auth_tag, key_id, aad)
    return plaintext.decode()


def _unseal_legacy_fernet(value: str) -> str:
    raw_key = get_settings().master_encryption_key
    if not raw_key:
        raise RuntimeError(
            "MASTER_ENCRYPTION_KEY no configurada (necesaria para leer datos en formato legado Fernet)"
        )
    try:
        return Fernet(raw_key.encode()).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("No se pudo descifrar (formato legado Fernet: clave o dato inválido)") from exc


def token_fingerprint(token: str) -> str:
    """sha256 hex -- para buscar/loguear/deduplicar sin descifrar el valor real."""
    return hashlib.sha256(token.encode()).hexdigest()


class SecretBox:
    """API estable usada por todo el repo (`channel_connections`, `plugin_configs`,
    etc.) -- por dentro ya usa `seal()`/`unseal()` (AES-256-GCM + fallback legado
    Fernet). Ningún call-site existente necesita cambiar."""

    def __init__(self, key: str | None = None) -> None:
        # `key` es un parámetro legado (nadie en el repo instancia SecretBox(key)
        # hoy, pero no se rompe la firma): si se pasa, se usa como override de
        # MASTER_ENCRYPTION_KEY solo para el camino de lectura Fernet legado.
        self._legacy_key_override = key

    def encrypt(self, plaintext: str) -> str:
        return seal(plaintext, aad=b"")

    def decrypt(self, ciphertext: str) -> str:
        if self._legacy_key_override and not ciphertext.startswith(_FORMAT_PREFIX):
            try:
                return Fernet(self._legacy_key_override.encode()).decrypt(ciphertext.encode()).decode()
            except InvalidToken as exc:
                raise RuntimeError("No se pudo descifrar (formato legado Fernet: clave o dato inválido)") from exc
        return unseal(ciphertext, aad=b"")

    def encrypt_json(self, data: dict[str, Any]) -> str:
        return self.encrypt(json.dumps(data, ensure_ascii=False))

    def decrypt_json(self, ciphertext: str) -> dict[str, Any]:
        result = json.loads(self.decrypt(ciphertext))
        if not isinstance(result, dict):
            raise ValueError("El contenido descifrado no es un objeto JSON")
        return result


_box: SecretBox | None = None


def get_secret_box() -> SecretBox:
    global _box
    if _box is None:
        _box = SecretBox()
    return _box
