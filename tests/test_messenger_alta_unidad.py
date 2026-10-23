"""Alta de Messenger (ADR-0152): lo que no necesita BD ni red. El flujo completo y la API están en
`tests/integration/test_messenger_alta.py` y `test_api_meta_messenger.py`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.billing.plans import PLANS
from core.gateway.meta_messenger_alta import (
    PAGE_SUBSCRIBED_FIELDS,
    config_id_de_messenger,
    evaluar_pagina,
    faltan_para_messenger,
    plan_incluye_messenger,
)


# ------------------------------------------------------------------ plan (decisión del dueño, 2026-10-23)
@pytest.mark.parametrize("plan_id", ["basic", "premium", "empresarial"])
def test_todos_los_planes_de_pago_incluyen_messenger_e_instagram(plan_id):
    assert plan_incluye_messenger(plan_id)
    plan = next(p for p in PLANS if p.id == plan_id)
    assert {"whatsapp", "messenger", "instagram"} <= set(plan.features["canales"])


def test_el_plan_gratuito_no_incluye_messenger_ni_instagram():
    assert not plan_incluye_messenger("free")
    free = next(p for p in PLANS if p.id == "free")
    assert free.features["canales"] == ["whatsapp"]


@pytest.mark.parametrize("plan_id", [None, "", "no-existe"])
def test_sin_plan_valido_no_hay_messenger(plan_id):
    assert not plan_incluye_messenger(plan_id)


# ------------------------------------------------------------------ roles de la Página
@pytest.mark.parametrize(
    ("roles", "elegible"),
    [
        (["MESSAGING", "MANAGE"], True),
        (["MESSAGING", "MODERATE"], True),
        (["MESSAGING", "CREATE_CONTENT", "ANALYZE"], True),
        (["MANAGE", "MODERATE", "CREATE_CONTENT", "ADVERTISE", "ANALYZE", "MESSAGING"], True),  # administrador
        (["MANAGE", "MODERATE"], False),  # sin MESSAGING
        (["MESSAGING"], False),  # sin rol de administración
        (["ANALYZE", "ADVERTISE"], False),
        ([], False),
    ],
)
def test_hace_falta_mensajeria_y_un_rol_de_administracion(roles, elegible):
    ok, motivo = evaluar_pagina(roles)
    assert ok is elegible
    assert (motivo is None) is elegible
    if not elegible:
        assert "rol" in motivo and "Meta Business Suite" in motivo


# ------------------------------------------------------------------ configuración
def _settings(**kw):
    base = {"meta_app_id": "app", "meta_app_secret": "secreto", "meta_config_id": "", "meta_messenger_config_id": ""}
    return SimpleNamespace(**{**base, **kw})


def test_la_configuracion_propia_de_messenger_gana_sobre_la_de_whatsapp():
    s = _settings(meta_config_id="whatsapp", meta_messenger_config_id="messenger")
    assert config_id_de_messenger(s) == "messenger"
    assert faltan_para_messenger(s) == []


def test_sin_configuracion_propia_no_usa_la_de_whatsapp():
    # La de Embedded Signup abriría el registro de WhatsApp y no concede los permisos de Páginas.
    s = _settings(meta_config_id="whatsapp")
    assert config_id_de_messenger(s) == ""
    assert faltan_para_messenger(s) == ["META_MESSENGER_CONFIG_ID"]


def test_dice_exactamente_que_variables_faltan():
    assert faltan_para_messenger(_settings()) == ["META_MESSENGER_CONFIG_ID"]
    vacia = _settings(meta_app_id="", meta_app_secret="")
    assert faltan_para_messenger(vacia) == ["META_APP_ID", "META_APP_SECRET", "META_MESSENGER_CONFIG_ID"]


# ------------------------------------------------------------------ suscripción
def test_la_suscripcion_pide_los_campos_de_mensajeria():
    campos = set(PAGE_SUBSCRIBED_FIELDS.split(","))
    assert {"messages", "messaging_postbacks", "message_echoes", "message_deliveries"} <= campos
