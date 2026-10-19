"""Guarda única de pausa (adenda 2026-10-19 de ADR-0122): conexión propia sin cuenta y cuenta de Meta.

Sin BD: la fila de la conexión y la cuenta se parchean. La integración está en
`tests/integration/test_pausa_conexion_propia.py`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core.gateway import meta_connection_state as mcs


def _conv(**kw):
    base = {"id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "channel": "whatsapp", "meta_connection_id": None}
    return SimpleNamespace(**{**base, **kw})


@pytest.fixture
def fila(monkeypatch):
    estado: dict = {"fila": None, "lecturas": 0}

    async def _fila(_conv):
        estado["lecturas"] += 1
        return estado["fila"]

    monkeypatch.setattr(mcs, "_fila_de_su_conexion", _fila)
    return estado


@pytest.fixture
def cuentas(monkeypatch):
    motivos: dict = {}

    async def _motivo(cuenta):
        return motivos.get(cuenta)

    monkeypatch.setattr(mcs, "motivo_pausa_de", _motivo)
    return motivos


@pytest.mark.parametrize(
    ("active", "deleted", "motivo"),
    [(True, False, None), (False, False, mcs.MOTIVO_CONEXION_PAUSADA), (True, True, mcs.MOTIVO_CONEXION_ELIMINADA)],
)
async def test_conexion_sin_cuenta_pausa_por_su_estado(fila, cuentas, active, deleted, motivo) -> None:
    cid = uuid.uuid4()
    fila["fila"] = mcs._FilaConexion(meta_connection_id=None, active=active, deleted=deleted)
    pausa = await mcs.pausa_de_conversacion(_conv(connection_id=cid))
    if motivo is None:
        assert pausa is None
    else:
        assert pausa == mcs.Pausa(motivo, "connection", cid)
    assert fila["lecturas"] == 1, "una sola lectura de la fila"


async def test_el_chat_web_desactivado_tambien_pausa(fila, cuentas) -> None:
    fila["fila"] = mcs._FilaConexion(meta_connection_id=None, active=False, deleted=False)
    assert await mcs.motivo_pausa_de_conversacion(_conv(channel="web", connection_id=uuid.uuid4())) == (
        mcs.MOTIVO_CONEXION_PAUSADA
    )


async def test_la_fila_canonica_de_meta_manda_su_cuenta(fila, cuentas) -> None:
    cuenta = uuid.uuid4()
    fila["fila"] = mcs._FilaConexion(meta_connection_id=cuenta, active=False, deleted=False)
    conv = _conv(connection_id=uuid.uuid4())
    assert await mcs.pausa_de_conversacion(conv) is None, "su `active` no cuenta"
    cuentas[cuenta] = mcs.MOTIVO_PAUSADA
    assert await mcs.pausa_de_conversacion(conv) == mcs.Pausa(mcs.MOTIVO_PAUSADA, "meta_account", cuenta)


async def test_fila_ilegible_no_pausa(fila, cuentas) -> None:
    fila["fila"] = None
    assert await mcs.pausa_de_conversacion(_conv(connection_id=uuid.uuid4(), meta_connection_id=uuid.uuid4())) is None


async def test_sin_conexion_usa_el_puente_de_la_cuenta(fila, cuentas) -> None:
    cuenta = uuid.uuid4()
    cuentas[cuenta] = mcs.MOTIVO_ELIMINADA
    assert await mcs.motivo_pausa_de_conversacion(_conv(connection_id=None, meta_connection_id=cuenta)) == (
        mcs.MOTIVO_ELIMINADA
    )
    assert await mcs.pausa_de_conversacion(_conv(channel="web", connection_id=None, meta_connection_id=cuenta)) is None
    assert fila["lecturas"] == 0


async def test_el_aviso_de_una_conexion_propia_es_generico(monkeypatch) -> None:
    import core.memory.working as working
    import core.tasks.push_tasks as push_tasks

    class _Redis:
        async def set(self, *_a, **_k):
            return True

    monkeypatch.setattr(working, "get_redis", lambda: _Redis())
    pushes: list = []
    monkeypatch.setattr(push_tasks.notify_owners_push, "delay", lambda *a, **_k: pushes.append(a))
    cid = uuid.uuid4()
    await mcs.avisar_mensaje_en_conexion_pausada(uuid.uuid4(), cid, mcs.MOTIVO_CONEXION_PAUSADA, alcance="connection")
    (_, titulo, _cuerpo, datos), = pushes
    assert titulo == mcs.TITULO_CONEXION_PROPIA_PAUSADA
    assert datos == {"type": "core.connection_paused", "connectionId": str(cid), "scope": "connection"}


@pytest.mark.parametrize(
    ("tiene_cuenta", "meta_status", "type_id", "esperado"),
    [
        (True, "connected", "whatsapp_meta", True),
        (True, None, "whatsapp_meta", False),
        (False, None, "whatsapp_meta", True),  # manual
        (False, None, "whatsapp_waweb", True),
        (False, None, "webchat", True),  # el mismo interruptor que Configuración → Chat web
    ],
)
def test_la_consola_ofrece_pausar_manuales_waweb_y_chat_web(tiene_cuenta, meta_status, type_id, esperado) -> None:
    from core.gateway.consola_conexiones import pausable

    assert pausable(tiene_cuenta=tiene_cuenta, meta_status=meta_status, type_id=type_id) is esperado
