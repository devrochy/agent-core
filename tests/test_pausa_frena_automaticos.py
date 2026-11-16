"""HU-ARCH-37: la pausa del negocio y del cliente frena lo automático hacia el cliente (D-76).

Unitarios por camino: el guardián del sender, los avisos por evento (sin reintentos), egress (sin escalar),
el correo saliente, los seguimientos de comercio y la distinción automático/persona. La integración con dos
negocios vive en `tests/integration/test_pausa_negocio_frena_automaticos.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import core.gateway.event_notices as notices
import core.gateway.sender as sender_mod
from core.gateway import pausa_automatica
from core.shared.tenant import tenant_scope

pytestmark = pytest.mark.pausa_real

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")


def _conversacion(**kw):
    base = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "customer_id": uuid.uuid4(),
        "connection_id": None,
        "meta_connection_id": uuid.uuid4(),
        "channel": "whatsapp",
        "bot_paused": False,
        "status": "open",
        "service_window_expires_at": datetime.now(UTC) + timedelta(hours=10),
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _Sesion:
    def __init__(self, conversation=None, customer=None) -> None:
        self.conversation = conversation
        self.customer = customer
        self.agregados: list = []

    async def get(self, model, id_):
        return self.customer if model.__name__ == "Customer" else self.conversation

    def add(self, obj) -> None:
        self.agregados.append(obj)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _con_pausa(monkeypatch: pytest.MonkeyPatch, motivo: str | None) -> None:
    async def _motivo(*_a, **_k):
        return motivo

    async def _negocio(*_a, **_k):
        return motivo == pausa_automatica.MOTIVO_NEGOCIO_PAUSADO

    monkeypatch.setattr(pausa_automatica, "motivo_pausa_automatica", _motivo)
    monkeypatch.setattr(pausa_automatica, "motivo_pausa_por_id", _motivo)
    monkeypatch.setattr(pausa_automatica, "negocio_pausado", _negocio)


# ---------------------------------------------------------------- automático o persona


@pytest.mark.parametrize(
    ("sender_type", "cmid", "automatico"),
    [
        ("agent", None, True),
        (None, None, True),
        ("human", None, False),
        ("human", "operador:123", False),
        ("human", f"segmento:{uuid.uuid4()}:{uuid.uuid4()}", True),
    ],
)
def test_es_envio_automatico(sender_type, cmid, automatico) -> None:
    assert pausa_automatica.es_envio_automatico(sender_type, cmid) is automatico


def test_el_prefijo_de_segmento_es_el_de_envios_segmento() -> None:
    from core.admin.envios_segmento import PREFIJO

    assert pausa_automatica._PREFIJO_SEGMENTO == f"{PREFIJO}:"


async def test_motivo_de_otro_negocio_no_decide(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_deberia(*_a, **_k):
        raise AssertionError("no se lee la pausa de otro negocio")

    monkeypatch.setattr(pausa_automatica, "negocio_pausado", _no_deberia)
    async with tenant_scope(uuid.uuid4()):
        assert await pausa_automatica.motivo_pausa_automatica(_conversacion()) is None


async def test_motivo_primero_el_negocio_despues_el_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion()
    cliente = SimpleNamespace(id=conv.customer_id, bot_paused=True)

    async def _negocio(_tid):
        return estado["negocio"]

    estado = {"negocio": True}
    monkeypatch.setattr(pausa_automatica, "negocio_pausado", _negocio)
    async with tenant_scope(TENANT):
        assert await pausa_automatica.motivo_pausa_automatica(conv, cliente) == "negocio_pausado"
        estado["negocio"] = False
        assert await pausa_automatica.motivo_pausa_automatica(conv, cliente) == "cliente_pausado"
        cliente.bot_paused = False
        assert await pausa_automatica.motivo_pausa_automatica(conv, cliente) is None


async def test_la_pausa_de_la_conversacion_sola_no_frena(monkeypatch: pytest.MonkeyPatch) -> None:
    """El escalamiento pausa la conversación justo antes de mandar su aviso: ese aviso tiene que salir."""
    conv = _conversacion(bot_paused=True)
    cliente = SimpleNamespace(id=conv.customer_id, bot_paused=False)

    async def _no(_tid):
        return False

    monkeypatch.setattr(pausa_automatica, "negocio_pausado", _no)
    async with tenant_scope(TENANT):
        assert await pausa_automatica.motivo_pausa_automatica(conv, cliente) is None


# ---------------------------------------------------------------- sender


async def test_send_to_conversation_frenado_deja_failed_y_no_encola(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion()
    sesion = _Sesion(conv, SimpleNamespace(id=conv.customer_id, bot_paused=False))
    monkeypatch.setattr(sender_mod, "get_session", lambda: sesion)
    _con_pausa(monkeypatch, "negocio_pausado")

    async def _no_deberia(*_a, **_k):
        raise AssertionError("no se llega a la pausa de la conexión ni al encolado")

    monkeypatch.setattr(sender_mod, "_registrar_si_pausada", _no_deberia)
    async with tenant_scope(TENANT):
        assert await sender_mod.send_to_conversation(conv.id, "hola") is False

    [msg] = sesion.agregados
    assert (msg.status, msg.error_reason, msg.sender_type) == ("failed", "negocio_pausado", "agent")


async def test_una_persona_pasa_el_guardian(monkeypatch: pytest.MonkeyPatch) -> None:
    _con_pausa(monkeypatch, "negocio_pausado")
    async with tenant_scope(TENANT):
        assert await sender_mod._registrar_si_frenada(_conversacion(), "hola", "human") is None


async def test_el_canal_de_prueba_tambien_se_frena(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion(channel="test")
    sesion = _Sesion(conv, SimpleNamespace(id=conv.customer_id, bot_paused=True))
    monkeypatch.setattr(sender_mod, "get_session", lambda: sesion)
    _con_pausa(monkeypatch, "cliente_pausado")

    async def _no_deberia(*_a, **_k):
        raise AssertionError("el Centro de pruebas también ve la pausa")

    monkeypatch.setattr(sender_mod, "_guardar_saliente_de_prueba", _no_deberia)
    async with tenant_scope(TENANT):
        assert await sender_mod.send_to_conversation(conv.id, "hola") is False
    assert sesion.agregados[0].error_reason == "cliente_pausado"


async def test_interactivo_frenado_se_registra_una_vez(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion()
    sesion = _Sesion(conv, SimpleNamespace(id=conv.customer_id, bot_paused=False))
    monkeypatch.setattr(sender_mod, "get_session", lambda: sesion)
    _con_pausa(monkeypatch, "negocio_pausado")
    interactivo = {"type": "button", "body": "¿Confirmas?", "buttons": [{"id": "si", "title": "Sí"}]}
    async with tenant_scope(TENANT):
        assert await sender_mod.send_interactive_to_conversation(conv.id, interactivo, fallback_text="¿Confirmas?") is False
    assert len(sesion.agregados) == 1


# ---------------------------------------------------------------- avisos por evento


async def test_aviso_con_negocio_pausado_no_sale_ni_se_reintenta(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion()
    sesion = _Sesion(conv)
    monkeypatch.setattr(notices, "get_session", lambda: sesion)

    async def _no(*_a, **_k):
        return False

    async def _sin_conexion_pausada(*_a, **_k):
        return None

    async def _no_deberia(*_a, **_k):
        raise AssertionError("no se intenta enviar")

    monkeypatch.setattr(notices, "_ya_enviado", _no)
    monkeypatch.setattr("core.gateway.meta_connection_state.motivo_pausa_de_conversacion", _sin_conexion_pausada)
    monkeypatch.setattr(sender_mod, "send_to_conversation", _no_deberia)
    monkeypatch.setattr(sender_mod, "send_template_to_conversation", _no_deberia)
    _con_pausa(monkeypatch, "negocio_pausado")

    async with tenant_scope(TENANT):
        res = await notices.send_event_notice(conv.id, "appointment.reminder", {}, notice_key="cita:1:24h")

    assert res.modo == "negocio_pausado"
    [msg] = sesion.agregados
    assert (msg.status, msg.error_reason, msg.client_message_id) == ("failed", "negocio_pausado", "cita:1:24h")


async def test_pausa_en_carrera_con_el_envio_no_lanza(monkeypatch: pytest.MonkeyPatch) -> None:
    conv = _conversacion()
    _con_pausa(monkeypatch, "cliente_pausado")
    assert await notices._pausa_tras_envio(conv) == notices.NoticeResult("cliente_pausado", "cliente_pausado")
    _con_pausa(monkeypatch, None)
    assert await notices._pausa_tras_envio(conv) is None


# ---------------------------------------------------------------- egress


async def test_egress_no_escala_si_la_pausa_frena_la_respuesta(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.kernel import egress

    enviados: list[str] = []

    async def _send(conversation_id, text, **kw):
        enviados.append(text)
        return False

    async def _escalate(*_a, **_k):
        raise AssertionError("una pausa no se escala")

    monkeypatch.setattr("core.gateway.sender.send_to_conversation", _send)
    monkeypatch.setattr("core.orchestrator.escalation.escalate", _escalate)
    _con_pausa(monkeypatch, "negocio_pausado")
    resultado = SimpleNamespace(text="Hola", escalated=False, image_url=None, confirmacion=None)
    await egress.entregar(uuid.uuid4(), resultado)
    assert enviados == ["Hola"]


# ---------------------------------------------------------------- correo


async def test_correo_con_negocio_pausado_no_se_encola(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.gateway.outbound_email import EmailOutboxError, queue_outbound_email

    _con_pausa(monkeypatch, "negocio_pausado")
    async with tenant_scope(TENANT):
        with pytest.raises(EmailOutboxError) as exc:
            await queue_outbound_email("ana@ejemplo.co", "Tu cita", "Mañana a las 10")
    assert exc.value.reason == "negocio_pausado"


# ---------------------------------------------------------------- seguimientos de comercio


@pytest.mark.parametrize("cron, por_negocio", [("follow_up_leads", "_follow_up_leads_del_negocio"), ("follow_up_quotes", "_follow_up_quotes_del_negocio")])
async def test_seguimientos_saltan_el_negocio_pausado(monkeypatch: pytest.MonkeyPatch, cron: str, por_negocio: str) -> None:
    from pack_commerce import jobs

    pausado, activo = uuid.uuid4(), uuid.uuid4()
    corridos: list[uuid.UUID] = []

    async def _ids():
        return [pausado, activo]

    async def _negocio(tid):
        return tid == pausado

    async def _config(tid, kind):
        return SimpleNamespace()

    async def _seguimiento(tid, cfg):
        corridos.append(tid)
        return 0, 0

    monkeypatch.setattr(jobs, "_tenant_ids", _ids)
    monkeypatch.setattr(pausa_automatica, "negocio_pausado", _negocio)
    monkeypatch.setattr(jobs, "_config_del_negocio", _config)
    monkeypatch.setattr(jobs, por_negocio, _seguimiento)
    await getattr(jobs, cron)()
    assert corridos == [activo]
