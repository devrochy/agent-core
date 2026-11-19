"""Quién firmó el webhook y qué grupos autoriza (ADR-0100, ADR-0126 / HU-CONN-05).

Dos bugs reales que estos tests cuidan:
- 2026-09-29: la ruta `POST /webhooks/whatsapp` validaba solo con el App Secret del
  framework; con una conexión BYOK Meta firma con la app del negocio y todas las
  entregas terminaban en 401.
- ADR-0100: el arreglo de arriba juntaba los secretos de todos los negocios del payload
  y aceptaba la firma con cualquiera, así que un negocio BYOK podía firmar eventos con
  el número de otro. Ahora cada secreto autoriza solo a su dueño (la plataforma, a todos).
- HU-CONN-05: el secreto era uno por negocio; con dos conexiones (dos apps) la otra daba
  401. Ahora cada grupo se verifica con el secreto de la conexión dueña de sus activos.
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


def _grupo(
    tenant_id: uuid.UUID | None, *, sub_id: str = "phone", conflict: bool = False, entry_id: str = "waba"
) -> webhooks._Grupo:
    evento = FlattenedEvent(
        object_type="whatsapp_business_account", field="messages", external_id=f"wamid.{uuid.uuid4().hex[:6]}",
        entry_id=entry_id, sub_id=sub_id, value={},
    )
    return webhooks._Grupo(entry_id, sub_id, [evento], EventRoute(tenant_id, conflict=conflict))


@pytest.fixture
def secretos(monkeypatch):
    """Plataforma = "plataforma-secret". Los secretos propios son POR GRUPO: la clave es
    `(negocio, entry, número)` (las conexiones dueñas de esos activos, HU-CONN-05); un grupo
    ausente no tiene conexión con secreto propio. Registra qué grupos se consultaron."""
    propios: dict[tuple[uuid.UUID, str, str | None], tuple[str, ...]] = {}
    pedidos: list[tuple[uuid.UUID, str, str | None]] = []

    async def fake_secretos(tenant_id, *, object_type, entry_id, sub_id, cache=None):
        assert object_type == "whatsapp_business_account"
        pedidos.append((tenant_id, entry_id, sub_id))
        return propios.get((tenant_id, entry_id, sub_id), ())

    monkeypatch.setattr("core.gateway.webhook_secrets.secretos_del_grupo", fake_secretos, raising=True)
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="plataforma-secret", meta_app_secret="otra"),
    )
    return propios, pedidos


async def test_la_firma_de_la_plataforma_autoriza_a_todos_sin_mirar_los_negocios(secretos) -> None:
    propios, pedidos = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[(b, "waba", "phone")] = ("secreto-de-b",)
    ga, gb = _grupo(a), _grupo(b)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("plataforma-secret"), [ga, gb])

    assert firmantes == webhooks.Firmantes(plataforma=True)
    assert firmantes.autoriza(ga) and firmantes.autoriza(gb)
    assert pedidos == []


async def test_la_firma_de_un_negocio_lo_autoriza_solo_a_el(secretos) -> None:
    """El ataque: B firma con su secreto un POST que trae también una entry de A."""
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    ga, gb = _grupo(a, sub_id="phone-a", entry_id="waba-a"), _grupo(b, sub_id="phone-b", entry_id="waba-b")
    propios[(a, "waba-a", "phone-a")] = ("secreto-de-a",)
    propios[(b, "waba-b", "phone-b")] = ("secreto-de-b",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-b"), [ga, gb])

    assert firmantes is not None and not firmantes.plataforma
    assert firmantes.negocios == frozenset({b})
    assert firmantes.autoriza(gb) and not firmantes.autoriza(ga)


async def test_cada_conexion_del_mismo_negocio_se_verifica_con_su_secreto(secretos) -> None:
    """HU-CONN-05: un negocio con una conexión BYOK y otra de otra app. El POST de la app 1
    autoriza solo el número de la conexión 1, y el de la app 2 solo el de la 2."""
    propios, _ = secretos
    t = uuid.uuid4()
    g1, g2 = _grupo(t, sub_id="phone-1", entry_id="waba-1"), _grupo(t, sub_id="phone-2", entry_id="waba-2")
    propios[(t, "waba-1", "phone-1")] = ("secreto-app-1",)
    propios[(t, "waba-2", "phone-2")] = ("secreto-app-2",)

    con_1 = await webhooks._firmantes_del_post(BODY, _firma("secreto-app-1"), [g1, g2])
    con_2 = await webhooks._firmantes_del_post(BODY, _firma("secreto-app-2"), [g1, g2])

    assert con_1 is not None and con_1.autoriza(g1) and not con_1.autoriza(g2)
    assert con_2 is not None and con_2.autoriza(g2) and not con_2.autoriza(g1)


async def test_un_grupo_sin_conexion_duena_no_se_autoriza_con_el_secreto_de_otra(secretos) -> None:
    """El atacante no elige el secreto: un número que no es de ninguna conexión del negocio
    no hereda el de las otras conexiones (antes, el secreto "del negocio" autorizaba todo)."""
    propios, _ = secretos
    t = uuid.uuid4()
    propio, ajeno = _grupo(t, sub_id="phone-1", entry_id="waba-1"), _grupo(t, sub_id="otro", entry_id="otra")
    propios[(t, "waba-1", "phone-1")] = ("secreto-app-1",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-app-1"), [propio, ajeno])

    assert firmantes is not None and firmantes.autoriza(propio) and not firmantes.autoriza(ajeno)


async def test_por_slug_solo_cuentan_los_grupos_de_su_negocio(secretos) -> None:
    propios, pedidos = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[(a, "waba", "phone")] = ("secreto-de-a",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-a"), [_grupo(a)], negocio_del_slug=b)

    assert firmantes == webhooks.Firmantes(plataforma=False)
    assert pedidos == []


async def test_un_negocio_sin_secreto_propio_no_firma(secretos) -> None:
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    propios[(b, "waba", "phone")] = ("secreto-de-b",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("cualquier-cosa"), [_grupo(a), _grupo(b)])

    assert firmantes == webhooks.Firmantes(plataforma=False, con_candidatos=True)


async def test_sin_ningun_secreto_no_hay_contra_que_verificar(monkeypatch, secretos) -> None:
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )

    assert await webhooks._firmantes_del_post(BODY, _firma("x"), [_grupo(uuid.uuid4()), _grupo(None)]) is None


async def test_un_grupo_en_conflicto_no_aporta_candidato(secretos) -> None:
    propios, pedidos = secretos
    a = uuid.uuid4()
    propios[(a, "waba", "phone")] = ("secreto-de-a",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-a"), [_grupo(a, conflict=True)])

    assert firmantes == webhooks.Firmantes(plataforma=False)
    assert pedidos == []


async def test_un_error_al_resolver_el_secreto_no_autoriza(monkeypatch, secretos) -> None:
    async def falla(*_a, **_k):
        raise RuntimeError("bd caída")

    monkeypatch.setattr("core.gateway.webhook_secrets.secretos_del_grupo", falla, raising=True)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("secreto-de-a"), [_grupo(uuid.uuid4())])

    assert firmantes == webhooks.Firmantes(plataforma=False, con_candidatos=True)


async def test_sin_secreto_de_plataforma_una_firma_incorrecta_es_401_no_el_escape(monkeypatch, secretos) -> None:
    """Basta un grupo con secreto propio para exigir firma: `Firmantes` vacío (401), nunca
    `None` (que habilita el escape sin firma de los entornos de prueba)."""
    propios, _ = secretos
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )
    a = uuid.uuid4()
    propios[(a, "waba", "phone")] = ("secreto-de-a",)

    assert await webhooks._firmantes_del_post(BODY, _firma("otra-cosa"), [_grupo(a)]) == webhooks.Firmantes(
        plataforma=False, con_candidatos=True
    )


async def test_sin_secreto_de_plataforma_un_error_al_leer_tampoco_habilita_el_escape(monkeypatch, secretos) -> None:
    async def falla(*_a, **_k):
        raise RuntimeError("bd caída")

    monkeypatch.setattr("core.gateway.webhook_secrets.secretos_del_grupo", falla, raising=True)
    monkeypatch.setattr(
        "core.gateway.connections.get_settings",
        lambda: SimpleNamespace(whatsapp_app_secret="", meta_app_secret=""),
    )

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("x"), [_grupo(uuid.uuid4())])

    assert firmantes == webhooks.Firmantes(plataforma=False, con_candidatos=True)


async def test_dos_negocios_con_la_misma_app_se_autorizan_entre_si(secretos) -> None:
    """No se puede distinguir con criptografía: se autoriza a los dos y se avisa."""
    propios, _ = secretos
    a, b = uuid.uuid4(), uuid.uuid4()
    ga, gb = _grupo(a, sub_id="phone-a"), _grupo(b, sub_id="phone-b")
    propios[(a, "waba", "phone-a")] = propios[(b, "waba", "phone-b")] = ("app-compartida",)

    firmantes = await webhooks._firmantes_del_post(BODY, _firma("app-compartida"), [ga, gb])

    assert firmantes is not None and firmantes.negocios == frozenset({a, b})
    assert firmantes.autoriza(ga) and firmantes.autoriza(gb)


def _de_grupos(*grupos: webhooks._Grupo) -> webhooks.Firmantes:
    """Firmantes de un secreto propio que autorizó estos grupos."""
    return webhooks.Firmantes(
        plataforma=False,
        grupos=frozenset(g.clave for g in grupos),
        negocios=frozenset(g.route.tenant_id for g in grupos if g.route.tenant_id),
    )


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
    gb = _grupo(b, sub_id="phone-b")

    r = await webhooks._process_payload(
        {}, known_tenant_id=None, firmantes=_de_grupos(gb), grupos=[_grupo(a, sub_id="phone-a"), gb],
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
        {}, known_tenant_id=None, firmantes=_de_grupos(_grupo(b, sub_id="phone-b")),
        grupos=[_grupo(a, sub_id="phone-a")],
    )

    assert puerta["registrados"] == [
        {"sub_id": "phone-a", "tenant_id": None, "error": "signature_tenant_mismatch", "firma_ok": False}
    ]


async def test_un_grupo_de_otra_conexion_del_mismo_negocio_no_se_procesa_ni_alerta(puerta) -> None:
    """La app de la conexión 1 firmando el número de la conexión 2 del mismo negocio: no se
    procesa (no es su conexión), pero tampoco es una inyección entre negocios."""
    t = uuid.uuid4()
    g1, g2 = _grupo(t, sub_id="phone-1"), _grupo(t, sub_id="phone-2")

    await webhooks._process_payload({}, known_tenant_id=None, firmantes=_de_grupos(g1), grupos=[g1, g2])

    assert puerta["despachados"] == [t]
    rechazo = next(x for x in puerta["registrados"] if x["sub_id"] == "phone-2")
    assert rechazo["error"] == "signature_connection_mismatch" and rechazo["tenant_id"] is None
    assert puerta["alertas"] == []


async def test_por_slug_un_grupo_sin_negocio_no_se_atribuye_al_del_slug(puerta) -> None:
    """Antes, el endpoint por slug le atribuía a su negocio cualquier número desconocido.
    Ahora un grupo sin conexión dueña no se procesa, aunque lo autorice la plataforma."""
    t = uuid.uuid4()

    r = await webhooks._process_payload(
        {}, known_tenant_id=t, firmantes=webhooks.Firmantes(plataforma=True), grupos=[_grupo(None, sub_id="x")],
    )

    assert r.status_code == 200 and b"ignored" in r.body
    assert puerta["despachados"] == []
    assert puerta["registrados"] == [{"sub_id": "x", "tenant_id": None, "error": "unresolved_tenant", "firma_ok": True}]


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

    await webhooks._alert_on_firma_ajena(_de_grupos(_grupo(b)), [a])
    await webhooks._alert_on_firma_ajena(_de_grupos(_grupo(b)), [a, a])
    assert len(alertas) == 1

    await webhooks._alert_on_firma_ajena(_de_grupos(_grupo(b)), [c])
    assert len(alertas) == 2

    # Otro firmante sobre el mismo afectado es otro par: su alerta no queda silenciada.
    await webhooks._alert_on_firma_ajena(_de_grupos(_grupo(c)), [a])
    assert len(alertas) == 3


async def test_si_redis_falla_la_alerta_de_firma_ajena_sale_igual(monkeypatch) -> None:
    """El dedupe es para no inundar el canal; sin Redis se prefiere alertar de más."""
    alertas: list[str] = []

    class RedisCaido:
        async def set(self, *_a, **_k):
            raise ConnectionError("redis caído")

    async def fake_alert(title, _body, *, severity):
        alertas.append(title)

    monkeypatch.setattr(webhooks, "get_redis", lambda: RedisCaido())
    monkeypatch.setattr("core.shared.alerts.send_alert", fake_alert)

    await webhooks._alert_on_firma_ajena(_de_grupos(_grupo(uuid.uuid4())), [uuid.uuid4()])

    assert alertas == ["Un negocio firmó eventos de otro"]


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


# ------------------------------------------------------------------ rechazo previo (ADR-0126)
def _por_grupo(n: int) -> dict:
    return {(f"waba-{i}", f"phone-{i}"): [] for i in range(n)}


async def test_sin_firma_bien_formada_se_rechaza_antes_de_rutear(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "_unsigned_allowed", lambda: False)

    for firma in ("", "sha256=", "sha256=xyz", "sha1=" + "a" * 40):
        r = await webhooks._rechazo_previo(BODY, firma, _por_grupo(1))
        assert r is not None and r.status_code == 401, firma


async def test_con_el_escape_de_test_no_se_exige_el_header(monkeypatch) -> None:
    monkeypatch.setattr(webhooks, "_unsigned_allowed", lambda: True)

    assert await webhooks._rechazo_previo(BODY, "", _por_grupo(1)) is None


async def test_demasiados_grupos_solo_con_la_firma_de_la_plataforma(secretos) -> None:
    muchos = _por_grupo(webhooks.MAX_GRUPOS_SIN_PLATAFORMA + 1)

    sin_plataforma = await webhooks._rechazo_previo(BODY, _firma("secreto-de-un-negocio"), muchos)
    con_plataforma = await webhooks._rechazo_previo(BODY, _firma("plataforma-secret"), muchos)
    pocos = await webhooks._rechazo_previo(BODY, _firma("secreto-de-un-negocio"), _por_grupo(3))

    assert sin_plataforma is not None and sin_plataforma.status_code == 401
    assert con_plataforma is None and pocos is None
