"""Manager del proceso `connector` del canal `waweb` (ADR-0074).

Mantiene un socket por sesión activa, conecta los callbacks del socket con el
inbound del core y con el drenaje de salida (con tope/pausa), persiste el estado
y reconecta con backoff. No conoce Neonize: habla con `WaWebSocket`.

Diseño para test: todas las dependencias son inyectables (registro, store,
factoría de socket, drainer, sleep, alert) — se puede correr entero con fakes.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from core.shared.alerts import send_alert
from core.shared.logger import get_logger
from core.waweb import handoff, limits, status
from core.waweb import inbound as inbound_mod
from core.waweb.backend import InboundEvent, WaWebSocket
from core.waweb.outbound import drain_once, purge_outbox
from core.waweb.policy import WaWebPolicy, load_policy
from core.waweb.registry import SessionContext, SessionRegistry
from core.waweb.store import SessionStore

log = get_logger(__name__)

_STATUS_MAP: dict[str, str] = {
    "connected": status.CONNECTED,
    "disconnected": status.DISCONNECTED,
    "logged_out": status.LOGGED_OUT,
    "banned": status.ERROR,  # ban temporal: se alerta pero se reconecta
    "error": status.ERROR,
}


@dataclass
class WaWebConnector:
    registry: SessionRegistry
    store: SessionStore
    socket_factory: Callable[[], WaWebSocket]
    policy_loader: Callable[[uuid.UUID], Awaitable[WaWebPolicy]] = load_policy
    drainer: Callable[..., Awaitable[bool]] = drain_once
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    alert: Callable[..., Awaitable[None]] = send_alert
    sweep_interval_s: int = 15
    reconnect_max_s: int = 60
    # Watchdog de vinculación: si un socket no está conectado y no publica QR en
    # este tiempo, se recicla (Neonize puede dejar de emitir QR tras un timeout
    # de login SIN marcar el socket como cerrado → quedaba en "esperando QR").
    pairing_timeout_s: float = 45.0
    # Si `connect()` no termina en este tiempo el socket se recicla: un `await`
    # eterno en la conexión congelaba TODO el connector (ningún tenant más
    # arrancaba sesión y el watchdog ni llegaba a correr).
    connect_timeout_s: float = 90.0
    # Tras N reciclados sin QR se limpia la sesión guardada: Neonize deja de
    # emitir QR después de un timeout de login y reciclar sin limpiar no lo
    # recupera (bug real visto en producción: bucle infinito de watchdog).
    pairing_reset_after: int = 3
    now: Callable[[], float] = time.monotonic
    _tasks: dict[uuid.UUID, asyncio.Task] = field(default_factory=dict, init=False)
    _stop: set[uuid.UUID] = field(default_factory=set, init=False)
    _sockets: dict[uuid.UUID, WaWebSocket] = field(default_factory=dict, init=False)
    _qr_at: dict[uuid.UUID, float] = field(default_factory=dict, init=False)
    _connected: set[uuid.UUID] = field(default_factory=set, init=False)
    _pairing_trips: dict[uuid.UUID, int] = field(default_factory=dict, init=False)
    # Negocios cuyo socket ACTUAL ya emitió al menos un QR. El watchdog solo
    # recicla si NO hubo ningún QR en este ciclo: antes reciclaba también las
    # sesiones sanas "esperando escaneo" (el QR tiene >45 s sin escanear), lo que
    # reiniciaba el vínculo cada ~45 s y castigaba la conexión con WhatsApp.
    _qr_cycles: set[uuid.UUID] = field(default_factory=set, init=False)

    async def run(self) -> None:
        """Bucle principal: barre sesiones y arranca/detiene sockets."""
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("waweb_sweep_failed")
            await self.sleep(self.sweep_interval_s)

    async def sweep(self) -> None:
        contexts = await self.registry.active_sessions()
        wanted = {c.tenant_id for c in contexts}
        for tenant_id in list(self._tasks):
            task = self._tasks[tenant_id]
            if tenant_id not in wanted or task.done():
                task.cancel()
                sock = self._sockets.pop(tenant_id, None)
                if sock is not None:
                    try:
                        await sock.disconnect()
                    except Exception:
                        log.warning("waweb_sweep_disconnect_failed", tenant_id=str(tenant_id))
                del self._tasks[tenant_id]
                self._stop.discard(tenant_id)
                self._pairing_trips.pop(tenant_id, None)
                self._qr_cycles.discard(tenant_id)
        for ctx in contexts:
            if ctx.tenant_id not in self._tasks and ctx.tenant_id not in self._stop:
                self._tasks[ctx.tenant_id] = asyncio.create_task(self.run_session(ctx))

    async def run_session(self, ctx: SessionContext) -> None:
        work_dir = self.store.work_dir(ctx.tenant_id)
        attempt = 0
        while ctx.tenant_id not in self._stop:
            try:
                if ctx.status in (status.PENDING_QR, status.ERROR):
                    # Sesión que NUNCA se vinculó: el directorio de trabajo puede
                    # tener restos de intentos previos, y Neonize entonces "reanuda"
                    # en vez de emitir un QR nuevo (visto en vivo: timeout de login
                    # sin QR y sin que corriera el watchdog). Se limpia al empezar.
                    await self.store.clear(ctx.tenant_id)
                await self.store.materialize(ctx.tenant_id, work_dir)
                await self.registry.set_status(ctx.tenant_id, status.CONNECTING)
                self._qr_cycles.discard(ctx.tenant_id)
                sock = self.socket_factory()
                self._sockets[ctx.tenant_id] = sock
                await asyncio.wait_for(
                    sock.connect(
                        session_dir=work_dir,
                        on_qr=lambda qr: self._on_qr(ctx, qr),
                        on_status=lambda st, detail=None, jid=None: self._on_status(
                            ctx, st, detail, jid
                        ),
                        on_message=lambda ev: self._on_message(ctx, ev),
                    ),
                    timeout=self.connect_timeout_s,
                )
                attempt = 0
                await self._drain_until_closed(ctx, sock)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("waweb_session_error", tenant_id=str(ctx.tenant_id))
                await self._on_status(ctx, "error", str(exc)[:200])
            finally:
                try:
                    await self.store.persist(ctx.tenant_id, work_dir)
                except Exception:
                    log.exception("waweb_session_persist_failed")
                # Un ciclo que termina NO deja socket vivo: las reconexiones
                # internas de Neonize competirían con el próximo socket y el
                # servidor reemplaza un stream (visto en vivo: doble auth +
                # StreamReplaced + mensajes perdidos).
                try:
                    await sock.disconnect()
                except Exception:
                    log.warning("waweb_session_disconnect_failed", tenant_id=str(ctx.tenant_id))
            if ctx.tenant_id in self._stop:
                self._sockets.pop(ctx.tenant_id, None)
                return
            attempt += 1
            # Backoff acotado; nunca dos sockets vivos a la vez (attempt guard):
            # un segundo socket sobre la misma sesión es causa conocida de bans.
            await self.sleep(min(self.reconnect_max_s, 2 ** min(attempt, 6)))

    async def _drain_until_closed(self, ctx: SessionContext, sock: WaWebSocket) -> None:
        if sock.closed.is_set() or ctx.tenant_id in self._stop:
            return
        policy = await self.policy_loader(ctx.tenant_id)
        idle = 0
        inicio = self.now()
        while not sock.closed.is_set() and ctx.tenant_id not in self._stop:
            processed = await self.drainer(
                ctx.tenant_id,
                sock,
                policy,
                sleep=self.sleep,
                timeout=1,
                allow=lambda: limits.allow_send(ctx.tenant_id, policy),
                on_sent=lambda: limits.record_outbound(ctx.tenant_id),
            )
            if not processed:
                idle += 1
                # Persistencia periódica: Neonize actualiza su estado en el
                # directorio efímero mientras la sesión vive; sin este guardado
                # un reinicio perdería la sesión y pediría QR de nuevo.
                if idle % 20 == 0:
                    try:
                        await self.store.persist(ctx.tenant_id, self.store.work_dir(ctx.tenant_id))
                        await handoff.set_ready(str(ctx.tenant_id))
                    except Exception:
                        log.exception("waweb_session_persist_failed")
                if ctx.tenant_id not in self._connected:
                    ultimo = max(self._qr_at.get(ctx.tenant_id, 0.0), inicio)
                    if (
                        ctx.tenant_id not in self._qr_cycles
                        and self.now() - ultimo > self.pairing_timeout_s
                    ):
                        trips = self._pairing_trips.get(ctx.tenant_id, 0) + 1
                        self._pairing_trips[ctx.tenant_id] = trips
                        log.warning(
                            "waweb_pairing_watchdog",
                            tenant_id=str(ctx.tenant_id),
                            trips=trips,
                        )
                        if trips >= self.pairing_reset_after:
                            # Reciclar en vano no recupera el QR: se limpia la
                            # sesión guardada para volver a emitirlo desde cero.
                            try:
                                await self.store.clear(ctx.tenant_id)
                                self._pairing_trips[ctx.tenant_id] = 0
                                log.warning(
                                    "waweb_pairing_session_cleared",
                                    tenant_id=str(ctx.tenant_id),
                                )
                            except Exception:
                                log.exception(
                                    "waweb_pairing_clear_failed",
                                    tenant_id=str(ctx.tenant_id),
                                )
                        sock.closed.set()
                        break
                await self.sleep(0.05)

    async def _on_qr(self, ctx: SessionContext, qr: str) -> None:
        self._qr_at[ctx.tenant_id] = self.now()
        self._qr_cycles.add(ctx.tenant_id)
        self._pairing_trips.pop(ctx.tenant_id, None)
        await handoff.publish_qr(str(ctx.tenant_id), qr)
        # `detail=""` limpia un `last_error` previo (p. ej. de un intento fallido).
        await self.registry.set_status(ctx.tenant_id, status.PENDING_QR, detail="")

    async def _on_status(
        self, ctx: SessionContext, backend_status: str, detail: str | None = None, jid: str | None = None
    ) -> None:
        mapped = _STATUS_MAP.get(backend_status, status.ERROR)
        if mapped == status.CONNECTED:
            self._connected.add(ctx.tenant_id)
            self._pairing_trips.pop(ctx.tenant_id, None)
        else:
            self._connected.discard(ctx.tenant_id)
        await self.registry.set_status(ctx.tenant_id, mapped, detail=detail, jid=jid)
        if mapped != status.CONNECTED:
            await handoff.clear_ready(str(ctx.tenant_id))
        if backend_status == "banned":
            # Señal directa de suspensión: alerta crítica (no se detiene: un ban
            # temporal expira y el manager reintenta).
            await self.alert(
                "WhatsApp (waweb) suspendido temporalmente",
                f"Tenant {ctx.tenant_id}: {detail or 'sin detalle'}",
                severity="critical",
            )
        elif mapped == status.CONNECTED:
            await limits.clear_disconnects(ctx.tenant_id)
            await handoff.set_ready(str(ctx.tenant_id))
            await self._check_duplicate_jid(ctx, jid)
        elif mapped == status.LOGGED_OUT:
            self._stop.add(ctx.tenant_id)
            # Descartar envíos encolados: que no se despachen contra una sesión muerta.
            try:
                purged = await purge_outbox(ctx.tenant_id, "sesión de WhatsApp cerrada")
            except Exception:
                purged = 0
            await self.store.clear(ctx.tenant_id)
            suffix = f" ({purged} envíos descartados)" if purged else ""
            await self.alert(
                "WhatsApp (waweb) deslogueado",
                f"El número del tenant {ctx.tenant_id} cerró la sesión. Requiere re-vincular por QR.{suffix}",
                severity="critical",
            )
        elif mapped == status.DISCONNECTED:
            count = await limits.record_disconnect(ctx.tenant_id)
            if count >= limits.DISCONNECT_ALERT_THRESHOLD:
                await self.alert(
                    "WhatsApp (waweb) con desconexiones repetidas",
                    f"{count} desconexiones en 10 min para el tenant {ctx.tenant_id}.",
                    severity="warning",
                )

    async def _check_duplicate_jid(self, ctx: SessionContext, jid: str | None) -> None:
        """Un mismo número no debe quedar vinculado a dos negocios: duplicaría
        conversaciones y respondería cruzado. Se conserva el tenant de menor id
        y se detiene el resto."""
        if not jid:
            return
        try:
            tenants = await self.registry.tenants_with_jid(jid)
        except Exception:
            log.exception("waweb_duplicate_check_failed", tenant_id=str(ctx.tenant_id))
            return
        others = [t for t in tenants if t != ctx.tenant_id]
        if not others:
            return
        await self.alert(
            "WhatsApp (waweb): número vinculado en más de un negocio",
            f"El número {jid} aparece en {ctx.tenant_id} y {others}. Se mantiene el de menor id.",
            severity="critical",
        )
        if any(str(other) < str(ctx.tenant_id) for other in others):
            await self.registry.set_status(
                ctx.tenant_id, status.ERROR, detail="Número vinculado también en otro negocio"
            )
            self._stop.add(ctx.tenant_id)
            sock = self._sockets.get(ctx.tenant_id)
            if sock is not None:
                try:
                    await sock.disconnect()
                except Exception:
                    log.warning("waweb_duplicate_disconnect_failed", tenant_id=str(ctx.tenant_id))

    async def _on_message(self, ctx: SessionContext, event: InboundEvent) -> None:
        accepted = await inbound_mod.publish_inbound(
            tenant_id=ctx.tenant_id,
            connection_external_id=ctx.connection_external_id,
            sender=event.sender,
            text=event.text,
            wamid=event.wamid,
            customer_name=event.customer_name,
            media=event.media,
        )
        if accepted:
            log.info(
                "waweb_inbound_publicado",
                tenant_id=str(ctx.tenant_id),
                wamid=event.wamid,
                sender=event.sender,
            )
        else:
            log.info("waweb_inbound_duplicado", tenant_id=str(ctx.tenant_id), wamid=event.wamid)
