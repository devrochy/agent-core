"""Validación de MetaAppConfig (equivalente Pydantic al Zod del pedido original)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.gateway.meta_app_config import MetaAppConfig


def test_acepta_config_valida():
    config = MetaAppConfig(app_id="123456789", app_secret="s3cr3t", config_id="987654321")
    assert config.app_id == "123456789"


@pytest.mark.parametrize("app_id", ["", "abc", "12-34", "12 34"])
def test_rechaza_app_id_no_numerico(app_id):
    with pytest.raises(ValidationError):
        MetaAppConfig(app_id=app_id, app_secret="s3cr3t", config_id="987654321")


@pytest.mark.parametrize("config_id", ["", "abc", "12-34"])
def test_rechaza_config_id_no_numerico(config_id):
    with pytest.raises(ValidationError):
        MetaAppConfig(app_id="123456789", app_secret="s3cr3t", config_id=config_id)


def test_rechaza_app_secret_vacio():
    with pytest.raises(ValidationError):
        MetaAppConfig(app_id="123456789", app_secret="", config_id="987654321")
