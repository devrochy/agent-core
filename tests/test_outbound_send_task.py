"""`decide_retry`: rate limit usa `estimated_time_to_regain_access` tal
cual (nunca el backoff genérico); 131047 y AUTH nunca reintentan;
reintentos agotados -> fail_permanent (dispara DLQ)."""

from __future__ import annotations

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
