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


@dataclass
class DeviceIdentity:
    """Datos del dispositivo vinculado, tal como los entrega WhatsApp.

    `jid` es la identidad completa (<numero>@s.whatsapp.net); `phone` son solo
    dígitos. `push_name` es el nombre de perfil y `business_name` el del negocio
    (WhatsApp Business). `platform` es el tipo de dispositivo (android, iphone,
    web…). Cualquiera puede faltar según el evento y la versión de WhatsApp.
    """

    jid: str | None = None
    phone: str | None = None
    push_name: str | None = None
    business_name: str | None = None
    platform: str | None = None


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

    async def identity(self) -> DeviceIdentity | None:
        """Identidad del dispositivo vinculado, si el backend ya la conoce.

        No debe hacer llamadas de red ni al puente Go: es una lectura del cache
        que el socket llenó con `PairStatusEv`/`Device`.
        """
        ...

    async def disconnect(self) -> None: ...
