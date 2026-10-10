"""Interfaz del socket de `waweb` (ADR-0074).

El `manager` no conoce Neonize: habla con este protocolo. `NeonizeSocket`
(`core/waweb/backend_neonize.py`) es la implementación real; los tests usan un
fake. Este es el punto exacto que valida el spike de F0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# Callbacks que el socket invoca hacia el manager.
QrHandler = Callable[[str], Awaitable[None]]
StatusHandler = Callable[[str, "str | None", "str | None"], Awaitable[None]]
MessageHandler = Callable[["InboundEvent"], Awaitable[None]]


@dataclass
class InboundEvent:
    """Mensaje entrante normalizado desde el socket."""

    sender: str  # solo dígitos, sin @s.whatsapp.net ni +
    text: str
    wamid: str
    customer_name: str | None = None
    media: list[dict[str, str]] = field(default_factory=list)


class WaWebSocket(Protocol):
    """Socket de WhatsApp Web: conectar, enviar, escuchar, cerrar."""

    closed: asyncio.Event

    async def connect(
        self,
        *,
        session_dir: Path,
        on_qr: QrHandler,
        on_status: StatusHandler,
        on_message: MessageHandler,
    ) -> None:
        """Conecta usando la sesión de `session_dir` (o pide QR si está vacía)."""
        ...

    async def send(self, *, to: str, text: str, image_url: str | None = None) -> str:
        """Envía un texto (o imagen con caption) y devuelve el wamid."""
        ...

    async def alive(self) -> bool:
        """True si el socket sigue conectado y autenticado según el backend.

        Chequeo de vida independiente de los eventos: si el auto-reconnect
        interno falla sin emitir `DisconnectedEv`, `closed` nunca se marca y el
        socket queda sordo. Ante la duda (backend ocupado o sin respuesta)
        devuelve True: solo un False es evidencia de socket muerto.
        """
        ...

    async def disconnect(self) -> None: ...
