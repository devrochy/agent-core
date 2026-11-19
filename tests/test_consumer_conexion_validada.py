"""ADR-0186 (revisión del PR-4): el consumer usa la conexión que el webhook ya validó y, para Twilio, no crea una
conversación sin conexión. Y `publish_inbound_message` libera el claim si el encolado falla."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.orchestrator import consumer


class _Sesion:
    def __init__(self, fila):
        self.fila = fila
        self.sql = None

    async def execute(self, stmt):
        self.sql = str(stmt)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: self.fila))


async def test_conexion_validada_filtra_negocio_y_proveedor():
    fila = object()
    sesion = _Sesion(fila)
    tid = uuid.uuid4()
    cid = uuid.uuid4()
    assert (
        await consumer._conexion_validada(
            sesion, tid, {"connection_id": str(cid), "provider": "twilio"}
        )
        is fila
    )
    assert "tenant_id" in sesion.sql and "provider" in sesion.sql
    assert await consumer._conexion_validada(sesion, tid, {"provider": "twilio"}) is None
    assert await consumer._conexion_validada(sesion, tid, {"connection_id": "no-es-uuid"}) is None
    assert await consumer._conexion_validada(sesion, None, {"connection_id": str(cid)}) is None


async def test_publicar_libera_el_claim_si_falla(monkeypatch):
    from core.gateway import webhooks

    liberados: list[str] = []

    async def _claim(*_a):
        return True

    async def _liberar(_t, _p, sid):
        liberados.append(sid)

    async def _publicar(*_a):
        raise ConnectionError("redis caído")

    monkeypatch.setattr(webhooks, "claim_message_once", _claim)
    monkeypatch.setattr("core.gateway.idempotency.release_message_claim", _liberar)
    monkeypatch.setattr(webhooks, "_publicar", _publicar)
    mensaje = SimpleNamespace(provider="twilio", external_message_id="SM1", media=[])
    with pytest.raises(ConnectionError):
        await webhooks.publish_inbound_message(uuid.uuid4(), mensaje)
    assert liberados == ["SM1"]
