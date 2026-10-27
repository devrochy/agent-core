"""Manager del proceso `connector` del canal `waweb` (ADR-0074).

Mantiene un socket por sesión activa, conecta los callbacks del socket con el
inbound del core y con el drenaje de salida (con tope/pausa), persiste el estado
y reconecta con backoff. No conoce Neonize: habla con `WaWebSocket`.

Diseño para test: todas las dependencias son inyectables (registro, store,
factoría de socket, drainer, sleep, alert) — se puede correr entero con fakes.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

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

# Texto que ve el panel (`last_error`) cuando una sesión que ya estuvo vinculada
# vuelve a pedir QR: el emparejamiento se perdió y hay que re-vincular.
SESION_PERDIDA = "Sesión perdida: re-vincular por QR"

_STATUS_MAP: dict[str, str] = {
    # El backend real emite `connecting` al abrir el socket: antes caía al default
    # `ERROR` y dejaba la fila en `error` en cada reinicio hasta conectar.
    "connecting": status.CONNECTING,
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
    # Tras un QR emitido sin `connected`: whatsmeow rota el QR (el primero dura
    # 60 s, los siguientes 20 s) y, al vencer el último, emite `Login event:
    # timeout` y NO emite más QR (el conector quedaba mudo, visto en test
    # 2026-10-26). Pasado este silencio se recicla para pedir un QR nuevo. Debe ser
    # mayor que los 60 s del primer QR, por eso no reutiliza `pairing_timeout_s`.
    qr_expired_s: float = 90.0
    # Si `connect()` no termina en este tiempo el socket se recicla: un `await`
    # eterno en la conexión congelaba TODO el connector (ningún tenant más
    # arrancaba sesión y el watchdog ni llegaba a correr).
    connect_timeout_s: float = 90.0
    # Tras N reciclados sin QR se limpia la sesión guardada: Neonize deja de
    # emitir QR después de un timeout de login y reciclar sin limpiar no lo
    # recupera (bug real visto en producción: bucle infinito de watchdog).
    pairing_reset_after: int = 3
    # Chequeo de vida de sesiones conectadas: cada `liveness_interval_s` se
    # pregunta al socket si sigue conectado; si lleva `liveness_grace_s` sin
    # conexión se recicla. Neonize puede fallar el auto-reconnect SIN emitir
    # `DisconnectedEv` y el socket quedaba zombie: estado `connected`, drain
    # vivo y ningún mensaje entrante (incidentes en test 2026-10-06 y
    # 2026-10-10). La gracia evita pisar el auto-reconnect normal (~3-30 s).
    liveness_interval_s: float = 30.0
    liveness_grace_s: float = 60.0
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
    # Negocios que llegaron a `connected` en su tarea actual. `ctx.status` es el
    # estado leído al crear la tarea: si era `pending_qr` y el negocio se vinculó
    # después, un reciclado (desconexión, zombie) NO debe limpiar la sesión
    # recién guardada y pedir QR otra vez.
    _linked: set[uuid.UUID] = field(default_factory=set, init=False)
    # Negocios con un reset del panel consumido y todavía sin aplicar. `ctx` es
    # el estado leído al crear la tarea (no se relee): sin esta marca, una sesión
    # que arrancó `connected` reconectaba con las credenciales viejas tras el reset.
    _reset_pending: set[uuid.UUID] = field(default_factory=set, init=False)
    # Negocios cuya sesión se desvinculó A PROPÓSITO (reset del panel) en la tarea
    # actual: `ctx.linked_before` queda desactualizado, y el QR que sigue es lo
    # esperado, no una "sesión perdida".
    _unlinked: set[uuid.UUID] = field(default_factory=set, init=False)
    # Negocios cuyo directorio de trabajo es confiable para persistir: se restauró
    # del blob durable en este ciclo o la sesión llegó a `connected`. Un directorio
    # que Neonize creó vacío (sin dispositivo) nunca debe pisar el blob guardado.
    _can_persist: set[uuid.UUID] = field(default_factory=set, init=False)
    # Negocios a los que se les debe borrar el directorio LOCAL (el blob durable no
    # se toca) al iniciar el siguiente ciclo, para que Neonize emita un QR nuevo.
    _wipe_local: set[uuid.UUID] = field(default_factory=set, init=False)
    # Negocios ya alertados de "sesión perdida" (el QR rota cada ~20 s: una vez).
    _lost_alerted: set[uuid.UUID] = field(default_factory=set, init=False)

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
                self._linked.discard(tenant_id)
                self._unlinked.discard(tenant_id)
                self._can_persist.discard(tenant_id)
                self._wipe_local.discard(tenant_id)
                self._lost_alerted.discard(tenant_id)
                # Una tarea que muere sin pasar por `_on_status` no debe dejar
                # `connected` a la siguiente: el chequeo de vida reciclaría un
                # socket que todavía se está vinculando.
                self._connected.discard(tenant_id)
        for ctx in contexts:
            if ctx.tenant_id not in self._tasks and ctx.tenant_id not in self._stop:
                self._tasks[ctx.tenant_id] = asyncio.create_task(self.run_session(ctx))

    async def run_session(self, ctx: SessionContext) -> None:
        work_dir = self.store.work_dir(ctx.tenant_id)
        attempt = 0
        while ctx.tenant_id not in self._stop:
            try:
                reset = ctx.tenant_id in self._reset_pending
                if reset:
                    await self._clear_session(ctx.tenant_id, "reset_panel")
                elif not self._was_linked(ctx):
                    # Sesión que NUNCA se vinculó: puede haber restos de intentos
                    # previos y Neonize entonces "reanuda" en vez de emitir un QR
                    # nuevo (visto en vivo). La decisión usa un dato durable
                    # (`ctx.linked_before`), NO el `status` de la fila ni `_linked`
                    # (memoria del proceso, vacía tras cada reinicio): una sesión que
                    # sí estuvo vinculada y quedó en `error`/`pending_qr` por un
                    # fallo transitorio perdía sus credenciales al siguiente
                    # arranque (incidente 2026-10-26).
                    await self._clear_session(ctx.tenant_id, "nunca_vinculada")
                if reset or ctx.tenant_id in self._wipe_local:
                    # Re-vincular desde el panel (o QR vencido): además del blob
                    # durable —solo en el reset— hay que borrar el `session.db`
                    # local, o Neonize reanuda la sesión vieja (ya cerrada: el
                    # socket se desconectó en el `finally`).
                    shutil.rmtree(work_dir, ignore_errors=True)
                    self._wipe_local.discard(ctx.tenant_id)
                if reset:
                    self._reset_pending.discard(ctx.tenant_id)
                restored = await self.store.materialize(ctx.tenant_id, work_dir)
                if restored:
                    self._can_persist.add(ctx.tenant_id)
                else:
                    self._can_persist.discard(ctx.tenant_id)
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
                # Con un reset pendiente NO se persiste: el directorio de trabajo
                # tiene la sesión vieja y pisaría el `creds_encrypted=None` que
                # dejó `reset_session`.
                await self._persist(ctx, work_dir)
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
        ultimo_chequeo = inicio
        sin_conexion_desde: float | None = None
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
            if ctx.tenant_id not in self._connected:
                sin_conexion_desde = None
            elif self.now() - ultimo_chequeo >= self.liveness_interval_s:
                ultimo_chequeo = self.now()
                if await sock.alive():
                    sin_conexion_desde = None
                elif sin_conexion_desde is None:
                    sin_conexion_desde = ultimo_chequeo
                    log.info("waweb_socket_sin_conexion", tenant_id=str(ctx.tenant_id))
                elif ultimo_chequeo - sin_conexion_desde >= self.liveness_grace_s:
                    await self._recycle_zombie(ctx, sock, ultimo_chequeo - sin_conexion_desde)
                    break
            if not processed:
                idle += 1
                # El panel pidió reciclar la sesión (re-vincular): se cierra el
                # socket y se marca el reset para que `run_session` no persista
                # el directorio viejo y el próximo ciclo arranque sin credenciales
                # (emite un QR nuevo). Sin esto, el QR no aparece hasta que el
                # pairing-watchdog recicle por su cuenta.
                if await handoff.consume_reset(str(ctx.tenant_id)):
                    log.warning("waweb_reset_solicitado", tenant_id=str(ctx.tenant_id))
                    self._reset_pending.add(ctx.tenant_id)
                    self._connected.discard(ctx.tenant_id)
                    self._linked.discard(ctx.tenant_id)
                    self._unlinked.add(ctx.tenant_id)
                    self._can_persist.discard(ctx.tenant_id)
                    self._lost_alerted.discard(ctx.tenant_id)
                    self._qr_cycles.discard(ctx.tenant_id)
                    self._pairing_trips.pop(ctx.tenant_id, None)
                    sock.closed.set()
                    break
                # Persistencia periódica: Neonize actualiza su estado en el
                # directorio efímero mientras la sesión vive; sin este guardado
                # un reinicio perdería la sesión y pediría QR de nuevo.
                if idle % 20 == 0:
                    await self._persist(ctx, self.store.work_dir(ctx.tenant_id))
                    # `waweb:ready` solo mientras haya conexión: refrescarlo sin
                    # ella hacía que los envíos no fallaran rápido (esperaban 45 s).
                    if ctx.tenant_id in self._connected:
                        try:
                            await handoff.set_ready(str(ctx.tenant_id))
                        except Exception:
                            log.exception("waweb_set_ready_failed", tenant_id=str(ctx.tenant_id))
                if ctx.tenant_id not in self._connected:
                    ultimo = max(self._qr_at.get(ctx.tenant_id, 0.0), inicio)
                    ya_emitio_qr = ctx.tenant_id in self._qr_cycles
                    limite = self.qr_expired_s if ya_emitio_qr else self.pairing_timeout_s
                    if self.now() - ultimo > limite:
                        if ya_emitio_qr:
                            await self._on_qr_vencido(ctx)
                        else:
                            await self._on_sin_qr(ctx)
                        sock.closed.set()
                        break
                await self.sleep(0.05)

    def _was_linked(self, ctx: SessionContext) -> bool:
        """¿La sesión se vinculó alguna vez y conserva credenciales?

        Dato durable (`ctx.linked_before`) o vinculada en esta tarea. Tras un reset
        del panel (desvinculación deliberada) el dato durable queda obsoleto.
        """
        if ctx.tenant_id in self._linked:
            return True
        return ctx.linked_before and ctx.tenant_id not in self._unlinked

    async def _clear_session(self, tenant_id: uuid.UUID, motivo: str) -> None:
        """Borra el blob durable de la sesión. Toda limpieza deja traza con motivo."""
        # `nunca_vinculada` corre en cada ciclo de una sesión sin vincular (no hay
        # nada que perder): no merece un warning por ciclo.
        emit = log.info if motivo == "nunca_vinculada" else log.warning
        emit("waweb_session_cleared", tenant_id=str(tenant_id), motivo=motivo)
        await self.store.clear(tenant_id)

    async def _persist(self, ctx: SessionContext, work_dir: Path) -> None:
        """Guarda el directorio de trabajo solo si es de una sesión real.

        Un directorio que Neonize creó vacío (esquema sin dispositivo) pisaba el
        blob con una "sesión" vacía: así quedaron las sesiones de test (160 KB sin
        dispositivo). Con un reset pendiente tampoco se persiste: el directorio
        tiene la sesión vieja y pisaría el `creds_encrypted=None` del panel.
        """
        if ctx.tenant_id in self._reset_pending or ctx.tenant_id not in self._can_persist:
            return
        try:
            await self.store.persist(ctx.tenant_id, work_dir)
        except Exception:
            log.exception("waweb_session_persist_failed", tenant_id=str(ctx.tenant_id))

    async def _on_qr_vencido(self, ctx: SessionContext) -> None:
        """El QR emitido venció sin escanearse y Neonize dejó de emitir: reciclar."""
        log.warning("waweb_qr_vencido", tenant_id=str(ctx.tenant_id), qr_expired_s=self.qr_expired_s)
        # Solo el directorio local (el blob durable NO se toca, aunque la sesión se
        # haya vinculado antes): sin él Neonize no reanuda y emite un QR nuevo.
        self._wipe_local.add(ctx.tenant_id)

    async def _on_sin_qr(self, ctx: SessionContext) -> None:
        """Un socket sin QR ni `connected` (login colgado): reciclar y, si no hay
        nada que perder, limpiar la sesión guardada para emitir QR desde cero."""
        trips = self._pairing_trips.get(ctx.tenant_id, 0) + 1
        self._pairing_trips[ctx.tenant_id] = trips
        log.warning("waweb_pairing_watchdog", tenant_id=str(ctx.tenant_id), trips=trips)
        if trips < self.pairing_reset_after:
            return
        if self._was_linked(ctx):
            # Sesión vinculada que no logra reconectar (red caída, solape de
            # contenedores): reciclar sí, borrarle las credenciales NUNCA.
            log.warning("waweb_pairing_session_conservada", tenant_id=str(ctx.tenant_id), trips=trips)
            if trips == self.pairing_reset_after:
                await self._alertar(
                    "WhatsApp (waweb) no logra reconectar",
                    f"Tenant {ctx.tenant_id}: la sesión vinculada no conecta tras {trips} intentos. "
                    "Se conserva la sesión guardada y se sigue reintentando.",
                    severity="warning",
                )
            return
        # Reciclar en vano no recupera el QR: se limpia la sesión guardada para
        # volver a emitirlo desde cero.
        try:
            await self._clear_session(ctx.tenant_id, "watchdog_sin_qr")
            self._pairing_trips[ctx.tenant_id] = 0
        except Exception:
            log.exception("waweb_pairing_clear_failed", tenant_id=str(ctx.tenant_id))

    async def _alertar(self, titulo: str, detalle: str, *, severity: str) -> None:
        try:
            await self.alert(titulo, detalle, severity=severity)
        except Exception:
            log.exception("waweb_alert_failed", titulo=titulo)

    async def _recycle_zombie(self, ctx: SessionContext, sock: WaWebSocket, segundos: float) -> None:
        """Cierra un socket que dice estar conectado pero no lo está.

        `run_session` lo recicla con la sesión guardada (sin QR nuevo).
        """
        log.warning("waweb_socket_zombie", tenant_id=str(ctx.tenant_id), sin_conexion_s=round(segundos))
        try:
            await self.alert(
                "WhatsApp (waweb) socket sin conexión",
                f"Tenant {ctx.tenant_id}: el socket llevaba {round(segundos)} s sin conexión "
                "sin avisar desconexión. Se recicla con la sesión guardada.",
                severity="warning",
            )
        except Exception:
            log.exception("waweb_zombie_alert_failed", tenant_id=str(ctx.tenant_id))
        try:
            await self._on_status(ctx, "disconnected", "socket sin conexión")
        finally:
            sock.closed.set()

    async def _on_qr(self, ctx: SessionContext, qr: str) -> None:
        self._qr_at[ctx.tenant_id] = self.now()
        self._qr_cycles.add(ctx.tenant_id)
        self._pairing_trips.pop(ctx.tenant_id, None)
        await handoff.publish_qr(str(ctx.tenant_id), qr)
        # Una sesión que YA estuvo vinculada y vuelve a pedir QR perdió su
        # emparejamiento: se dice en el panel (`last_error`) y se alerta una vez.
        perdida = self._was_linked(ctx)
        # `detail=""` limpia un `last_error` previo (p. ej. de un intento fallido).
        await self.registry.set_status(
            ctx.tenant_id, status.PENDING_QR, detail=SESION_PERDIDA if perdida else ""
        )
        if perdida and ctx.tenant_id not in self._lost_alerted:
            self._lost_alerted.add(ctx.tenant_id)
            log.warning("waweb_sesion_perdida", tenant_id=str(ctx.tenant_id))
            await self._alertar(
                "WhatsApp (waweb) sesión perdida",
                f"Tenant {ctx.tenant_id}: la sesión ya vinculada pide QR otra vez. "
                "Requiere re-vincular por QR desde el panel.",
                severity="critical",
            )

    async def _on_status(
        self, ctx: SessionContext, backend_status: str, detail: str | None = None, jid: str | None = None
    ) -> None:
        mapped = _STATUS_MAP.get(backend_status, status.ERROR)
        if mapped == status.CONNECTED:
            self._connected.add(ctx.tenant_id)
            self._linked.add(ctx.tenant_id)
            self._unlinked.discard(ctx.tenant_id)
            self._can_persist.add(ctx.tenant_id)
            self._lost_alerted.discard(ctx.tenant_id)
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
            self._linked.discard(ctx.tenant_id)
            # Credenciales revocadas: el `finally` de `run_session` no debe volver a
            # guardar el directorio de la sesión cerrada.
            self._can_persist.discard(ctx.tenant_id)
            # Descartar envíos encolados: que no se despachen contra una sesión muerta.
            try:
                purged = await purge_outbox(ctx.tenant_id, "sesión de WhatsApp cerrada")
            except Exception:
                purged = 0
            await self._clear_session(ctx.tenant_id, "logged_out")
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
