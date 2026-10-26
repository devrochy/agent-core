"""La sesión de WhatsApp Web ya vinculada no se pierde en reinicios (ADR-0074).

Incidente en test (2026-10-26): las sesiones de `licoreria-010cb7` y `solvershub`
quedaron en `pending_qr` con el blob guardado reducido a un `session.db` de
whatsmeow vacío (solo esquema), aunque WhatsApp seguía dando el dispositivo por
vinculado; cada re-vinculación coincidió con un despliegue del conector.

Causa confirmada con estos tests (rojos contra el código anterior):

1. `run_session` limpiaba el blob al iniciar si `ctx.status` era `pending_qr` o
   `error` y el tenant no estaba en `_linked` (memoria del proceso, vacía tras
   cada reinicio), sin mirar si la sesión se había vinculado alguna vez.
2. El pairing-watchdog limpiaba el blob tras N reciclados sin QR, también en
   sesiones vinculadas que solo tardaban en reconectar.
3. `persist` (en el `finally` y cada 20 ciclos ociosos) escribía el directorio
   aunque no tuviera dispositivo: tras la limpieza dejaba un `session.db` vacío
   como "sesión guardada".
4. El backend real emite `connecting` y el manager lo mapeaba a `ERROR`: todo
   reinicio dejaba la fila en `error` hasta conectar (y un reinicio que cae
   antes de conectar la deja así), que alimentaba el punto 1.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.waweb import manager as manager_mod
from core.waweb import status
from core.waweb.manager import WaWebConnector
from core.waweb.policy import WaWebPolicy
from core.waweb.registry import SessionContext

TENANT = uuid.UUID(int=1)
BLOB_VINCULADO = "CREDS-VINCULADAS"
ESQUEMA_VACIO = "ESQUEMA-VACIO"


class StoreDurable:
    """Store con blob en memoria que imita al real: `materialize` escribe el blob
    en `session.db`, y `persist` guarda lo que haya en ese archivo (o un esquema
    vacío si Neonize lo creó sin dispositivo)."""

    def __init__(self, base: Path, blob: str | None) -> None:
        self.base = base
        self.blob = blob
        self.cleared: list[uuid.UUID] = []
        self.persisted: list[uuid.UUID] = []

    def work_dir(self, tenant_id: uuid.UUID) -> Path:
        return self.base / str(tenant_id)

    async def materialize(self, tenant_id: uuid.UUID, work_dir: Path) -> bool:
        if self.blob is None:
            return False
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / "session.db").write_text(self.blob)
        return True

    async def persist(self, tenant_id: uuid.UUID, work_dir: Path) -> None:
        self.persisted.append(tenant_id)
        db = work_dir / "session.db"
        self.blob = db.read_text() if db.exists() else None

    async def clear(self, tenant_id: uuid.UUID) -> None:
        self.cleared.append(tenant_id)
        self.blob = None


class RegistroDurable:
    """Imita `DbSessionRegistry`: la fila sobrevive entre "procesos" y el contexto
    se arma con datos durables, como `active_sessions` real."""

    def __init__(self, store: StoreDurable, *, estado: str, conectada_alguna_vez: bool) -> None:
        self.store = store
        self.estado = estado
        self.conectada_alguna_vez = conectada_alguna_vez
        self.last_error: str | None = None
        self.historial: list[tuple[str, str | None]] = []

    async def active_sessions(self) -> list[SessionContext]:
        return [
            SessionContext(
                tenant_id=TENANT,
                connection_id=uuid.UUID(int=2),
                connection_external_id="waweb:1",
                status=self.estado,
                linked_before=self.conectada_alguna_vez and self.store.blob is not None,
            )
        ]

    async def set_status(self, tenant_id, new_status, *, detail=None, jid=None):
        self.estado = new_status
        self.historial.append((new_status, detail))
        if detail is not None:
            self.last_error = detail or None
        if new_status == status.CONNECTED:
            self.conectada_alguna_vez = True
            self.last_error = None

    async def tenants_with_jid(self, jid: str) -> list[uuid.UUID]:
        return []


class SocketNeonize:
    """Como el real: abre `session.db` (lo crea vacío si no había) y luego `script`."""

    def __init__(self, script) -> None:  # type: ignore[no-untyped-def]
        self.closed = asyncio.Event()
        self._script = script
        self.callbacks: dict[str, Any] = {}

    async def connect(self, *, session_dir, on_qr, on_status, on_message):
        session_dir.mkdir(parents=True, exist_ok=True)
        db = session_dir / "session.db"
        if not db.exists():
            db.write_text(ESQUEMA_VACIO)
        self.callbacks = {"qr": on_qr, "status": on_status, "message": on_message}
        await self._script(self, on_qr, on_status)

    async def send(self, *, to, text, image_url=None):
        return "WA-1"

    async def alive(self):
        return True

    async def disconnect(self):
        self.closed.set()


async def _solo_connecting(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
    await on_status("connecting", None)


async def _connecting_y_colgado(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
    await on_status("connecting", None)
    await asyncio.sleep(3600)  # el proceso muere aquí (despliegue / SIGKILL)


async def _mudo(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
    """Login que expiró: ni `connected` ni QR (el `Login event: timeout` de test)."""


async def _un_qr(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
    await on_qr("QR-1")


async def _conecta(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
    await on_status("connecting", None)
    await on_status("connected", None)


class Reloj:
    def __init__(self) -> None:
        self.t = 0.0


class Escenario:
    """Connector con todo inyectado y un reloj que avanza con cada `sleep`."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        estado: str,
        conectada_alguna_vez: bool,
        blob: str | None,
        script=_mudo,  # type: ignore[no-untyped-def]
        ciclos: int = 1,
        drain=None,  # type: ignore[no-untyped-def]
        **ajustes: Any,
    ) -> None:
        self.reloj = Reloj()
        self.store = StoreDurable(tmp_path, blob)
        self.registro = RegistroDurable(
            self.store, estado=estado, conectada_alguna_vez=conectada_alguna_vez
        )
        self.alertas: list[tuple[tuple, dict]] = []
        self.sockets: list[SocketNeonize] = []
        self._script = script
        self._ciclos = ciclos

        async def _sleep(segundos: float) -> None:
            self.reloj.t += max(segundos, 0.05)
            await asyncio.sleep(0)

        async def _noop_drain(*a: Any, **k: Any) -> bool:
            return False

        async def _alert(*a: Any, **k: Any) -> None:
            self.alertas.append((a, k))

        async def _policy(_tenant_id: uuid.UUID) -> WaWebPolicy:
            return WaWebPolicy()

        self.connector = WaWebConnector(
            registry=self.registro,  # type: ignore[arg-type]
            store=self.store,  # type: ignore[arg-type]
            socket_factory=self._fabrica,
            policy_loader=_policy,
            drainer=drain or _noop_drain,
            sleep=_sleep,
            alert=_alert,
            now=lambda: self.reloj.t,
            pairing_timeout_s=ajustes.pop("pairing_timeout_s", 0.2),
            **ajustes,
        )

    def _fabrica(self) -> SocketNeonize:
        sock = SocketNeonize(self._script)
        self.sockets.append(sock)
        if len(self.sockets) >= self._ciclos:
            self.connector._stop.add(TENANT)
        return sock

    async def correr(self) -> SessionContext:
        """Un 'arranque del proceso': el contexto sale del registro durable."""
        (ctx,) = await self.registro.active_sessions()
        await asyncio.wait_for(self.connector.run_session(ctx), timeout=3)
        return ctx


