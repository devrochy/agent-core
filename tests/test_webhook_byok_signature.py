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


async def test_dos_negocios_con_la_misma_app_se_autorizan_entre_si(secretos) -> None:
    """No se puede distinguir con criptografía: se autoriza a los dos y se avisa."""
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[a] = propios[b] = "app-compartida"

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("app-compartida"), [_grupo(a), _grupo(b)])

    assert firmantes is not None and firmantes.negocios == frozenset({a, b})


# ------------------------------------------------------------------ la puerta en _process_payload
async def test_un_grupo_de_otro_negocio_no_se_procesa(monkeypatch) -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    registrados: list[dict] = []
    despachados: list[uuid.UUID] = []
    alertas: list[list[uuid.UUID]] = []

    async def fake_log(_app, event, *, tenant_id, process_error, signature_ok=True):
        registrados.append(
            {"sub_id": event.sub_id, "tenant_id": tenant_id, "error": process_error, "firma_ok": signature_ok}
        )
        return True

    async def fake_dispatch(tenant_id, _entry_id, _events):
        despachados.append(tenant_id)
        return 1, 0

    async def nada(*_a, **_k):
        return None

    async def operativo(_tid):
        return True

    async def fake_alerta(_firmantes, ajenos):
        alertas.append(ajenos)

    monkeypatch.setattr(webhooks, "_log_webhook_event", fake_log)
    monkeypatch.setattr(webhooks, "_dispatch_entry_messages", fake_dispatch)
    monkeypatch.setattr(webhooks, "_dispatch_template_events", nada)
    monkeypatch.setattr(webhooks, "_dispatch_status_events", nada)
    monkeypatch.setattr(webhooks, "_mark_entry_processed", nada)
    monkeypatch.setattr(webhooks, "_alert_on_firma_ajena", fake_alerta)
    monkeypatch.setattr("core.billing.enforcement.is_tenant_operational", operativo)
    monkeypatch.setattr(webhooks, "get_settings", lambda: SimpleNamespace(meta_app_id="app", app_env="production"))

    r = await webhooks._process_payload(
        {}, known_tenant_id=None, firmantes=webhooks.Firmantes.de_negocio(b),
        grupos=[_grupo(a, sub_id="phone-a"), _grupo(b, sub_id="phone-b")],
    )

    assert r.status_code == 200
    assert despachados == [b]
    rechazo = next(x for x in registrados if x["sub_id"] == "phone-a")
    assert rechazo == {"sub_id": "phone-a", "tenant_id": None, "error": "signature_tenant_mismatch", "firma_ok": False}
    assert alertas == [[a]]
