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


def _send_one_fixture(
    monkeypatch, *, earliest=True, claim=True, adapter=None, channel="whatsapp", sender_type="agent", window=None
):
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
        template_name=None, template_language=None, template_components=None, interactive=None,
        sender_type=sender_type,
    )
    conv = SimpleNamespace(
        connection_id=None, meta_connection_id=None, customer_id=uuid.uuid4(), channel=channel,
        service_window_expires_at=window,
    )
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
    monkeypatch.setattr(mod, "outbound_adapter_for", lambda provider, channel=None: adapter)

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


def test_policy_no_usa_el_backoff_normal_y_se_difiere():
    """Un rechazo por política anti-baneo (tope/pausa de waweb) no es un
    fallo de envío: nunca cae en fail_permanent (que dispara DLQ y escalación),
    ni siquiera con los reintentos agotados."""
    from core.gateway.meta_errors import GraphError
    from core.tasks.outbound_send import decide_retry

    exc = GraphError(
        "envíos pausados o tope diario alcanzado", category="POLICY", is_transient=True
    )
    assert decide_retry(exc, attempts_made=0).action == "defer"
    assert decide_retry(exc, attempts_made=MAX_ATTEMPTS).action == "defer"


@pytest.mark.asyncio
async def test_send_one_policy_bloqueado_deja_el_mensaje_en_cola(monkeypatch):
    """Tope/pausa: el mensaje queda `queued` para reprogramarse y el error
    viaja clasificado como POLICY, no como fallo transitorio."""
    from types import SimpleNamespace

    from core.gateway.meta_errors import GraphError
    from core.tasks.outbound_send import _send_one
    from core.waweb.handoff import WaWebPolicyBlocked

    async def blocked(*a, **k):
        raise WaWebPolicyBlocked("envíos pausados o tope diario alcanzado")

    adapter = SimpleNamespace(send_text=blocked, send_image=blocked)
    msg, tenant_id, calls = _send_one_fixture(monkeypatch, adapter=adapter)

    result = await _send_one(msg.id, tenant_id)
    assert isinstance(result, GraphError)
    assert result.category == "POLICY"
    assert msg.status == "queued"
    assert calls["release"] == 1
    assert calls["next"] == []


@pytest.mark.asyncio
async def test_handle_policy_defer_reprograma_con_eta(monkeypatch):
    """El mensaje bloqueado se reprograma (misma tarea, nueva ETA) sin tocar
    su estado ni escalar la conversación."""
    import uuid
    from datetime import datetime

    import core.tasks.outbound_send as mod
    from core.gateway.meta_errors import GraphError

    reprogramados = []

    class FakeTask:
        def apply_async(self, args, eta=None, **kw):
            reprogramados.append({"args": args, "eta": eta})

    fijo = datetime(2026, 10, 8, 0, 2, tzinfo=UTC)
    monkeypatch.setattr(mod, "outbound_send_task", FakeTask())

    async def fake_bump(_message_id):
        return 1

    async def fake_eta(_tenant_id):
        return fijo

    monkeypatch.setattr(mod, "_bump_policy_deferrals", fake_bump)
    monkeypatch.setattr(mod, "_policy_eta", fake_eta)

    msg_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    await mod._handle_policy_defer(
        msg_id, tenant_id, GraphError("tope", category="POLICY", is_transient=True)
    )

    assert reprogramados == [{"args": [str(msg_id), str(tenant_id)], "eta": fijo}]


@pytest.mark.asyncio
async def test_handle_policy_defer_agotado_audita_y_no_reprograma(monkeypatch):
    """Tras 24 h de bloqueo se deja constancia en auditoría del negocio y no
    se siguen encolando tareas."""
    import uuid

    import core.tasks.outbound_send as mod
    from core.gateway.meta_errors import GraphError
    from core.tasks.outbound_send import MAX_POLICY_DEFERRALS

    reprogramados = []
    auditorias = []

    class FakeTask:
        def apply_async(self, *a, **k):
            reprogramados.append(1)

    async def fake_bump(_message_id):
        return MAX_POLICY_DEFERRALS + 1

    async def fake_audit(**kw):
        auditorias.append(kw)

    monkeypatch.setattr(mod, "outbound_send_task", FakeTask())
    monkeypatch.setattr(mod, "_bump_policy_deferrals", fake_bump)
    monkeypatch.setattr(mod, "record_audit", fake_audit)

    msg_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    await mod._handle_policy_defer(
        msg_id, tenant_id, GraphError("tope", category="POLICY", is_transient=True)
    )

    assert reprogramados == []
    assert auditorias[0]["action"] == "outbound_bloqueado_por_politica"


