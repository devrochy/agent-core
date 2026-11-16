"""Canal Telegram (`telegram`) con bot propio por negocio — cliente de la Bot API (ADR-0199).

El registry del core (`agent_core.channels`) carga este módulo y lee `PROVIDER`, `InboundAdapter` y
`OutboundAdapter`. Los adaptadores llegan en los PR core 3 y 4; hasta entonces solo hay `PROVIDER` y el registry no
registra nada para Telegram.

Credenciales de la conexión (las arma el alta y las guarda cifradas en `channel_connections`):

- `bot_token`: `<bot_id>:<secreto>` de @BotFather. Va en la ruta de cada llamada a la Bot API.
- `webhook_secret`: aleatorio por conexión; Telegram lo manda en `X-Telegram-Bot-Api-Secret-Token`.
"""

from __future__ import annotations

PROVIDER = "telegram"

__all__ = ["PROVIDER"]
