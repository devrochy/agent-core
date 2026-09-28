"""`decide_retry`: rate limit usa `estimated_time_to_regain_access` tal
cual (nunca el backoff genérico); 131047 y AUTH nunca reintentan;
reintentos agotados -> fail_permanent (dispara DLQ)."""

from __future__ import annotations

from datetime import UTC

import pytest

from core.gateway.meta_errors import REENGAGEMENT_WINDOW_CODE, GraphError
from core.tasks.outbound_send import MAX_ATTEMPTS, decide_retry


def _rate_limit_error(retry_after_ms: int) -> GraphError:
    return GraphError(
        "rate limit", category="RATE_LIMIT", code=130429, is_transient=True, retry_after_ms=retry_after_ms
    )


def test_rate_limit_reencola_con_el_delay_exacto_del_criterio_de_aceptacion():
    """estimated_time_to_regain_access=3 minutos -> 180000 ms."""
    exc = _rate_limit_error(retry_after_ms=3 * 60_000)
    decision = decide_retry(exc, attempts_made=0)
    assert decision.action == "retry"
    assert decision.countdown_s == 180.0


def test_reengagement_nunca_reintenta():
    exc = GraphError(
        "fuera de ventana", category="PERMANENT", code=REENGAGEMENT_WINDOW_CODE, is_transient=False
    )
    decision = decide_retry(exc, attempts_made=0)
    assert decision.action == "fail_permanent"


def test_auth_nunca_reintenta():
    exc = GraphError("credenciales inválidas", category="AUTH", code=190, is_transient=False)
    decision = decide_retry(exc, attempts_made=0)
    assert decision.action == "fail_permanent"


def test_rate_limit_con_reintentos_agotados_falla_permanente():
    exc = _rate_limit_error(retry_after_ms=60_000)
    decision = decide_retry(exc, attempts_made=MAX_ATTEMPTS)
    assert decision.action == "fail_permanent"


def test_transient_reintenta_con_backoff_exponencial_acotado():
    exc = GraphError("timeout", category="TRANSIENT", is_transient=True)
    d0 = decide_retry(exc, attempts_made=0)
    d1 = decide_retry(exc, attempts_made=1)
    assert d0.action == "retry"
    assert d1.action == "retry"
    assert d1.countdown_s > d0.countdown_s
    assert d1.countdown_s <= 900.0


@pytest.mark.asyncio
async def test_send_one_no_op_si_mensaje_no_esta_en_queued(monkeypatch):
    """Protección extra de idempotencia: si la fila ya está en un estado
    terminal, la tarea no vuelve a llamar a Meta."""
    import uuid
    from types import SimpleNamespace

    from core.tasks.outbound_send import _send_one

    row = SimpleNamespace(status="sent")

    class FakeSession:
        async def get(self, model, id_):
            return row

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import core.tasks.outbound_send as mod

    monkeypatch.setattr(mod, "get_session", lambda: FakeSession())

    async def _debe_no_llamarse(*a, **k):
        raise AssertionError("no debe resolver credenciales para un mensaje ya terminal")

    monkeypatch.setattr(mod, "resolve_credentials", _debe_no_llamarse)

    result = await _send_one(uuid.uuid4(), uuid.uuid4())
    assert result is None


def _send_one_fixture(monkeypatch, *, earliest=True, claim=True, adapter=None):
    """Monta el FakeSession y mocks para `_send_one`; devuelve (msg, mocks)."""
    import uuid
    from datetime import datetime
    from types import SimpleNamespace

    import core.tasks.outbound_send as mod
    from core.db.models import Conversation, Customer, Message

    tenant_id = uuid.uuid4()
    msg = SimpleNamespace(
        id=uuid.uuid4(), tenant_id=tenant_id, status="queued",
        conversation_id=uuid.uuid4(), created_at=datetime.now(UTC),
        media_type=None, media_url=None, content="hola",
    )
    conv = SimpleNamespace(connection_id=None, meta_connection_id=None, customer_id=uuid.uuid4())
    cust = SimpleNamespace(external_id="whatsapp:+573001234567")

    class FakeSession:
        async def get(self, model, id_):
            return {Message: msg, Conversation: conv, Customer: cust}.get(model)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    calls = {"release": 0, "next": [], "sent": []}

    async def fake_adapter_send_text(credentials, external_id, content):
        return "wamid-1"

    async def fake_adapter_send_image(*a):
        return "wamid-1"

    adapter = adapter or SimpleNamespace(
        send_text=fake_adapter_send_text, send_image=fake_adapter_send_image
    )

    async def fake_release(*a, **k):
        calls["release"] += 1

    async def fake_next(*a):
        calls["next"].append(a)

    async def fake_sent(*a, **k):
        calls["sent"].append(a)

    async def fake_earliest(*a, **k):
        return earliest

    async def fake_claim(*a, **k):
        return claim

    async def fake_credentials(*a, **k):
        return ("meta", {"phone_number_id": "pn", "access_token": "tok"})

    monkeypatch.setattr(mod, "get_session", lambda: FakeSession())
    monkeypatch.setattr(mod, "_is_earliest_queued", fake_earliest)
    monkeypatch.setattr(mod, "_claim_outbound", fake_claim)
    monkeypatch.setattr(mod, "_release_claim", fake_release)
    monkeypatch.setattr(mod, "_enqueue_next", fake_next)
    monkeypatch.setattr(mod, "resolve_credentials", fake_credentials)
    monkeypatch.setattr(mod, "outbound_adapter_for", lambda provider: adapter)

    return msg, tenant_id, calls


