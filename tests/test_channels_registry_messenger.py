"""El registro de adapters de salida quedó indexado por (provider, channel)
-- antes era solo por provider, y WhatsApp/Messenger compartían la clave
"meta", haciendo estructuralmente imposible tener dos adapters de salida
distintos (hallazgo documentado en el docstring de whatsapp/inbound.py)."""

from __future__ import annotations

import pytest

from core.gateway.channels.messenger.outbound import FacebookMessengerOutbound
from core.gateway.channels.registry import outbound_adapter_for
from core.gateway.channels.whatsapp.outbound import WhatsAppMetaOutbound


def test_whatsapp_y_messenger_devuelven_adapters_distintos():
    whatsapp = outbound_adapter_for("meta", "whatsapp")
    messenger = outbound_adapter_for("meta", "messenger")
    assert isinstance(whatsapp, WhatsAppMetaOutbound)
    assert isinstance(messenger, FacebookMessengerOutbound)
    assert whatsapp is not messenger


def test_channel_faltante_da_error_claro_no_keyerror_criptico():
    with pytest.raises(KeyError, match="instagram"):
        outbound_adapter_for("meta", "instagram")


def test_default_de_channel_sigue_siendo_whatsapp_por_retrocompatibilidad():
    assert isinstance(outbound_adapter_for("meta"), WhatsAppMetaOutbound)