@pytest.fixture(autouse=True)
def _sin_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop(*a: Any, **k: Any) -> None:
        return None

    async def _sin_reset(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(manager_mod.handoff, "set_ready", _noop)
    monkeypatch.setattr(manager_mod.handoff, "clear_ready", _noop)
    monkeypatch.setattr(manager_mod.handoff, "publish_qr", _noop)
    monkeypatch.setattr(manager_mod.handoff, "consume_reset", _sin_reset)
    monkeypatch.setattr(manager_mod.limits, "clear_disconnects", _noop)
    monkeypatch.setattr(manager_mod, "purge_outbox", _noop)

    async def _record(_: uuid.UUID) -> int:
        return 1

    monkeypatch.setattr(manager_mod.limits, "record_disconnect", _record)


class _LogQueAnota:
    """Sustituto del logger del módulo (structlog cachea el logger)."""

    def __init__(self, eventos: list[dict]) -> None:
        self._eventos = eventos

    def __getattr__(self, nivel: str):  # type: ignore[no-untyped-def]
        def anotar(event: str, **kw: Any) -> None:
            self._eventos.append({"event": event, "log_level": nivel, **kw})

        return anotar


# --- G/A: no se limpia una sesión que alguna vez se vinculó ----------------------------


@pytest.mark.parametrize("estado", [status.ERROR, status.PENDING_QR])
async def test_reinicio_con_sesion_vinculada_en_error_o_pending_qr_no_borra_el_blob(
    tmp_path: Path, estado: str
) -> None:
    """Hipótesis del incidente: la sesión estuvo `connected`, la fila quedó en
    `error`/`pending_qr` (falla de Redis, `banned`, `ConnectFailureEv` en el solape de
    contenedores) y el siguiente arranque —con `_linked` vacío— le borraba el blob."""
    esc = Escenario(tmp_path, estado=estado, conectada_alguna_vez=True, blob=BLOB_VINCULADO)

    await esc.correr()

    assert esc.store.cleared == [], "una sesión que estuvo vinculada no se borra al iniciar"
    assert esc.store.blob == BLOB_VINCULADO


async def test_sesion_nunca_vinculada_si_se_limpia_al_iniciar(tmp_path: Path) -> None:
    """Contraste: sin `linked_before` el comportamiento de siempre se mantiene."""
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=ESQUEMA_VACIO
    )

    await esc.correr()

    assert esc.store.cleared == [TENANT]


