"""Cifrado simétrico de secretos en BD (Fernet).

Todo valor sensible persistido (API keys de plugins, credenciales de canales,
configs con SecretStr) pasa por aquí. La clave maestra vive SOLO en el .env.

Genera una clave: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet

from core.config import get_settings


class SecretBox:
    def __init__(self, key: str | None = None) -> None:
        raw = key or get_settings().master_encryption_key
        if not raw:
            raise RuntimeError(
                "MASTER_ENCRYPTION_KEY no configurada. "
                "Genera una con: python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"'
            )
        self._fernet = Fernet(raw.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()

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
