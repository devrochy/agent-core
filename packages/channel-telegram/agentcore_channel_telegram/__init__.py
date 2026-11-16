"""Canal Telegram (`telegram`) con bot propio por negocio — cliente de la Bot API (ADR-0199).

El registry del core (`agent_core.channels`) carga este módulo y lee `PROVIDER`, `InboundAdapter` y
`OutboundAdapter`. El de entrada llega en el PR core 3; el de salida, en el PR core 4.

Credenciales de la conexión (las arma el alta y las guarda cifradas en `channel_connections`):

- `bot_token`: `<bot_id>:<secreto>` de @BotFather. Va en la ruta de cada llamada a la Bot API.
- `webhook_secret`: aleatorio por conexión; Telegram lo manda en `X-Telegram-Bot-Api-Secret-Token`.
"""

from __future__ import annotations

from agentcore_channel_telegram.adapters import PROVIDER, TelegramInbound

InboundAdapter = TelegramInbound

__all__ = ["PROVIDER", "InboundAdapter", "TelegramInbound"]
