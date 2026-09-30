"""`validate_service_window`: mensaje libre fuera de ventana falla local
sin llamar a Meta; plantilla fuera de ventana no falla; sin ventana seteada
(conversación nueva, nunca hubo inbound) no bloquea el primer envío."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core.gateway.sender import validate_service_window
from core.shared.exceptions import OutboundWindowExpiredError


def _conversation(expires_at):
    return SimpleNamespace(service_window_expires_at=expires_at)


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
