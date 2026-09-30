"""AES-256-GCM (core/shared/crypto.py): rotación de claves, AAD, retrocompatibilidad
con el formato Fernet legado. Equivalente a los tests pgTAP que pedía el ticket
original, adaptados a un módulo Python puro (sin BD)."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.fernet import Fernet

from core.config import get_settings
from core.shared import crypto


def test_seal_unseal_roundtrip():
    ciphertext = crypto.seal("hola secreto")
    assert ciphertext.startswith("gcm:v1:")
    assert crypto.unseal(ciphertext) == "hola secreto"


def test_aad_ata_el_ciphertext_a_la_fila():
    """Ticket: mover una fila de tenant A a B (AAD distinto) debe fallar la
    autenticación, no devolver datos de otro tenant."""
    ciphertext = crypto.seal("token del tenant A", aad=b"tenantA:conn1:app_access")
    with pytest.raises(RuntimeError):
        crypto.unseal(ciphertext, aad=b"tenantB:conn1:app_access")
    # con el AAD correcto, sigue andando
    assert crypto.unseal(ciphertext, aad=b"tenantA:conn1:app_access") == "token del tenant A"


def test_seal_parts_y_unseal_parts_con_aad():
    ciphertext, iv, auth_tag, key_id = crypto.seal_parts(b"bytes secretos", aad=b"aad-x")
    assert crypto.unseal_parts(ciphertext, iv, auth_tag, key_id, aad=b"aad-x") == b"bytes secretos"
    with pytest.raises(RuntimeError):
        crypto.unseal_parts(ciphertext, iv, auth_tag, key_id, aad=b"aad-otro")


def test_key_id_desconocido_al_descifrar_no_hace_fallback_silencioso():
    ciphertext = crypto.seal("dato")
    tampered = ciphertext.replace(":k1:", ":no-existe:")
    with pytest.raises(crypto.UnknownKeyIdError):
        crypto.unseal(tampered)


def test_key_id_desconocido_al_cifrar(monkeypatch):
    monkeypatch.setattr(get_settings(), "token_active_key_id", "no-existe")
    with pytest.raises(crypto.UnknownKeyIdError):
        crypto.seal("dato")


def test_sin_active_key_id_no_puede_cifrar(monkeypatch):
    monkeypatch.setattr(get_settings(), "token_active_key_id", "")
    with pytest.raises(RuntimeError):
        crypto.seal("dato")


def test_valor_fernet_legado_se_sigue_leyendo():
    legacy_key = get_settings().master_encryption_key
    legacy_token = Fernet(legacy_key.encode()).encrypt(b"valor viejo").decode()
    assert not legacy_token.startswith("gcm:v1:")
    assert crypto.unseal(legacy_token) == "valor viejo"


def test_secret_box_api_estable_usa_formato_nuevo_al_escribir():
    box = crypto.get_secret_box()
    encrypted = box.encrypt_json({"a": 1, "b": "texto"})
    assert encrypted.startswith("gcm:v1:")
    assert box.decrypt_json(encrypted) == {"a": 1, "b": "texto"}


def test_secret_box_sigue_leyendo_fernet_legado():
    legacy_key = get_settings().master_encryption_key
    legacy_token = Fernet(legacy_key.encode()).encrypt(json.dumps({"x": 1}).encode()).decode()
    assert crypto.get_secret_box().decrypt_json(legacy_token) == {"x": 1}


def test_token_fingerprint_es_deterministico():
    assert crypto.token_fingerprint("mi-token") == crypto.token_fingerprint("mi-token")
    assert crypto.token_fingerprint("mi-token") != crypto.token_fingerprint("otro-token")


def test_token_master_keys_json_invalido(monkeypatch):
    monkeypatch.setattr(get_settings(), "token_master_keys", "no es json")
    with pytest.raises(RuntimeError):
        crypto.seal("dato")


def test_sin_master_encryption_key_no_puede_leer_legado(monkeypatch):
    monkeypatch.setattr(get_settings(), "master_encryption_key", "")
    legacy_token = Fernet(base64.urlsafe_b64encode(b"0" * 32)).encrypt(b"x").decode()
    with pytest.raises(RuntimeError):
        crypto.unseal(legacy_token)
