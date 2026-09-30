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