async def test_cadena_del_incidente_conectada_reinicio_cae_antes_de_conectar_y_otro_reinicio(
    tmp_path: Path,
) -> None:
    """Estuvo `connected` → un despliegue la deja a medio conectar → otro despliegue.

    Proceso 1 emite `connecting` (como el backend real) y muere antes de `connected`
    (SIGKILL/cancelación). Proceso 2 lee la fila tal como quedó.
    """
    esc1 = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_connecting_y_colgado,
    )
    (ctx,) = await esc1.registro.active_sessions()
    tarea = asyncio.create_task(esc1.connector.run_session(ctx))
    for _ in range(50):
        await asyncio.sleep(0)
        if esc1.sockets and any(h[0] == status.CONNECTING for h in esc1.registro.historial):
            break
    tarea.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarea

    # Proceso 2: mismo registro/store durables, connector nuevo (`_linked` vacío).
    esc2 = Escenario(
        tmp_path, estado=esc1.registro.estado, conectada_alguna_vez=True, blob=esc1.store.blob,
        script=_conecta,
    )
    await esc2.correr()

    assert esc2.store.cleared == []
    assert esc2.store.blob == BLOB_VINCULADO


async def test_connecting_del_backend_no_deja_la_fila_en_error(tmp_path: Path) -> None:
    """`connecting` es un estado válido, no un error (antes caía al default `ERROR`)."""
    esc = Escenario(tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
                    script=_solo_connecting)

    await esc.correr()

    assert status.ERROR not in [h[0] for h in esc.registro.historial]
    assert esc.registro.estado != status.ERROR


async def test_watchdog_no_limpia_una_sesion_vinculada_que_no_logra_conectar(tmp_path: Path) -> None:
    """Sesión vinculada que reanuda pero no llega a `connected` (login en timeout):
    el watchdog recicla, pero nunca puede borrar las credenciales guardadas."""
    esc = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_mudo, ciclos=5, pairing_reset_after=2,
    )

    await esc.correr()

    assert len(esc.sockets) == 5, "debe seguir reciclando"
    assert esc.store.cleared == []
    assert esc.store.blob == BLOB_VINCULADO


async def test_watchdog_si_limpia_una_sesion_nunca_vinculada(tmp_path: Path) -> None:
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_mudo, ciclos=3, pairing_reset_after=2,
    )

    await esc.correr()

    assert esc.store.cleared.count(TENANT) >= 2