@pytest.mark.asyncio
async def test_send_one_plantilla_usa_send_template(monkeypatch):
    """HU-ARCH-13: un outbound con `template_name` sale por `send_template` con
    el payload ya resuelto, no como texto."""
    from types import SimpleNamespace

    from core.tasks.outbound_send import _send_one

    enviados: list = []

    async def _template(credentials, external_id, name, language, components):
        enviados.append((external_id, name, language, components))
        return "wamid-plantilla"

    async def _no_texto(*a, **k):
        raise AssertionError("una plantilla no sale como texto")

    adapter = SimpleNamespace(send_text=_no_texto, send_image=_no_texto, send_template=_template)
    msg, tenant_id, _calls = _send_one_fixture(monkeypatch, adapter=adapter)
    msg.template_name = "aviso_cobro"
    msg.template_language = "es"
    msg.template_components = [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}]

    assert await _send_one(msg.id, tenant_id) is None
    assert msg.status == "sent"
    assert msg.external_message_id == "wamid-plantilla"
    assert enviados == [
        ("whatsapp:+573001234567", "aviso_cobro", "es", msg.template_components)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("sender_type", "humano"), [("agent", "false"), ("human", "true")])
async def test_send_one_de_messenger_pasa_ventana_y_autoria_al_adaptador(monkeypatch, sender_type, humano):
    """Messenger decide `RESPONSE` o `HUMAN_AGENT` con la ventana y la autoría del mensaje (ADR-0152);
    el adaptador no ve la conversación ni el mensaje, así que viajan en las credenciales."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    from core.tasks.outbound_send import _send_one

    vence = datetime.now(UTC) - timedelta(days=1)
    visto = {}

    async def send_text(credentials, external_id, content):
        visto.update(credentials)
        return "mid-1"

    adapter = SimpleNamespace(send_text=send_text)
    msg, tenant_id, _calls = _send_one_fixture(
        monkeypatch, adapter=adapter, channel="messenger", sender_type=sender_type, window=vence
    )

    assert await _send_one(msg.id, tenant_id) is None
    assert visto["service_window_expires_at"] == vence.isoformat()
    assert visto["human"] == humano
    assert visto["access_token"] == "tok"  # lo demás de las credenciales no se pierde


@pytest.mark.asyncio
async def test_send_one_de_whatsapp_no_agrega_datos_de_messenger(monkeypatch):
    from types import SimpleNamespace

    from core.tasks.outbound_send import _send_one

    visto = {}

    async def send_text(credentials, external_id, content):
        visto.update(credentials)
        return "wamid-1"

    msg, tenant_id, _calls = _send_one_fixture(monkeypatch, adapter=SimpleNamespace(send_text=send_text))

    assert await _send_one(msg.id, tenant_id) is None
    assert "human" not in visto and "service_window_expires_at" not in visto


@pytest.mark.asyncio
async def test_send_one_de_un_canal_sin_adaptador_falla_permanente_y_libera_el_claim(monkeypatch):
    """Instagram, hasta HU-CHAN-04: no cae al adaptador de WhatsApp ni reintenta."""
    import core.tasks.outbound_send as mod
    from core.gateway.meta_errors import GraphError

    msg, tenant_id, calls = _send_one_fixture(monkeypatch, channel="instagram")

    def sin_adaptador(provider, channel=None):
        raise KeyError(f"Sin adaptador de salida para {provider}/{channel}")

    monkeypatch.setattr(mod, "outbound_adapter_for", sin_adaptador)

    result = await mod._send_one(msg.id, tenant_id)

    assert isinstance(result, GraphError) and result.category == "PERMANENT"
    assert "meta/instagram" in str(result)
    assert calls["release"] == 1 and calls["next"] == []
