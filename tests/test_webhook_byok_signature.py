"""Quién firmó el webhook genérico y a qué negocios autoriza (ADR-0096).

Dos bugs reales que estos tests cuidan:
- 2026-09-29: la ruta `POST /webhooks/whatsapp` validaba solo con el App Secret del
  framework; con una conexión BYOK Meta firma con la app del negocio y todas las
  entregas terminaban en 401.
- ADR-0096: el arreglo de arriba juntaba los secretos de todos los negocios del payload
  y aceptaba la firma con cualquiera, así que un negocio BYOK podía firmar eventos con
  el número de otro. Ahora cada secreto autoriza solo a su dueño (la plataforma, a todos).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from types import SimpleNamespace

import pytest

from core.gateway import webhooks
from core.gateway.webhook_flatten import FlattenedEvent
from core.gateway.webhook_route_tenant import EventRoute

BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def _firma(secreto: str, body: bytes = BODY) -> str:
    return "sha256=" + hmac.new(secreto.encode(), body, hashlib.sha256).hexdigest()


def _grupo(tenant_id: uuid.UUID | None, *, sub_id: str = "phone", conflict: bool = False) -> webhooks._Grupo:
    evento = FlattenedEvent(
        object_type="whatsapp_business_account", field="messages", external_id=f"wamid.{uuid.uuid4().hex[:6]}",
        entry_id="waba", sub_id=sub_id, value={},
    )
    return webhooks._Grupo("waba", sub_id, [evento], EventRoute(tenant_id, conflict=conflict))


@pytest.fixture
def secretos(monkeypatch):
    """Plataforma = "plataforma-secret"; el secreto propio de cada negocio sale del dict
    (ausente = el negocio usa la app de la plataforma). Registra en qué scope se pidió."""
    propios: dict[uuid.UUID, str] = {}
    pedidos: list[tuple[uuid.UUID, object]] = []

    async def fake_own(tenant_id: uuid.UUID) -> str:
        from core.shared.tenant import get_current_tenant_id

        pedidos.append((tenant_id, get_current_tenant_id()))
        return propios.get(tenant_id, "")

    monkeypatch.setattr("core.gateway.connections.resolve_own_app_secret", fake_own, raising=True)
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="plataforma-secret", meta_app_secret="otra"),
    )
    return propios, pedidos


async def test_la_firma_de_la_plataforma_autoriza_a_todos_sin_mirar_los_negocios(secretos) -> None:
    propios, pedidos = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[b] = "secreto-de-b"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("plataforma-secret"), [_grupo(a), _grupo(b)])

    assert firmantes == webhooks.Firmantes(plataforma=True)
    assert firmantes.autoriza(a) and firmantes.autoriza(b)
    assert pedidos == []


async def test_la_firma_de_un_negocio_lo_autoriza_solo_a_el(secretos) -> None:
    """El ataque: B firma con su secreto un POST que trae también una entry de A."""
    propios, pedidos = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[a] = "secreto-de-a"
    propios[b] = "secreto-de-b"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-b"), [_grupo(a), _grupo(b)])

    assert firmantes is not None
    assert firmantes.negocios == frozenset({b}) and not firmantes.plataforma
    assert firmantes.autoriza(b) and not firmantes.autoriza(a)
    # El secreto BYOK vive bajo RLS: se pide dentro del scope de su negocio.
    assert all(tid == scope for tid, scope in pedidos)


async def test_un_negocio_sin_secreto_propio_no_firma(secretos) -> None:
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[b] = "secreto-de-b"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("cualquier-cosa"), [_grupo(a), _grupo(b)])

    assert firmantes == webhooks.Firmantes(plataforma=False)


async def test_sin_ningun_secreto_no_hay_contra_que_verificar(monkeypatch, secretos) -> None:
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )

    assert await webhooks._firmantes_del_post(BODY, _firma("x"), [_grupo(uuid.uuid4()), _grupo(None)]) is None


async def test_un_grupo_en_conflicto_no_aporta_candidato(secretos) -> None:
    propios, pedidos = secretos
    a = uuid.uuid4()
    propios[a] = "secreto-de-a"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-a"), [_grupo(a, conflict=True)])

    assert firmantes == webhooks.Firmantes(plataforma=False)
    assert pedidos == []


async def test_un_error_al_resolver_el_secreto_no_autoriza(monkeypatch, secretos) -> None:
    async def falla(_tenant_id):
        raise RuntimeError("bd caída")

    monkeypatch.setattr("core.gateway.connections.resolve_own_app_secret", falla, raising=True)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-a"), [_grupo(uuid.uuid4())])

    assert firmantes == webhooks.Firmantes(plataforma=False)


async def test_sin_secreto_de_plataforma_una_firma_incorrecta_es_401_no_el_escape(monkeypatch, secretos) -> None:
    """Basta un negocio con secreto propio para exigir firma: `Firmantes` vacío (401), nunca
    `None` (que habilita el escape sin firma de los entornos de prueba)."""
    propios, _ = secretos
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )
    a = uuid.uuid4()
    propios[a] = "secreto-de-a"

    assert await webhooks._firmantes_del_post(BODY, _firma("otra-cosa"), [_grupo(a)]) == webhooks.Firmantes(
        plataforma=False
    )


async def test_sin_secreto_de_plataforma_un_error_al_leer_tampoco_habilita_el_escape(monkeypatch, secretos) -> None:
    async def falla(_tenant_id):
        raise RuntimeError("bd caída")

    monkeypatch.setattr("core.gateway.connections.resolve_own_app_secret", falla, raising=True)
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("x"), [_grupo(uuid.uuid4())])

    assert firmantes == webhooks.Firmantes(plataforma=False)


async def test_dos_negocios_con_la_misma_app_se_autorizan_entre_si(secretos) -> None:
    """No se puede distinguir con criptografía: se autoriza a los dos y se avisa."""
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[a] = propios[b] = "app-compartida"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("app-compartida"), [_grupo(a), _grupo(b)])

    assert firmantes is not None and firmantes.negocios == frozenset({a, b})


# ------------------------------------------------------------------ la puerta en _process_payload
@pytest.fixture
def puerta(monkeypatch):
    """`_process_payload` con el despacho y el registro falsos. `operativos`: los negocios
    que pasan el chequeo de facturación (por defecto, todos)."""
    estado: dict = {"registrados": [], "despachados": [], "alertas": [], "operativos": None}

    async def fake_log(_app, event, *, tenant_id, process_error, signature_ok=True):
        estado["registrados"].append(
            {"sub_id": event.sub_id, "tenant_id": tenant_id, "error": process_error, "firma_ok": signature_ok}
        )
        return True

    async def fake_dispatch(tenant_id, _entry_id, _events):
        estado["despachados"].append(tenant_id)
        return 1, 0

    async def nada(*_a, **_k):
        return None

    async def operativo(tid):
        return estado["operativos"] is None or tid in estado["operativos"]

    async def fake_alerta(_firmantes, ajenos):
        estado["alertas"].append(ajenos)

    monkeypatch.setattr(webhooks, "_log_webhook_event", fake_log)
    monkeypatch.setattr(webhooks, "_dispatch_entry_messages", fake_dispatch)
    monkeypatch.setattr(webhooks, "_dispatch_template_events", nada)
    monkeypatch.setattr(webhooks, "_dispatch_status_events", nada)
    monkeypatch.setattr(webhooks, "_mark_entry_processed", nada)
    monkeypatch.setattr(webhooks, "_alert_on_firma_ajena", fake_alerta)
    monkeypatch.setattr("core.billing.enforcement.is_tenant_operational", operativo)
    monkeypatch.setattr(webhooks, "get_settings", lambda: SimpleNamespace(meta_app_id="app", app_env="production"))
    return estado


async def test_un_grupo_de_otro_negocio_no_se_procesa(puerta) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    r = await webhooks._process_payload(
        {}, known_tenant_id=None, firmantes=webhooks.Firmantes.de_negocio(b),
        grupos=[_grupo(a, sub_id="phone-a"), _grupo(b, sub_id="phone-b")],
    )

    assert r.status_code == 200
    assert puerta["despachados"] == [b]
    rechazo = next(x for x in puerta["registrados"] if x["sub_id"] == "phone-a")
    assert rechazo == {"sub_id": "phone-a", "tenant_id": None, "error": "signature_tenant_mismatch", "firma_ok": False}
    assert puerta["alertas"] == [[a]]


async def test_la_firma_se_mira_antes_que_la_facturacion(puerta) -> None:
    """Si la facturación fuera primero, el evento falsificado quedaría registrado a nombre
    de A como `tenant_not_operational`, y quien firmó sabría que A no está operativo."""
    a, b = uuid.uuid4(), uuid.uuid4()
    puerta["operativos"] = {b}

    await webhooks._process_payload(
        {}, known_tenant_id=None, firmantes=webhooks.Firmantes.de_negocio(b),
        grupos=[_grupo(a, sub_id="phone-a")],
    )

    assert puerta["registrados"] == [
        {"sub_id": "phone-a", "tenant_id": None, "error": "signature_tenant_mismatch", "firma_ok": False}
    ]


# ------------------------------------------------------------------ alerta y payloads raros
async def test_la_alerta_de_firma_ajena_sale_una_vez_por_ventana(monkeypatch) -> None:
    """Una configuración rota manda el mismo POST cada pocos minutos: una alerta por día
    y por par firmante-afectados, no una por POST."""
    claves: dict[str, int] = {}
    alertas: list[str] = []

    class FakeRedis:
        async def set(self, key, _value, *, nx, ex):
            assert nx and ex == webhooks._FIRMA_AJENA_VENTANA_S
            if key in claves:
                return None
            claves[key] = ex
            return True

    async def fake_alert(title, _body, *, severity):
        alertas.append(title)

    monkeypatch.setattr(webhooks, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr("core.shared.alerts.send_alert", fake_alert)
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    await webhooks._alert_on_firma_ajena(webhooks.Firmantes.de_negocio(b), [a])
    await webhooks._alert_on_firma_ajena(webhooks.Firmantes.de_negocio(b), [a, a])
    assert len(alertas) == 1

    await webhooks._alert_on_firma_ajena(webhooks.Firmantes.de_negocio(b), [c])
    assert len(alertas) == 2


async def test_un_payload_que_no_es_objeto_se_ignora(monkeypatch) -> None:
    """Un JSON válido que no es un objeto (una lista, un número) no debe reventar el
    ruteo con un 500, que Meta reintentaría durante 36 h."""

    async def no_rutear(_payload):
        raise AssertionError("no se rutea un payload que no es objeto")

    monkeypatch.setattr(webhooks, "_rutear_grupos", no_rutear)

    async def body() -> bytes:
        return b"[1, 2]"

    request = SimpleNamespace(headers={"x-hub-signature-256": _firma("x", b"[1, 2]")}, body=body)
    r = await webhooks.receive_whatsapp(request)  # type: ignore[arg-type]

    assert r.status_code == 200 and b"ignored" in r.body
