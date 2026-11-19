"""El motivo de Meta (`error_user_title`/`error_user_msg`) llega al panel y al registro.

"Error de Meta: Invalid parameter" (código 100) sola no dice qué falló al agregar un número;
el motivo sí (p. ej. que el número ya está en otra cuenta o que el nombre no se permite).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest

import core.admin.meta_numbers_service as service
from core.gateway.meta_errors import GraphError, classify_and_build_error
from core.gateway.meta_graph import _error_message_for_log

TENANT = uuid.uuid4()
CONNECTION = uuid.uuid4()


def _invalid_parameter(title: str | None = None, msg: str | None = None) -> GraphError:
    error = {"message": "Invalid parameter", "type": "OAuthException", "code": 100}
    if title is not None:
        error["error_user_title"] = title
    if msg is not None:
        error["error_user_msg"] = msg
    return classify_and_build_error(http_status=400, body={"error": error}, headers=httpx.Headers())


def test_user_text_con_titulo_y_mensaje():
    exc = _invalid_parameter("Número ya registrado", "El número ya está en otra cuenta de WhatsApp Business.")
    assert exc.user_text == "Número ya registrado: El número ya está en otra cuenta de WhatsApp Business."


def test_user_text_con_solo_uno_de_los_dos():
    assert _invalid_parameter(msg="Nombre no permitido.").user_text == "Nombre no permitido."
    assert _invalid_parameter(title="Nombre no permitido").user_text == "Nombre no permitido"


def test_user_text_sin_motivo_es_el_mensaje():
    exc = _invalid_parameter()
    assert exc.user_text == exc.message == "Error de Meta: Invalid parameter"


def test_el_registro_de_la_llamada_guarda_el_motivo():
    assert _error_message_for_log(None) is None
    sin_motivo = _invalid_parameter()
    assert _error_message_for_log(sin_motivo) == "Error de Meta: Invalid parameter"
    con_motivo = _invalid_parameter("Título", "Detalle")
    assert _error_message_for_log(con_motivo) == "Error de Meta: Invalid parameter | Título: Detalle"


@pytest.mark.asyncio
async def test_agregar_numero_devuelve_el_motivo_de_meta(monkeypatch):
    connection = SimpleNamespace(id=CONNECTION, tenant_id=TENANT)
    waba = SimpleNamespace(waba_id="waba-1")
    creds = SimpleNamespace(access_token="tok", app_secret="sec", graph_api_version="v21.0")

    async def fake_resolve(cid, tenant_id):
        return connection, waba, creds

    async def fake_add(*_args, **_kwargs):
        raise _invalid_parameter(msg="El nombre visible no cumple las reglas de Meta.")

    monkeypatch.setattr(service, "resolve_waba_and_credentials", fake_resolve)
    monkeypatch.setattr("core.gateway.meta_phone_numbers.add_phone_number", fake_add)

    error = await service.add_number(
        TENANT, str(CONNECTION), cc="57", phone_number="3001234567", verified_name="Business Wap"
    )

    assert error == "El nombre visible no cumple las reglas de Meta."