async def test_cada_limpieza_se_registra_con_su_motivo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    eventos: list[dict] = []
    monkeypatch.setattr(manager_mod, "log", _LogQueAnota(eventos))
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_mudo, ciclos=3, pairing_reset_after=2,
    )

    await esc.correr()

    limpiezas = [e for e in eventos if e["event"] == "waweb_session_cleared"]
    assert {e["motivo"] for e in limpiezas} >= {"nunca_vinculada", "watchdog_sin_qr"}
    assert all(e["tenant_id"] == str(TENANT) for e in limpiezas)


# --- B: persist no pisa credenciales con un directorio sin dispositivo ------------------


async def test_no_persiste_un_directorio_sin_dispositivo_tras_reiniciar_una_sesion_perdida(
    tmp_path: Path,
) -> None:
    """Sesión que nunca llegó a vincularse en esta tarea y sin blob que restaurar: el
    `session.db` vacío que crea Neonize no debe guardarse como "sesión"."""
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None, script=_un_qr
    )

    await esc.correr()

    assert esc.store.persisted == []
    assert esc.store.blob is None


async def test_no_persiste_periodicamente_mientras_espera_el_qr(tmp_path: Path) -> None:
    vueltas = {"n": 0}
    holder: dict[str, Any] = {}

    async def _drain(*a: Any, **k: Any) -> bool:
        vueltas["n"] += 1
        if vueltas["n"] >= 45:
            holder["esc"].connector._stop.add(TENANT)
        return False

    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_un_qr, drain=_drain, ciclos=99, pairing_timeout_s=10_000,
    )
    holder["esc"] = esc

    await esc.correr()

    assert esc.store.persisted == []


async def test_si_persiste_cuando_la_sesion_se_vincula(tmp_path: Path) -> None:
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None, script=_conecta
    )
    registrar = esc.registro.set_status

    async def parar_al_conectar(tenant_id, new_status, **kw):  # type: ignore[no-untyped-def]
        await registrar(tenant_id, new_status, **kw)
        if new_status == status.CONNECTED:
            esc.connector._stop.add(tenant_id)

    esc.registro.set_status = parar_al_conectar  # type: ignore[method-assign]

    await esc.correr()

    assert esc.store.persisted == [TENANT]
    assert esc.store.blob == ESQUEMA_VACIO  # en el test el "dispositivo" es el archivo


async def test_persiste_la_sesion_vinculada_restaurada_aunque_no_reconecte(tmp_path: Path) -> None:
    """Con blob restaurado el directorio es el de la sesión vinculada: se guarda."""
    esc = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_mudo,
    )

    await esc.correr()

    assert esc.store.persisted == [TENANT]
    assert esc.store.blob == BLOB_VINCULADO


# --- D: `ready` solo con la sesión conectada -------------------------------------------


async def test_set_ready_solo_si_la_sesion_esta_conectada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas: list[str] = []

    async def _ready(tenant_id: str, *a: Any, **k: Any) -> None:
        llamadas.append(tenant_id)

    monkeypatch.setattr(manager_mod.handoff, "set_ready", _ready)
    vueltas = {"n": 0}
    holder: dict[str, Any] = {}

    async def _drain(*a: Any, **k: Any) -> bool:
        vueltas["n"] += 1
        if vueltas["n"] >= 45:
            holder["esc"].connector._stop.add(TENANT)
        return False

    esc = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_mudo, drain=_drain, ciclos=99, pairing_timeout_s=10_000,
    )
    holder["esc"] = esc

    await esc.correr()

    assert llamadas == [], "sin conexión no debe refrescarse `waweb:ready`"


async def test_set_ready_se_refresca_con_la_sesion_conectada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llamadas: list[str] = []

    async def _ready(tenant_id: str, *a: Any, **k: Any) -> None:
        llamadas.append(tenant_id)

    monkeypatch.setattr(manager_mod.handoff, "set_ready", _ready)
    vueltas = {"n": 0}
    holder: dict[str, Any] = {}

    async def _drain(*a: Any, **k: Any) -> bool:
        vueltas["n"] += 1
        if vueltas["n"] >= 45:
            holder["esc"].connector._stop.add(TENANT)
        return False

    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_conecta, drain=_drain, ciclos=99,
    )
    holder["esc"] = esc

    await esc.correr()

    # Una vez al conectar + el refresco periódico (cada 20 vueltas ociosas).
    assert len(llamadas) >= 3


