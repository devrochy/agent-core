"""`NeonizeSocket` serializa las llamadas al puente Go de Neonize.

Regresión real (2026-09-30): con dos negocios vinculando a la vez en el mismo
conector, Neonize reventaba el proceso con `fatal error: concurrent map writes`
(data race del lado Go) y el contenedor entraba en bucle de reinicios: ningún
negocio llegaba a ver el QR.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from core.waweb import backend_neonize


class _EventRegistrar:
    def __call__(self, _tipo: object):
        def _wrap(fn):
            return fn

        return _wrap

    def qr(self, fn):
        return fn


class FakeClient:
    """Cliente falso que detecta solapamiento en la zona crítica."""

    vivos = 0
    max_concurrentes = 0

    def __init__(self, _ruta: str) -> None:
        FakeClient.vivos += 1
        FakeClient.max_concurrentes = max(FakeClient.max_concurrentes, FakeClient.vivos)
        self.event = _EventRegistrar()

    async def connect(self) -> None:
        await asyncio.sleep(0.05)
        FakeClient.vivos -= 1

    async def idle(self) -> None:
        await asyncio.sleep(3600)

    async def send_message(self, _jid: str, _message: object) -> object:
        return types.SimpleNamespace(ID="WAMID-1")

    async def disconnect(self) -> None:
        return None

    # Como en `neonize.aioze` 0.5.2: las propiedades devuelven una corutina
    # (`asyncio.to_thread` sobre el puente Go), no un bool.
    conectado = True
    autenticado = True

    demora_s = 0.0

    @property
    def is_connected(self):
        async def _valor() -> bool:
            await asyncio.sleep(self.demora_s)
            return self.conectado

        return _valor()

    @property
    def is_logged_in(self):
        async def _valor() -> bool:
            return self.autenticado

        return _valor()


@pytest.fixture(autouse=True)
def _fake_neonize(monkeypatch: pytest.MonkeyPatch) -> None:
    ev = types.SimpleNamespace(
        ConnectedEv=object(),
        PairStatusEv=object(),
        DisconnectedEv=object(),
        LoggedOutEv=object(),
        TemporaryBanEv=object(),
        ConnectFailureEv=object(),
        MessageEv=object(),
    )
    monkeypatch.setattr(backend_neonize, "_import_neonize", lambda: (FakeClient, ev))
    modulo_jid = types.ModuleType("neonize.utils.jid")
    modulo_jid.jid_is_lid = lambda _jid: False  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize.utils.jid", modulo_jid)

    modulo_proto = types.ModuleType("neonize.proto.waE2E.WAWebProtobufsE2E_pb2")

    class _Message:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    modulo_proto.Message = _Message  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "neonize.proto.waE2E.WAWebProtobufsE2E_pb2", modulo_proto)

    FakeClient.vivos = 0
    FakeClient.max_concurrentes = 0


async def _noop(*_a: object, **_k: object) -> None:
    return None


async def test_dos_vinculaciones_a_la_vez_no_solapan_el_puente(tmp_path: Path) -> None:
    primera = backend_neonize.NeonizeSocket()
    segunda = backend_neonize.NeonizeSocket()

    await asyncio.gather(
        primera.connect(
            session_dir=tmp_path / "a", on_qr=_noop, on_status=_noop, on_message=_noop
        ),
        segunda.connect(
            session_dir=tmp_path / "b", on_qr=_noop, on_status=_noop, on_message=_noop
        ),
    )

    assert FakeClient.max_concurrentes == 1, "el puente Go nunca debe usarse en paralelo"

    await primera.disconnect()
    await segunda.disconnect()


async def test_send_serializado_y_devuelve_wamid(tmp_path: Path) -> None:
    socket = backend_neonize.NeonizeSocket()
    await socket.connect(session_dir=tmp_path / "c", on_qr=_noop, on_status=_noop, on_message=_noop)

    wamid = await socket.send(to="57300", text="hola")

    assert wamid == "WAMID-1"
    await socket.disconnect()


async def _conectado(tmp_path: Path) -> backend_neonize.NeonizeSocket:
    socket = backend_neonize.NeonizeSocket()
    await socket.connect(session_dir=tmp_path / "v", on_qr=_noop, on_status=_noop, on_message=_noop)
    return socket


async def test_alive_espera_las_propiedades_del_cliente(tmp_path: Path) -> None:
    """`is_connected` es una corutina en aioze: sin `await` daría siempre True."""
    socket = await _conectado(tmp_path)
    assert await socket.alive() is True

    socket._client.conectado = False
    assert await socket.alive() is False

    socket._client.conectado = True
    socket._client.autenticado = False
    assert await socket.alive() is False
    await socket.disconnect()


async def test_alive_sin_cliente_o_con_idle_terminado_es_false(tmp_path: Path) -> None:
    assert await backend_neonize.NeonizeSocket().alive() is False

    socket = await _conectado(tmp_path)
    socket._idle_task.cancel()
    await asyncio.sleep(0)
    assert await socket.alive() is False
    await socket.disconnect()


async def test_alive_con_puente_ocupado_no_declara_muerto(tmp_path: Path) -> None:
    """Esperar el lock (otro tenant usando el puente) no es evidencia de muerte."""
    socket = await _conectado(tmp_path)
    socket._client.conectado = False
    socket.alive_timeout_s = 0.05

    async with backend_neonize._BRIDGE_LOCK:
        assert await socket.alive() is True
    await socket.disconnect()


async def test_alive_no_cancela_una_llamada_al_puente_en_curso(tmp_path: Path) -> None:
    """El tope solo aplica a esperar el lock: una llamada lenta a Go no se corta
    (cortarla soltaría el lock con el hilo aún dentro del puente)."""
    socket = await _conectado(tmp_path)
    socket._client.conectado = False
    socket._client.demora_s = 0.1
    socket.alive_timeout_s = 0.02

    assert await socket.alive() is False
    assert not backend_neonize._BRIDGE_LOCK.locked()
    await socket.disconnect()