@pytest.mark.asyncio
async def test_send_one_no_op_si_no_es_el_mas_antiguo(monkeypatch):
    """Orden: un mensaje que no es el outbound 'queued' más antiguo de la
    conversación NO se envía (lo encadena el predecesor cuando termine)."""

    import core.tasks.outbound_send as mod
    from core.tasks.outbound_send import _send_one

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, earliest=False)

    async def _no(*a, **k):
        raise AssertionError("no debe resolver credenciales si no es el más antiguo")

    monkeypatch.setattr(mod, "resolve_credentials", _no)

    assert await _send_one(msg.id, tenant_id) is None
    assert calls["release"] == 0
    assert calls["next"] == []


@pytest.mark.asyncio
async def test_send_one_no_op_si_el_claim_no_se_obtiene(monkeypatch):
    """Claim: si otra tarea ya reclamó el mensaje (doble encolado/redelivery),
    esta no envía."""

    import core.tasks.outbound_send as mod
    from core.tasks.outbound_send import _send_one

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, claim=False)

    async def _no(*a, **k):
        raise AssertionError("no debe resolver credenciales si no obtuvo el claim")

    monkeypatch.setattr(mod, "resolve_credentials", _no)

    assert await _send_one(msg.id, tenant_id) is None
    assert calls["release"] == 0


@pytest.mark.asyncio
async def test_send_one_envia_y_encadena_al_siguiente(monkeypatch):
    """Éxito: marca 'sent', persiste el wamid y encadena el siguiente
    outbound 'queued' de la conversación."""

    from core.tasks.outbound_send import _send_one

    msg, tenant_id, calls = _send_one_fixture(monkeypatch)

    result = await _send_one(msg.id, tenant_id)
    assert result is None
    assert msg.status == "sent"
    assert msg.external_message_id == "wamid-1"
    assert calls["release"] == 1
    assert len(calls["next"]) == 1
    conv_id, tid, created_at, msg_id = calls["next"][0]
    assert conv_id == msg.conversation_id
    assert tid == tenant_id
    assert created_at == msg.created_at
    assert msg_id == msg.id


@pytest.mark.asyncio
async def test_send_one_error_libera_claim_y_no_encadena(monkeypatch):
    """Fallo transitorio: libera el claim (para el retry) y NO encadena el
    siguiente (el orden lo mantiene este mensaje hasta que se resuelva)."""
    from types import SimpleNamespace

    from core.gateway.meta_errors import GraphError
    from core.tasks.outbound_send import _send_one

    async def boom(*a, **k):
        raise GraphError("boom", category="TRANSIENT", is_transient=True)

    adapter = SimpleNamespace(send_text=boom, send_image=boom)
    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=adapter)

    result = await _send_one(msg.id, tenant_id)
    assert isinstance(result, GraphError)
    assert msg.status == "queued"  # sigue pendiente para el retry
    assert calls["release"] == 1
    assert calls["next"] == []


@pytest.mark.asyncio
async def test_claim_outbound_setnx_excluye_segunda_tarea(monkeypatch):
    """El claim es atómico: la primera tarea lo obtiene, la segunda no; al
    liberarlo se puede volver a reclamar."""
    import uuid

    from core.tasks.outbound_send import _claim_outbound, _release_claim

    class FakeRedis:
        def __init__(self):
            self.claimed = set()

        async def set(self, key, val, **kw):
            if key in self.claimed:
                return False
            self.claimed.add(key)
            return True

        async def delete(self, key):
            self.claimed.discard(key)

    fake = FakeRedis()
    monkeypatch.setattr("core.memory.working.get_redis", lambda: fake)

    mid, cid, tid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await _claim_outbound(mid, cid, tid) is True
    assert await _claim_outbound(mid, cid, tid) is False  # ya reclamado
    await _release_claim(mid, cid, tid)
    assert await _claim_outbound(mid, cid, tid) is True  # liberado -> reclama
