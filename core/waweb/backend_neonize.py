"""Socket real del canal `waweb` sobre Neonize (ADR-0074).

**Pieza validada contra la versión instalada de `neonize` (0.5.2).** El resto del
connector está probado con un backend falso; acá se mapean los eventos reales:

- `QREv` (`.Codes`)           → callback de QR
- `ConnectedEv` / `PairStatusEv` → `connected`
- `DisconnectedEv` (`.status`) → `disconnected` (+ cierra el socket)
- `LoggedOutEv` (`.Reason`)    → `logged_out` (+ cierra; el manager no reconecta)
- `TemporaryBanEv` (`.Code`)   → `banned` (+ alerta crítica; sí reconecta)
- `ConnectFailureEv`           → `error`
- `StreamReplacedEv`           → `disconnected` (+ cierra: el servidor la reemplazó)
- `MessageEv` (`Info.MessageSource`, `Info.ID`, `Message`) → `InboundEvent`

Además, `alive()` consulta `is_connected` / `is_logged_in` del cliente: el
auto-reconnect interno de whatsmeow puede fallar sin emitir ningún evento
(visto en test: `Error sending close to websocket ... EOF` y luego silencio) y
el manager necesita una señal que no dependa de los eventos.

No importa `neonize` a nivel de módulo: si no está instalado, falla con un
mensaje claro al conectar.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from core.shared.logger import get_logger
from core.waweb.backend import InboundEvent, MessageHandler, QrHandler, StatusHandler

log = get_logger(__name__)

# Neonize (el puente Go de fondo) tiene una condición de carrera real: crear o
# conectar dos clientes a la vez revienta el proceso con `fatal error: concurrent
# map writes` (visto en vivo con dos negocios con sesión en el mismo conector).
# Un lock global serializa TODAS las llamadas al puente Go -- el throughput de
# vinculación no importa, la estabilidad sí.
_BRIDGE_LOCK = asyncio.Lock()


def _import_neonize():  # type: ignore[no-untyped-def]
    try:
        from neonize.aioze import events as ev
        from neonize.aioze.client import NewAClient
    except Exception as exc:  # pragma: no cover - depende del entorno del connector
        raise RuntimeError(
            "neonize no está instalado. El rol `connector` lo requiere: "
            "`pip install neonize` (el contenedor de despliegue ya lo trae)."
        ) from exc
    return NewAClient, ev


async def _resolve(value: object) -> object:
    """Valor de una propiedad de Neonize que puede ser corutina o valor directo."""
    return await value if inspect.isawaitable(value) else value


def _message_text(message: object) -> str:
    """Texto de un mensaje entrante (conversation o extendedTextMessage.text)."""
    conversation = getattr(message, "conversation", None)
    if isinstance(conversation, str) and conversation:
        return conversation
    extended = getattr(message, "extendedTextMessage", None)
    text = getattr(extended, "text", None)
    return text if isinstance(text, str) else ""


def _jid_number(jid: object) -> str:
    """Número (solo dígitos) de un JID proto de Neonize.

    `source.Sender` es un proto `JID`, NO una cadena: hay que leer `.User`
    (hacer `str(proto)` devolvía `User: "..."` y producía remitentes basura).
    """
    user = getattr(jid, "User", None)
    if isinstance(user, str) and user:
        return user.split(":", 1)[0].lstrip("+")
    raw = str(jid or "")
    return raw.split("@", 1)[0].split(":", 1)[0].lstrip("+")


class NeonizeSocket:
    """Implementa `WaWebSocket` (core/waweb/backend.py) sobre Neonize."""

    # Tope para `alive()`: esperar el lock del puente más que esto (otro tenant
    # ocupándolo) no es evidencia de socket muerto.
    alive_timeout_s: float = 10.0
    # `disconnect()` no puede esperar el lock para siempre: si otra llamada al
    # puente quedó trabada (deadlock de Neonize), este tenant nunca salía del
    # `finally` y el panel se quedaba sin QR (visto en test 2026-10-28).
    disconnect_lock_timeout_s: float = 10.0

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self._client = None  # type: ignore[assignment]
        self._idle_task: asyncio.Task | None = None

    async def connect(
        self,
        *,
        session_dir: Path,
        on_qr: QrHandler,
        on_status: StatusHandler,
        on_message: MessageHandler,
    ) -> None:
        NewAClient, ev = _import_neonize()
        from neonize.utils.jid import jid_is_lid

        session_dir.mkdir(parents=True, exist_ok=True)
        # Todo lo que toca el puente Go va serializado (ver `_BRIDGE_LOCK`).
        async with _BRIDGE_LOCK:
            client = NewAClient(str(session_dir / "session.db"))
        self._client = client

        async def _qr(_c: object, data_qr: object) -> None:
            # Neonize entrega el QR como bytes por el callback dedicado
            # (`client.event.qr`), no por el evento `QREv`.
            code = data_qr.decode(errors="ignore") if isinstance(data_qr, (bytes, bytearray)) else str(data_qr)
            if code:
                await on_qr(code)

        async def _connected(_c: object, _event: object) -> None:
            await on_status("connected", None, None)

        async def _pair(_c: object, event: object) -> None:
            error = str(getattr(event, "Error", "") or "")
            jid = str(getattr(event, "ID", "") or "") or None
            await on_status(
                "error" if error else "connected",
                error[:200] or None,
                None if error else jid,
            )

        async def _disconnected(_c: object, event: object) -> None:
            detail = str(getattr(event, "status", "") or "")[:200] or None
            await on_status("disconnected", detail, None)
            self.closed.set()

        async def _logged_out(_c: object, event: object) -> None:
            reason = str(getattr(event, "Reason", "") or "")[:200] or "logged_out"
            await on_status("logged_out", reason, None)
            self.closed.set()

        async def _banned(_c: object, event: object) -> None:
            detail = f"ban temporal code={getattr(event, 'Code', '')} expira {getattr(event, 'Expire', '')}"
            await on_status("banned", detail[:200], None)
            self.closed.set()

        async def _failure(_c: object, event: object) -> None:
            detail = str(getattr(event, "Message", "") or getattr(event, "Reason", "") or "")
            await on_status("error", detail[:200] or None, None)
            self.closed.set()

        async def _stream_replaced(_c: object, _event: object) -> None:
            # Otra conexión con la misma sesión ganó el stream: este socket
            # queda sordo (ni recibe ni envía). Si no se cierra, el manager
            # cree que la sesión está viva y se pierden mensajes en silencio
            # (visto en vivo: "Got replaced stream error, sending
            # StreamReplaced event" tras el auto-reconnect interno de Neonize).
            log.warning("waweb_stream_replaced")
            await on_status("disconnected", "conexión reemplazada por otra sesión", None)
            self.closed.set()

        async def _message(_c: object, event: object) -> None:
            info = getattr(event, "Info", None)
            source = getattr(info, "MessageSource", None)
            if bool(getattr(source, "IsFromMe", False)) or bool(getattr(source, "IsGroup", False)):
                return
            sender_jid = getattr(source, "Sender", None)
            # Si el remitente es un LID, el número real (PN) viene en SenderAlt.
            alt = getattr(source, "SenderAlt", None)
            if sender_jid is not None and jid_is_lid(sender_jid) and _jid_number(alt):
                sender_jid = alt
            message = getattr(event, "Message", None)
            text = _message_text(message) if message is not None else ""
            wamid = str(getattr(info, "ID", "") or "")
            sender = _jid_number(sender_jid)
            if not wamid or not text or not sender:
                # Sin remitente resoluble no se publica: mejor no crear un
                # customer basura ni responder a un destino inválido.
                log.warning("waweb_inbound_sin_remitente", wamid=wamid)
                return
            name = str(getattr(info, "Pushname", "") or "") or None
            await on_message(InboundEvent(sender=sender, text=text, wamid=wamid, customer_name=name))

        handlers = {
            "ConnectedEv": _connected,
            "PairStatusEv": _pair,
            "DisconnectedEv": _disconnected,
            "LoggedOutEv": _logged_out,
            "TemporaryBanEv": _banned,
            "ConnectFailureEv": _failure,
            "StreamReplacedEv": _stream_replaced,
            "MessageEv": _message,
        }
        async with _BRIDGE_LOCK:
            for name, handler in handlers.items():
                event_type = getattr(ev, name, None)
                if event_type is not None and hasattr(client, "event"):
                    client.event(event_type)(handler)
                else:
                    log.warning("waweb_neonize_event_ausente", event_name=name)

            # El QR va por su propio callback (`event.qr`), no por `EVENT_TO_INT`.
            client.event.qr(_qr)

            await on_status("connecting", None)
            await client.connect()
            # `idle()` mantiene el socket vivo en segundo plano.
            self._idle_task = asyncio.create_task(client.idle())

    async def send(self, *, to: str, text: str, image_url: str | None = None) -> str:
        if self._client is None:
            raise RuntimeError("el socket waweb no está conectado")
        jid = to if "@" in to else f"{to}@s.whatsapp.net"
        # Media nativa (imagen) entra en F3; por ahora la URL va en el texto.
        body = text if not image_url else f"{text}\n{image_url}".strip()
        from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

        async with _BRIDGE_LOCK:
            sent = await self._client.send_message(jid, Message(conversation=body))
        return str(getattr(sent, "ID", "") or "")

    async def alive(self) -> bool:
        if self._client is None:
            return False
        if self._idle_task is not None and self._idle_task.done():
            # `idle()` espera la tarea de conexión de Neonize: si terminó, el
            # puente Go ya no mantiene el socket.
            return False
        # El tope cubre SOLO la espera del lock: cancelar una llamada al puente
        # ya en curso soltaría el lock con el hilo todavía dentro de Go, y el
        # próximo en entrar reproduciría el `concurrent map writes`.
        try:
            await asyncio.wait_for(_BRIDGE_LOCK.acquire(), self.alive_timeout_s)
        except TimeoutError:
            # Puente ocupado (otro tenant enviando o conectando): no es evidencia.
            log.warning("waweb_neonize_alive_timeout")
            return True
        try:
            # En `neonize.aioze` estas propiedades devuelven una corutina
            # (`asyncio.to_thread` sobre el puente Go), no un bool: evaluarlas
            # sin `await` daría siempre True.
            connected = await _resolve(self._client.is_connected)
            logged_in = await _resolve(self._client.is_logged_in)
        except Exception:
            log.warning("waweb_neonize_alive_failed", exc_info=True)
            return True
        finally:
            _BRIDGE_LOCK.release()
        return bool(connected) and bool(logged_in)

    async def disconnect(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        if self._client is not None:
            try:
                # Solo se acota la ESPERA del lock: cancelar la llamada ya en
                # curso liberaría el lock con el hilo todavía dentro de Go
                # (`concurrent map writes`). Si el lock no llega, el watchdog de
                # progreso del manager reinicia el proceso.
                await asyncio.wait_for(_BRIDGE_LOCK.acquire(), self.disconnect_lock_timeout_s)
            except TimeoutError:
                log.warning("waweb_neonize_disconnect_lock_timeout")
                self.closed.set()
                return
            try:
                await self._client.disconnect()
            except Exception:
                log.warning("waweb_neonize_disconnect_failed")
            finally:
                _BRIDGE_LOCK.release()
        self.closed.set()
