"""Tests puros del manager del connector `waweb` (ADR-0074)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.waweb import manager as manager_mod
from core.waweb import status
from core.waweb.backend import InboundEvent
from core.waweb.manager import WaWebConnector
from core.waweb.policy import WaWebPolicy
from core.waweb.registry import SessionContext


class FakeSocket:
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.connected = False
        self.session_dir: Path | None = None
        self.sent: list[tuple[str, str]] = []
        self.callbacks: dict[str, Any] = {}

    async def connect(self, *, session_dir, on_qr, on_status, on_message):
        self.connected = True
        self.session_dir = session_dir
        self.callbacks = {"qr": on_qr, "status": on_status, "message": on_message}
        # Igual que el backend real: al conectar emite "connected".
        await on_status("connected", None)

    async def send(self, *, to, text, image_url=None):
        self.sent.append((to, text))
        return "WA-1"

    async def alive(self):
        return True

    async def disconnect(self):
        self.closed.set()


class FakeStore:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.persisted: list[uuid.UUID] = []
        self.cleared: list[uuid.UUID] = []

    def work_dir(self, tenant_id: uuid.UUID) -> Path:
        return self.base / str(tenant_id)

    async def materialize(self, tenant_id: uuid.UUID, work_dir: Path) -> bool:
        return False

    async def persist(self, tenant_id: uuid.UUID, work_dir: Path) -> None:
        self.persisted.append(tenant_id)

    async def clear(self, tenant_id: uuid.UUID) -> None:
        self.cleared.append(tenant_id)


class FakeRegistry:
    def __init__(self, contexts: list[SessionContext]) -> None:
        self.contexts = contexts
        self.statuses: dict[uuid.UUID, str] = {}
        self.jids_with: dict[str, list[uuid.UUID]] = {}
        self.on_set: Any = None

    async def active_sessions(self) -> list[SessionContext]:
        return self.contexts

    async def set_status(self, tenant_id, new_status, *, detail=None, jid=None):
        self.statuses[tenant_id] = new_status
        if self.on_set is not None:
            await self.on_set(tenant_id, new_status)

    async def tenants_with_jid(self, jid: str) -> list[uuid.UUID]:
        return list(self.jids_with.get(jid, []))


@pytest.fixture(autouse=True)
def _stub_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(manager_mod.handoff, "set_ready", _noop)
    monkeypatch.setattr(manager_mod.handoff, "clear_ready", _noop)
    monkeypatch.setattr(manager_mod.handoff, "publish_qr", _noop)
    monkeypatch.setattr(manager_mod, "purge_outbox", _noop)

    async def _sin_reset(*a: Any, **k: Any) -> bool:
        return False

    # Sin esto cada vuelta del drain intentaba hablar con Redis.
    monkeypatch.setattr(manager_mod.handoff, "consume_reset", _sin_reset)


def _ctx() -> SessionContext:
    return SessionContext(
        tenant_id=uuid.UUID(int=1),
        connection_id=uuid.UUID(int=2),
        connection_external_id="waweb:1",
        status=status.PENDING_QR,
    )


def _connector(tmp_path: Path, registry: FakeRegistry, sock: FakeSocket | None = None) -> WaWebConnector:
    async def _noop_sleep(_: float) -> None:
        return None

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    return WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=lambda: sock or FakeSocket(),
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_noop_sleep,
        alert=_alert,
    )


async def test_on_message_publica_inbound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[dict[str, Any]] = []

    async def _publish(**kwargs: Any) -> bool:
        published.append(kwargs)
        return True

    monkeypatch.setattr(manager_mod.inbound_mod, "publish_inbound", _publish)
    connector = _connector(tmp_path, FakeRegistry([]))

    await connector._on_message(_ctx(), InboundEvent(sender="57300", text="hola", wamid="W1"))

    assert published[0]["tenant_id"] == uuid.UUID(int=1)
    assert published[0]["connection_external_id"] == "waweb:1"
    assert published[0]["text"] == "hola"


async def test_on_status_logged_out_detiene_y_limpia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _clear(_: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(manager_mod.limits, "clear_disconnects", _clear)
    alerts: list[Any] = []

    async def _alert(*a: Any, **k: Any) -> None:
        alerts.append((a, k))

    registry = FakeRegistry([])
    connector = _connector(tmp_path, registry)
    connector.alert = _alert

    await connector._on_status(_ctx(), "logged_out")

    assert registry.statuses[uuid.UUID(int=1)] == status.LOGGED_OUT
    assert connector.store.cleared == [uuid.UUID(int=1)]
    assert uuid.UUID(int=1) in connector._stop
    assert alerts and alerts[0][1]["severity"] == "critical"


async def test_on_status_desconexiones_repetidas_alerta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _record(_: uuid.UUID) -> int:
        return manager_mod.limits.DISCONNECT_ALERT_THRESHOLD

    monkeypatch.setattr(manager_mod.limits, "record_disconnect", _record)
    alerts: list[Any] = []

    async def _alert(*a: Any, **k: Any) -> None:
        alerts.append((a, k))

    connector = _connector(tmp_path, FakeRegistry([]))
    connector.alert = _alert

    await connector._on_status(_ctx(), "disconnected")

    assert alerts and alerts[0][1]["severity"] == "warning"


async def test_run_session_conecta_y_persiste(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _clear(_: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(manager_mod.limits, "clear_disconnects", _clear)

    ctx = _ctx()
    registry = FakeRegistry([ctx])
    sock = FakeSocket()
    connector = _connector(tmp_path, registry, sock)

    async def on_set(tenant_id: uuid.UUID, new_status: str) -> None:
        # Al conectar, detenemos el bucle (no hay drainer real en el test).
        if new_status == status.CONNECTED:
            connector._stop.add(tenant_id)

    registry.on_set = on_set

    await asyncio.wait_for(connector.run_session(ctx), timeout=2)

    assert sock.connected is True
    assert registry.statuses[ctx.tenant_id] == status.CONNECTED
    assert set(connector.store.persisted) == {ctx.tenant_id}


async def test_watchdog_vinculacion_recicla_socket_sin_qr(tmp_path: Path) -> None:
    """Un socket que queda colgado sin QR (timeout de login de Neonize) se recicla.

    Antes el drain seguía en bucle para siempre y el panel quedaba en
    "esperando el QR" sin forma de recuperarse salvo reiniciar el conector.
    """
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    sockets: list[Any] = []

    class SocketMudo(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            # Ni "connected" ni QR: el login quedó colgado (timeout).
            self.connected = True
            self.session_dir = session_dir
            self.callbacks = {"qr": on_qr, "status": on_status, "message": on_message}

    def factory() -> Any:
        sock = SocketMudo()
        sockets.append(sock)
        if len(sockets) >= 2:
            connector._stop.add(ctx.tenant_id)
        return sock

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _sleep(segundos: float) -> None:
        reloj["t"] += max(segundos, 0.05)

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    connector = WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
        now=lambda: reloj["t"],
        pairing_timeout_s=0.2,
    )

    await asyncio.wait_for(connector.run_session(ctx), timeout=2)

    assert len(sockets) == 2
    assert sockets[0].closed.is_set()


async def test_connect_colgado_se_recicla_por_timeout(tmp_path: Path) -> None:
    """Un `connect()` que nunca termina no puede congelar el connector.

    Bug real: el `await connect()` de un tenant quedó colgado para siempre y el
    proceso entero dejó de arrancar sesiones de los demás (ni el watchdog corría).
    """
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    sockets: list[Any] = []
    connector_holder: dict[str, Any] = {}
    secuencia: list[str] = []

    async def on_set(_tenant_id: uuid.UUID, new_status: str) -> None:
        secuencia.append(new_status)

    registry.on_set = on_set

    class SocketColgado(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            self.session_dir = session_dir
            await asyncio.sleep(3600)  # se queda colgado para siempre

    def factory() -> Any:
        connector = connector_holder["connector"]
        if not sockets:
            sock: Any = SocketColgado()
        else:
            connector._stop.add(ctx.tenant_id)
            sock = FakeSocket()
        sockets.append(sock)
        return sock

    async def _sleep(_: float) -> None:
        return None

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    connector = WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
        connect_timeout_s=0.1,
    )
    connector_holder["connector"] = connector

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert len(sockets) == 2, "el socket colgado debe reciclarse"
    # El timeout de conexión quedó registrado como error antes de reciclar.
    assert status.ERROR in secuencia


async def test_watchdog_limpia_la_sesion_tras_n_reciclados(tmp_path: Path) -> None:
    """Tras N reciclados sin QR se limpia la sesión guardada (Neonize deja de
    emitir QR después de un timeout de login; reciclar sin limpiar no lo cura)."""
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    sockets: list[Any] = []

    class SocketMudo(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            self.session_dir = session_dir
            self.callbacks = {"qr": on_qr, "status": on_status, "message": on_message}

    def factory() -> Any:
        sock = SocketMudo()
        sockets.append(sock)
        if len(sockets) >= 3:
            connector._stop.add(ctx.tenant_id)
        return sock

    async def _sleep(segundos: float) -> None:
        reloj["t"] += max(segundos, 0.05)

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    connector = WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
        now=lambda: reloj["t"],
        pairing_timeout_s=0.2,
        pairing_reset_after=2,
    )

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert connector.store.cleared.count(ctx.tenant_id) >= 2, (
        "debe limpiar al iniciar el ciclo sin vincular y otra vez al 2º reciclado"
    )


async def test_socket_con_qr_no_se_recicla_por_timeout(tmp_path: Path) -> None:
    """Un QR ya emitido NO se recicla: la sesión sana espera a que la escaneen.

    Regresión real: el watchdog reciclaba también las sesiones con QR vigente
    (porque el QR llevaba >45 s sin escanear), reiniciando el vínculo cada ~45 s.
    """
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    sockets: list[Any] = []
    holder: dict[str, Any] = {}

    class SocketSoloQr(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            self.session_dir = session_dir
            self.callbacks = {"qr": on_qr, "status": on_status, "message": on_message}
            # Emite un QR pero NUNCA reporta "connected" (falta el escaneo).
            await on_qr("QR-DE-PRUEBA")

    def factory() -> Any:
        sock = SocketSoloQr()
        sockets.append(sock)
        return sock

    async def _sleep(segundos: float) -> None:
        reloj["t"] += max(segundos, 0.05)
        # Bien pasado el timeout del watchdog, cortamos el bucle a mano.
        if reloj["t"] > 1.0:
            holder["connector"]._stop.add(ctx.tenant_id)

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    connector = WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
        now=lambda: reloj["t"],
        pairing_timeout_s=0.2,
    )
    holder["connector"] = connector

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert len(sockets) == 1, "con un QR emitido no debe reciclarse el socket"
    # Solo la limpieza inicial (sesión sin vincular), no reciclados del watchdog.
    assert connector.store.cleared.count(ctx.tenant_id) == 1


def _connector_con_reloj(
    tmp_path: Path,
    registry: FakeRegistry,
    factory: Any,
    reloj: dict[str, float],
    alerts: list[Any],
    paso_s: float = 5.0,
) -> WaWebConnector:
    """Connector cuyo reloj avanza `paso_s` por cada `sleep` del drain."""

    async def _sleep(segundos: float) -> None:
        reloj["t"] += paso_s
        # Cede el loop: si el reciclado no ocurre, `wait_for` corta el test en
        # vez de colgarlo.
        await asyncio.sleep(0)

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        alerts.append((a, k))

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    return WaWebConnector(
        registry=registry,
        store=FakeStore(tmp_path),
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
        now=lambda: reloj["t"],
        liveness_interval_s=30.0,
        liveness_grace_s=60.0,
    )


@pytest.fixture
def _limits_sin_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _clear(_: uuid.UUID) -> None:
        return None

    async def _record(_: uuid.UUID) -> int:
        return 1

    monkeypatch.setattr(manager_mod.limits, "clear_disconnects", _clear)
    monkeypatch.setattr(manager_mod.limits, "record_disconnect", _record)


@pytest.mark.usefixtures("_limits_sin_redis")
async def test_socket_zombie_se_recicla_con_la_sesion_guardada(tmp_path: Path) -> None:
    """Un socket "conectado" que ya no lo está se recicla sin pedir QR.

    Incidente en test (2026-10-06 y 2026-10-10): el auto-reconnect interno de
    Neonize falló sin emitir `DisconnectedEv`; la sesión siguió en `connected`
    con el drain vivo y los mensajes entrantes se perdían en silencio.
    """
    ctx = _ctx()  # arrancó en pending_qr: se vincula dentro de esta tarea
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    alerts: list[Any] = []
    sockets: list[Any] = []
    secuencia: list[str] = []

    async def on_set(_tenant_id: uuid.UUID, new_status: str) -> None:
        secuencia.append(new_status)

    registry.on_set = on_set

    class SocketZombie(FakeSocket):
        async def alive(self):
            return False

    def factory() -> Any:
        sock = SocketZombie()
        sockets.append(sock)
        if len(sockets) >= 2:
            connector._stop.add(ctx.tenant_id)
        return sock

    connector = _connector_con_reloj(tmp_path, registry, factory, reloj, alerts)

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert len(sockets) == 2, "el socket zombie debe reciclarse"
    assert sockets[0].closed.is_set()
    assert status.DISCONNECTED in secuencia
    assert [k["severity"] for _a, k in alerts] == ["warning"]
    # Solo la limpieza inicial: tras vincularse, el reciclado conserva la sesión.
    assert connector.store.cleared.count(ctx.tenant_id) == 1
    # La gracia se respeta: no recicla antes de 60 s sin conexión.
    assert reloj["t"] >= 60.0


@pytest.mark.usefixtures("_limits_sin_redis")
async def test_reconexion_interna_breve_no_recicla(tmp_path: Path) -> None:
    """Sin conexión menos que la gracia (auto-reconnect normal): no se toca."""
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    alerts: list[Any] = []
    sockets: list[Any] = []
    holder: dict[str, Any] = {}

    class SocketQueSeRecupera(FakeSocket):
        async def alive(self):
            # Cae entre t=30 y t=60 (dos chequeos), luego vuelve.
            vivo = not (30.0 <= reloj["t"] < 70.0)
            if reloj["t"] > 300.0:
                holder["connector"]._stop.add(ctx.tenant_id)
            return vivo

    def factory() -> Any:
        sock = SocketQueSeRecupera()
        sockets.append(sock)
        return sock

    connector = _connector_con_reloj(tmp_path, registry, factory, reloj, alerts)
    holder["connector"] = connector

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert len(sockets) == 1
    assert alerts == []
    assert registry.statuses[ctx.tenant_id] == status.CONNECTED


@pytest.mark.usefixtures("_limits_sin_redis")
async def test_chequeo_de_vida_solo_con_sesion_conectada(tmp_path: Path) -> None:
    """Mientras espera el escaneo del QR no se consulta `alive()`."""
    ctx = _ctx()
    registry = FakeRegistry([ctx])
    reloj = {"t": 0.0}
    alerts: list[Any] = []
    consultas: list[float] = []
    holder: dict[str, Any] = {}

    class SocketSoloQr(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            self.session_dir = session_dir
            await on_qr("QR-DE-PRUEBA")

        async def alive(self):
            consultas.append(reloj["t"])
            return False

    def factory() -> Any:
        return SocketSoloQr()

    connector = _connector_con_reloj(tmp_path, registry, factory, reloj, alerts)
    holder["connector"] = connector

    original_sleep = connector.sleep

    async def _sleep(segundos: float) -> None:
        await original_sleep(segundos)
        if reloj["t"] > 300.0:
            connector._stop.add(ctx.tenant_id)

    connector.sleep = _sleep

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert consultas == []
    assert alerts == []


async def test_sweep_limpia_el_estado_conectado_de_una_tarea_muerta(tmp_path: Path) -> None:
    ctx = _ctx()
    connector = _connector(tmp_path, FakeRegistry([]))
    tarea = asyncio.create_task(asyncio.sleep(0))
    await tarea
    connector._tasks[ctx.tenant_id] = tarea
    connector._connected.add(ctx.tenant_id)
    connector._linked.add(ctx.tenant_id)

    await connector.sweep()

    assert ctx.tenant_id not in connector._connected
    assert ctx.tenant_id not in connector._linked


class StoreConBlob(FakeStore):
    """Store que simula el blob cifrado de `waweb_sessions.creds_encrypted`.

    `persist` copia el `session.db` del directorio de trabajo al blob y
    `materialize` lo restaura, como `DbEncryptedSessionStore`. `eventos` guarda
    el orden de las llamadas.
    """

    def __init__(self, base: Path, blob: str | None) -> None:
        super().__init__(base)
        self.blob = blob
        self.eventos: list[str] = []

    async def materialize(self, tenant_id: uuid.UUID, work_dir: Path) -> bool:
        self.eventos.append("materialize")
        if not self.blob:
            return False
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "session.db").write_text(self.blob)
        return True

    async def persist(self, tenant_id: uuid.UUID, work_dir: Path) -> None:
        self.eventos.append("persist")
        await super().persist(tenant_id, work_dir)
        db = work_dir / "session.db"
        if db.exists():
            self.blob = db.read_text()

    async def clear(self, tenant_id: uuid.UUID) -> None:
        self.eventos.append("clear")
        await super().clear(tenant_id)
        self.blob = None


async def test_reset_del_panel_con_sesion_vinculada_vuelve_a_pedir_qr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El reset pedido desde el panel debe surtir efecto aunque la tarea del
    connector haya arrancado con la sesión ya vinculada (`ctx.status=connected`).

    Bug: el `finally` de `run_session` volvía a persistir el directorio de
    trabajo (pisando el `creds_encrypted=None` que deja `reset_session`) y el
    siguiente ciclo no limpiaba porque `ctx` sigue diciendo `connected`: el
    connector reconectaba con la sesión vieja y nunca emitía QR.
    """

    async def _clear(_: uuid.UUID) -> None:
        return None

    monkeypatch.setattr(manager_mod.limits, "clear_disconnects", _clear)

    ctx = SessionContext(
        tenant_id=uuid.UUID(int=1),
        connection_id=uuid.UUID(int=2),
        connection_external_id="waweb:1",
        status=status.CONNECTED,
        linked_before=True,
    )
    registry = FakeRegistry([ctx])
    store = StoreConBlob(tmp_path, blob="creds-viejas")
    holder: dict[str, Any] = {}
    sockets: list[Any] = []
    vistos: list[tuple[bool, str | None]] = []

    class SocketNeonize(FakeSocket):
        async def connect(self, *, session_dir, on_qr, on_status, on_message):
            self.session_dir = session_dir
            db = session_dir / "session.db"
            # Lo que encuentra este socket al abrir el store de Neonize.
            vistos.append((db.exists(), store.blob))
            if db.exists():
                await on_status("connected", None)
            else:
                # Sin credenciales Neonize empieza a vincular: emite QR.
                session_dir.mkdir(parents=True, exist_ok=True)
                db.write_text("sin-vincular")
                await on_qr("QR-NUEVO")

    def factory() -> Any:
        sock = SocketNeonize()
        sockets.append(sock)
        if len(sockets) >= 2:
            holder["connector"]._stop.add(ctx.tenant_id)
        return sock

    resets = {"pendientes": 1}

    async def _consume_reset(_tenant_id: str) -> bool:
        if resets["pendientes"]:
            resets["pendientes"] -= 1
            # Lo que hace `reset_session` en el panel: la fila queda sin creds.
            store.blob = None
            store.eventos.append("reset")
            return True
        return False

    monkeypatch.setattr(manager_mod.handoff, "consume_reset", _consume_reset)

    async def _sleep(_: float) -> None:
        return None

    async def _noop_drain(*a: Any, **k: Any) -> bool:
        return False

    async def _alert(*a: Any, **k: Any) -> None:
        return None

    async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
        return WaWebPolicy()

    connector = WaWebConnector(
        registry=registry,
        store=store,
        socket_factory=factory,
        policy_loader=_policy,
        drainer=_noop_drain,
        sleep=_sleep,
        alert=_alert,
    )
    holder["connector"] = connector

    await asyncio.wait_for(connector.run_session(ctx), timeout=3)

    assert len(sockets) == 2
    assert vistos[0] == (True, "creds-viejas")
    tras_reset = store.eventos[store.eventos.index("reset") :]
    segundo_materialize = tras_reset.index("materialize")
    assert "persist" not in tras_reset[:segundo_materialize], (
        f"no debe persistir el directorio viejo tras el reset: {store.eventos}"
    )
    assert "clear" in tras_reset[:segundo_materialize], (
        f"debe limpiar la sesión antes del siguiente ciclo: {store.eventos}"
    )
    assert vistos[1] == (False, None), "el socket nuevo debe arrancar sin credenciales"
    assert registry.statuses[ctx.tenant_id] == status.PENDING_QR
    # El QR que sigue a un reset es lo esperado: no es una "sesión perdida".
    assert connector._lost_alerted == set()
