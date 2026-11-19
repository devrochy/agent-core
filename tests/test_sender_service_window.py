"""`validate_service_window`: mensaje libre fuera de ventana falla local
sin llamar a Meta; plantilla fuera de ventana no falla; sin ventana seteada
(conversación nueva, nunca hubo inbound) no bloquea el primer envío."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.gateway.sender import validate_service_window
from core.shared.exceptions import OutboundWindowExpiredError


def _conversation(expires_at, channel="whatsapp"):
    return SimpleNamespace(service_window_expires_at=expires_at, channel=channel)


def test_mensaje_libre_fuera_de_ventana_falla_local():
    expired = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(OutboundWindowExpiredError, match="24h"):
        validate_service_window(_conversation(expired), is_template=False)


def test_mensaje_libre_dentro_de_ventana_pasa():
    valid = datetime.now(UTC) + timedelta(hours=1)
    validate_service_window(_conversation(valid), is_template=False)


def test_plantilla_fuera_de_ventana_no_falla():
    expired = datetime.now(UTC) - timedelta(hours=1)
    validate_service_window(_conversation(expired), is_template=True)


def test_sin_ventana_seteada_no_bloquea():
    validate_service_window(_conversation(None), is_template=False)


# ------------------------------------------------------------------ Messenger e Instagram (ADR-0152)
@pytest.mark.parametrize("channel", ["messenger", "instagram"])
def test_el_asistente_no_escribe_fuera_de_la_ventana_de_messenger(channel):
    expired = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(OutboundWindowExpiredError, match="asistente no puede escribir"):
        validate_service_window(_conversation(expired, channel), is_template=False, sender_type="agent")


@pytest.mark.parametrize("channel", ["messenger", "instagram"])
def test_una_persona_responde_hasta_7_dias_despues_del_ultimo_mensaje(channel):
    # La ventana venció hace 3 días: el último mensaje del cliente fue hace 4 (dentro de los 7).
    expired = datetime.now(UTC) - timedelta(days=3)
    validate_service_window(_conversation(expired, channel), is_template=False, sender_type="human")


@pytest.mark.parametrize("channel", ["messenger", "instagram"])
def test_pasados_7_dias_ni_una_persona_puede_responder(channel):
    expired = datetime.now(UTC) - timedelta(days=7)  # el último mensaje fue hace 8 días
    with pytest.raises(OutboundWindowExpiredError, match="7 días"):
        validate_service_window(_conversation(expired, channel), is_template=False, sender_type="human")


def test_whatsapp_no_cambia_ni_para_una_persona():
    expired = datetime.now(UTC) - timedelta(hours=1)
    with pytest.raises(OutboundWindowExpiredError, match="24h"):
        validate_service_window(_conversation(expired), is_template=False, sender_type="human")