# --- C: QR vencido -----------------------------------------------------------------------


async def test_qr_vencido_sin_conectar_recicla_para_emitir_uno_nuevo(tmp_path: Path) -> None:
    """Tras `Login event: timeout` Neonize deja de emitir: el socket que ya emitió un QR
    y lleva más de `qr_expired_s` en silencio se recicla (antes quedaba mudo para siempre)."""
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_un_qr, ciclos=2, pairing_timeout_s=0.2, qr_expired_s=1.0,
    )

    await esc.correr()

    assert len(esc.sockets) == 2
    assert esc.sockets[0].closed.is_set()


async def test_qr_vencido_en_sesion_vinculada_recicla_sin_borrar_credenciales(tmp_path: Path) -> None:
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_un_qr, ciclos=3, pairing_timeout_s=0.2, qr_expired_s=1.0,
    )

    await esc.correr()

    assert len(esc.sockets) == 3
    assert esc.store.cleared == []
    assert esc.store.blob == BLOB_VINCULADO


async def test_qr_que_rota_a_tiempo_no_se_recicla(tmp_path: Path) -> None:
    """Anti-churn: whatsmeow rota el QR cada ~20 s (el primero dura 60 s); mientras siga
    emitiendo, esperar el escaneo no debe reiniciar el vínculo."""
    holder: dict[str, Any] = {}
    vueltas = {"n": 0}

    async def _drain(*a: Any, **k: Any) -> bool:
        vueltas["n"] += 1
        sock = holder["esc"].sockets[-1]
        if vueltas["n"] % 3 == 0:
            await sock.callbacks["qr"](f"QR-{vueltas['n']}")
        if vueltas["n"] >= 60:
            holder["esc"].connector._stop.add(TENANT)
        return False

    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None,
        script=_un_qr, drain=_drain, ciclos=99, pairing_timeout_s=0.2, qr_expired_s=1.0,
    )
    holder["esc"] = esc

    await esc.correr()

    assert len(esc.sockets) == 1


# --- E: visibilidad ----------------------------------------------------------------------


async def test_sesion_vinculada_que_pide_qr_queda_visible_y_alerta_una_vez(tmp_path: Path) -> None:
    vueltas = {"n": 0}
    holder: dict[str, Any] = {}

    async def _drain(*a: Any, **k: Any) -> bool:
        vueltas["n"] += 1
        sock = holder["esc"].sockets[-1]
        if vueltas["n"] in (2, 4, 6):
            await sock.callbacks["qr"](f"QR-{vueltas['n']}")  # el QR rota
        if vueltas["n"] >= 8:
            holder["esc"].connector._stop.add(TENANT)
        return False

    esc = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_un_qr, drain=_drain, ciclos=99, pairing_timeout_s=10_000,
    )
    holder["esc"] = esc

    await esc.correr()

    assert "Sesión perdida" in (esc.registro.last_error or "")
    assert "re-vincular" in (esc.registro.last_error or "")
    perdidas = [a for a in esc.alertas if "perdi" in (a[0][0] + a[0][1]).lower()]
    assert len(perdidas) == 1, "los QR rotan cada ~20 s: una sola alerta por tarea"


async def test_primer_qr_de_una_sesion_nueva_no_es_sesion_perdida(tmp_path: Path) -> None:
    esc = Escenario(
        tmp_path, estado=status.PENDING_QR, conectada_alguna_vez=False, blob=None, script=_un_qr
    )

    await esc.correr()

    assert esc.alertas == []
    assert not esc.registro.last_error


async def test_logged_out_no_vuelve_a_guardar_las_credenciales_revocadas(tmp_path: Path) -> None:
    async def _logged_out(sock, on_qr, on_status) -> None:  # type: ignore[no-untyped-def]
        await on_status("logged_out", "cerró la sesión")

    esc = Escenario(
        tmp_path, estado=status.CONNECTED, conectada_alguna_vez=True, blob=BLOB_VINCULADO,
        script=_logged_out,
    )

    await esc.correr()

    assert esc.store.cleared == [TENANT]
    assert esc.store.persisted == []
    assert esc.store.blob is None
