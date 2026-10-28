"""ADR-0178 D2 · avisos por evento por canal de texto, contra Postgres y Redis reales.

- Bandera apagada (por defecto): un cliente que solo tiene chat web no recibe nada, aunque el negocio ya haya
  guardado el texto del canal. Encendida: el aviso sale como texto con las variables del evento.
- QR (waweb): solo con `waweb_mode = allow_outbound`, con cupo del día y sin pausa; la cobranza no sale por QR.
- Messenger e Instagram: solo con la ventana de 24 h abierta.
- Aislamiento: la bandera y los textos son de cada negocio; un negocio no ve la conversación de otro.
- WhatsApp de siempre sigue primero y no cambia.
- API: los textos por canal se validan, sobreviven a un guardado que no los manda y la bandera es solo del master.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

EVENTO = "appointment.reminder"
TEXTO = "Hola {cliente.nombre}, recuerda tu cita {cita.titulo} el {cita.fecha} a las {cita.hora}."
RENDERIZADO = "Hola Ana, recuerda tu cita Corte el 2026-11-02 a las 10:00."
MAP = "/api/v1/admin/settings/message-templates"


def url_bandera(tid: uuid.UUID | str) -> str:
    return f"/api/v1/admin/platform/tenants/{tid}/avisos-por-canal"


def _payload_cita() -> dict[str, Any]:
    return {
        "customer_id": None, "appointment_id": str(uuid.uuid4()), "horas": 24, "titulo": "Corte",
        "fecha": "2026-11-02", "hora": "10:00", "zona": "America/Bogota",
    }


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Sin Celery: lo que `send_to_conversation` encola queda en esta lista (ids de mensaje)."""
    cola: list[str] = []
    monkeypatch.setattr("core.tasks.outbound_send.outbound_send_task.delay", lambda mid, tid: cola.append(str(mid)))
    return cola


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    from core.alta_negocio import onboard_tenant
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    creados: list[tuple[uuid.UUID, str]] = []
    for prefijo in ("avca", "avcb"):
        slug = f"{prefijo}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Avisos {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        creados.append((tid, slug))
    yield creados[0][0], creados[1][0]
    for tid, slug in creados:
        async with tenant_scope(tid):
            await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slug}")


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _bandera(tid: uuid.UUID, activa: bool) -> None:
    from core.gateway import avisos_bandera
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        await avisos_bandera.fijar(tid, activa, actor="test")


async def _textos(app_client, tid: uuid.UUID, por_canal: dict[str, str], evento: str = EVENTO) -> None:
    h = await api_headers(app_client, tid)
    r = await app_client.put(MAP, headers=h, json={"mapping": {evento: {"fallback_text_by_channel": por_canal}}})
    assert r.status_code == 200, r.text


async def _cliente(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import Customer
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            cliente = Customer(tenant_id=tid, channel="web", external_id=f"c:{uuid.uuid4().hex}", name="Ana")
            s.add(cliente)
            await s.flush()
            return cliente.id


async def _conversacion(
    tid: uuid.UUID, cliente: uuid.UUID, canal: str, *, ventana_horas: float | None = None, conexion: uuid.UUID | None = None
) -> uuid.UUID:
    from core.db.models import Conversation, utcnow
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = Conversation(
                tenant_id=tid, customer_id=cliente, channel=canal, connection_id=conexion,
                service_window_expires_at=None if ventana_horas is None else utcnow() + timedelta(hours=ventana_horas),
            )
            s.add(c)
            await s.flush()
            return c.id


async def _conexion_waweb(tid: uuid.UUID) -> uuid.UUID:
    from core.db.models import ChannelConnection
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            c = ChannelConnection(
                tenant_id=tid, channel="whatsapp", provider="waweb", type_id="whatsapp_waweb", category="communication",
                label="QR", external_id=f"waweb:{uuid.uuid4().hex}",
            )
            s.add(c)
            await s.flush()
            return c.id


async def _salientes(tid: uuid.UUID, conversacion: uuid.UUID) -> list[tuple[str, str, str]]:
    filas = await _sql(
        tid,
        "select content, status, coalesce(client_message_id, '') from messages "
        "where conversation_id = :c and direction = 'outbound' order by created_at",
        c=conversacion,
    )
    return [(a, b, c) for a, b, c in filas]


async def _avisar(tid: uuid.UUID, cliente: uuid.UUID, payload: dict[str, Any] | None = None, topic: str = EVENTO) -> None:
    import core.events.core_handlers as handlers
    from core.shared.tenant import tenant_scope

    datos = {**_payload_cita(), **(payload or {}), "customer_id": str(cliente)}
    async with tenant_scope(tid):
        await handlers._avisar_suscripcion(topic, datos)


async def _permitir_qr(tid: uuid.UUID, modo: str = "allow_outbound") -> None:
    from sqlalchemy import select as sel

    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            fila = (await s.execute(sel(AppSettings).where(AppSettings.tenant_id == tid))).scalar_one_or_none()
            if fila is None:
                fila = AppSettings(tenant_id=tid)
                s.add(fila)
            fila.behavior_config = {**(fila.behavior_config or {}), "waweb_mode": modo}


# ------------------------------------------------------------------ bandera apagada / encendida
async def test_bandera_apagada_un_cliente_solo_de_chat_web_no_recibe_nada(app_client, negocios, encolados) -> None:
    a, _ = negocios
    await _textos(app_client, a, {"webchat": TEXTO})
    cliente = await _cliente(a)
    conversacion = await _conversacion(a, cliente, "web")
    await _avisar(a, cliente)
    assert await _salientes(a, conversacion) == [] and encolados == []


async def test_bandera_encendida_el_aviso_sale_por_el_chat_web_una_sola_vez(app_client, negocios, encolados) -> None:
    a, _ = negocios
    await _textos(app_client, a, {"webchat": TEXTO})
    await _bandera(a, True)
    try:
        cliente = await _cliente(a)
        conversacion = await _conversacion(a, cliente, "web")
        payload = _payload_cita()
        await _avisar(a, cliente, payload)
        await _avisar(a, cliente, payload)  # el outbox reintenta: la misma clave de aviso no duplica
        [(contenido, estado, cmid)] = await _salientes(a, conversacion)
        assert (contenido, estado) == (RENDERIZADO, "queued")
        assert cmid == f"appt_reminder:{payload['appointment_id']}:24:0"
        assert len(encolados) == 1
    finally:
        await _bandera(a, False)


async def test_bandera_encendida_sin_texto_para_el_canal_no_envia(app_client, negocios, encolados) -> None:
    a, _ = negocios
    await _textos(app_client, a, {"messenger_meta": TEXTO})  # texto de otro canal: el web no tiene
    await _bandera(a, True)
    try:
        cliente = await _cliente(a)
        conversacion = await _conversacion(a, cliente, "web")
        await _avisar(a, cliente)
        assert await _salientes(a, conversacion) == [] and encolados == []
    finally:
        await _bandera(a, False)


async def test_el_texto_general_de_whatsapp_no_se_usa_en_otros_canales(app_client, negocios, encolados) -> None:
    a, _ = negocios
    h = await api_headers(app_client, a)
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"fallback_text": "Texto de WhatsApp"}}})
    assert r.status_code == 200, r.text
    await _bandera(a, True)
    try:
        cliente = await _cliente(a)
        conversacion = await _conversacion(a, cliente, "web")
        await _avisar(a, cliente, {"texto": "Texto del pack"})
        assert await _salientes(a, conversacion) == []
    finally:
        await _bandera(a, False)


async def test_whatsapp_sigue_primero_aunque_haya_otras_conversaciones(app_client, negocios, encolados) -> None:
    a, _ = negocios
    await _textos(app_client, a, {"webchat": TEXTO})
    await _bandera(a, True)
    try:
        cliente = await _cliente(a)
        web = await _conversacion(a, cliente, "web")
        wa = await _conversacion(a, cliente, "whatsapp", ventana_horas=3)
        await _avisar(a, cliente, {"texto": "Texto del pack para WhatsApp"})
        assert await _salientes(a, web) == []
        [(contenido, _, _)] = await _salientes(a, wa)  # lo de siempre: ventana abierta → texto de respaldo del pack
        assert contenido == "Texto del pack para WhatsApp"
    finally:
        await _bandera(a, False)


async def test_entre_varias_conversaciones_de_texto_gana_la_que_tiene_texto_definido(app_client, negocios, encolados) -> None:
    from core.gateway import avisos_canal
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    await _textos(app_client, a, {"instagram_meta": TEXTO})
    cliente = await _cliente(a)
    instagram = await _conversacion(a, cliente, "instagram", ventana_horas=2)
    web = await _conversacion(a, cliente, "web")  # la más reciente, pero sin texto definido para ella
    async with tenant_scope(a):
        elegida = await avisos_canal.conversacion_de_texto_para(cliente, EVENTO)
        assert elegida is not None and elegida.id == instagram
        await _textos(app_client, a, {})
        elegida = await avisos_canal.conversacion_de_texto_para(cliente, EVENTO)
        assert elegida is not None and elegida.id == web  # sin texto en ninguna: la más reciente (queda el rastro)
        assert await avisos_canal.conversacion_de_texto_para(await _cliente(a), EVENTO) is None


async def test_un_cliente_de_otro_negocio_en_el_payload_no_encuentra_conversacion(app_client, negocios) -> None:
    from core.gateway import avisos_canal
    from core.shared.tenant import tenant_scope

    a, b = negocios
    cliente_b = await _cliente(b)
    await _conversacion(b, cliente_b, "web")
    await _bandera(a, True)
    try:
        async with tenant_scope(a):
            assert await avisos_canal.conversacion_de_texto_para(cliente_b, EVENTO) is None
    finally:
        await _bandera(a, False)


# ------------------------------------------------------------------ canales con política
async def test_qr_exige_modo_envio_habilitado_cupo_y_no_envia_cobranza(app_client, negocios, encolados) -> None:
    from core.memory.working import get_redis
    from core.waweb import limits

    a, _ = negocios
    await _textos(app_client, a, {"whatsapp_waweb": TEXTO})
    h = await api_headers(app_client, a)
    r = await app_client.put(  # la API rechaza texto de QR para la cobranza...
        MAP, headers=h, json={"mapping": {"subscription.charge_reminder": {"fallback_text_by_channel": {"whatsapp_waweb": "x"}}}}
    )
    assert r.status_code == 400 and "QR" in r.text
    await _textos(app_client, a, {"whatsapp_waweb": TEXTO})
    await _sql(  # ...pero un mapa guardado a mano o antes de la regla tampoco la hace salir
        a, "update app_settings set message_templates_map = message_templates_map || cast(:m as jsonb) where tenant_id = :t",
        t=a, m='{"subscription.charge_reminder": {"template": "", "language": "es", "variables": [], "fallback_text": "",'
               ' "fallback_text_by_channel": {"whatsapp_waweb": "Debes {cargo.monto}"}}}',
    )
    await _bandera(a, True)
    redis = get_redis()
    try:
        cliente = await _cliente(a)
        conversacion = await _conversacion(a, cliente, "whatsapp", conexion=await _conexion_waweb(a))

        await _avisar(a, cliente)  # modo por defecto: solo respuesta → un aviso no es una respuesta
        assert await _salientes(a, conversacion) == []

        await _permitir_qr(a)
        await redis.set(limits.cap_key(a), 50)  # tope diario alcanzado: no se encola (el conector lo diferiría ~24 h)
        await _avisar(a, cliente)
        assert await _salientes(a, conversacion) == []

        await redis.delete(limits.cap_key(a))
        await redis.set(limits.tenant_pause_key(a), "1")  # pausa a mano
        await _avisar(a, cliente)
        assert await _salientes(a, conversacion) == []
        await redis.delete(limits.tenant_pause_key(a))

        # Cobranza: aun con texto, modo habilitado y cupo, no sale el texto por QR. Sigue el camino de antes: una
        # conversación sin ventana conocida deja el `sin_plantilla` visible en el panel (y nada se encola).
        await _avisar(a, cliente, {"monto": "1000", "moneda": "COP", "dias": -3, "charge_id": str(uuid.uuid4())},
                      topic="subscription.charge_reminder")
        assert [c for c, _, _ in await _salientes(a, conversacion) if c.startswith("Debes")] == []
        assert encolados == []

        await _avisar(a, cliente)  # ahora sí: modo habilitado, cupo y sin pausa
        [(contenido, estado, _), *_] = [m for m in await _salientes(a, conversacion) if m[1] == "queued"]
        assert (contenido, estado) == (RENDERIZADO, "queued") and len(encolados) == 1
    finally:
        await redis.delete(limits.cap_key(a), limits.tenant_pause_key(a))
        await _permitir_qr(a, "reply_only")
        await _bandera(a, False)


@pytest.mark.parametrize("canal", ["messenger", "instagram"])
async def test_messenger_e_instagram_solo_con_la_ventana_abierta(app_client, negocios, encolados, canal: str) -> None:
    a, _ = negocios
    tipo = {"messenger": "messenger_meta", "instagram": "instagram_meta"}[canal]
    await _textos(app_client, a, {tipo: TEXTO})
    await _bandera(a, True)
    try:
        cliente = await _cliente(a)
        vencida = await _conversacion(a, cliente, canal, ventana_horas=-1)
        await _avisar(a, cliente)
        assert await _salientes(a, vencida) == [] and encolados == []
        cliente2 = await _cliente(a)
        abierta = await _conversacion(a, cliente2, canal, ventana_horas=5)
        await _avisar(a, cliente2)
        [(contenido, estado, _)] = await _salientes(a, abierta)
        assert (contenido, estado) == (RENDERIZADO, "queued")
    finally:
        await _bandera(a, False)


# ------------------------------------------------------------------ aislamiento entre negocios
async def test_la_bandera_y_los_textos_son_de_cada_negocio(app_client, negocios, encolados) -> None:
    from core.gateway import avisos_bandera
    from core.gateway.event_notices import send_event_notice
    from core.shared.tenant import tenant_scope

    a, b = negocios
    for tid in (a, b):
        await _textos(app_client, tid, {"webchat": TEXTO})
    await _bandera(a, True)  # solo A
    try:
        ca, cb = await _cliente(a), await _cliente(b)
        conv_a, conv_b = await _conversacion(a, ca, "web"), await _conversacion(b, cb, "web")
        await _avisar(a, ca)
        await _avisar(b, cb)
        assert len(await _salientes(a, conv_a)) == 1
        assert await _salientes(b, conv_b) == []  # B tiene el mismo texto pero la bandera apagada
        async with tenant_scope(b):
            assert await avisos_bandera.habilitada_actual() is False
        async with tenant_scope(a):
            assert await avisos_bandera.habilitada_actual() is True
            # Con la bandera de A, un aviso hacia la conversación de B no sale: no es de este negocio.
            r = await send_event_notice(conv_b, EVENTO, {}, notice_key=uniq("aj"), por_canal=True)
            assert r.modo == "no_enviado"
        assert await _salientes(b, conv_b) == []
    finally:
        await _bandera(a, False)


# ------------------------------------------------------------------ API: textos por canal y bandera
async def test_los_textos_por_canal_se_validan_y_sobreviven_a_guardar_otra_cosa(app_client, negocios) -> None:
    a, _ = negocios
    h = await api_headers(app_client, a)
    for malo, parte in (
        ({"email_smtp": "x"}, "Canal sin texto"),
        ({"webchat": "Debes {cargo.monto}"}, "Variables no permitidas"),
    ):
        r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"fallback_text_by_channel": malo}}})
        assert r.status_code == 400 and r.json()["error"]["code"] == "message_templates_invalid", r.text
        assert parte in r.text

    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"fallback_text_by_channel": {"webchat": f"  {TEXTO} "}}}})
    assert r.status_code == 200 and r.json()["mapping"][EVENTO]["fallback_text_by_channel"] == {"webchat": TEXTO}
    # Un panel anterior guarda otra cosa sin mandar el campo: los textos por canal se conservan.
    r = await app_client.put(MAP, headers=h, json={"mapping": {EVENTO: {"fallback_text": "Texto de WhatsApp"}}})
    assert r.status_code == 200, r.text
    assert r.json()["mapping"][EVENTO] == {
        "template": "", "language": "es", "variables": [], "fallback_text": "Texto de WhatsApp",
        "fallback_text_by_channel": {"webchat": TEXTO}, "option_template": "",  # D3: campo nuevo, vacío por defecto
    }
    # Un panel anterior que no manda el evento (sin plantilla ni texto general) tampoco los pierde.
    r = await app_client.put(MAP, headers=h, json={"mapping": {}})
    assert r.status_code == 200, r.text
    assert r.json()["mapping"][EVENTO] == {
        "template": "", "language": "es", "variables": [], "fallback_text": "",
        "fallback_text_by_channel": {"webchat": TEXTO}, "option_template": "",  # D3: campo nuevo, vacío por defecto
    }
    # Mandarlo vacío lo borra; un evento sin nada deja de guardarse.
    todos = {evento: {"fallback_text_by_channel": {}} for evento in r.json()["mapping"]}  # otros tests dejan los suyos
    r = await app_client.put(MAP, headers=h, json={"mapping": todos})
    assert r.status_code == 200 and r.json()["mapping"] == {}
    guardado = await _sql(a, "select message_templates_map from app_settings where tenant_id = :t", t=a)
    assert guardado[0][0] == {}


async def test_solo_el_master_cambia_la_bandera_y_queda_auditada(app_client, negocios) -> None:
    a, b = negocios
    master = await api_headers(app_client, a)
    assert (await app_client.get(url_bandera(a))).status_code == 401
    r = await app_client.get(url_bandera(a), headers=master)
    assert r.status_code == 200 and r.json() == {"habilitada": False}  # apagada de fábrica
    for malo in ({"habilitada": "true"}, {"habilitada": 1}, {}, {"habilitada": True, "otro": 1}):
        assert (await app_client.put(url_bandera(a), headers=master, json=malo)).status_code == 422, malo
    for _ in range(2):  # idempotente
        r = await app_client.put(url_bandera(a), headers=master, json={"habilitada": True})
        assert r.status_code == 200 and r.json() == {"habilitada": True}
    assert (await app_client.get(url_bandera(b), headers=await api_headers(app_client, b))).json() == {"habilitada": False}
    auditoria = await _sql(a, "select actor, input from audit_log where tenant_id = :t and action = 'avisos_por_canal_bandera' and actor like 'admin:%' order by created_at", t=a)
    assert len(auditoria) == 2 and auditoria[0][0] == "admin:admin@local" and auditoria[0][1] == {"habilitada": True}
    # No pisa la bandera de las opciones directas ni el resto de settings, y viceversa.
    await _sql(a, "update tenants set settings = jsonb_set(coalesce(settings,'{}'::jsonb), '{opciones_directas}', '{\"habilitada\": true}') where id = :t", t=a)
    r = await app_client.put(url_bandera(a), headers=master, json={"habilitada": False})
    assert r.status_code == 200 and r.json() == {"habilitada": False}
    ((valores,),) = await _sql(a, "select settings from tenants where id = :t", t=a)
    assert valores["opciones_directas"] == {"habilitada": True} and valores["avisos_por_canal"] == {"habilitada": False}
    await _sql(a, "update tenants set settings = settings - 'opciones_directas' where id = :t", t=a)
    assert (await app_client.get(url_bandera(uuid.uuid4()), headers=master)).status_code == 404


async def test_ni_el_dueno_ni_un_colaborador_cambian_la_bandera(app_client, negocios) -> None:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    (antes,) = await _sql(a, "select count(*) from audit_log where tenant_id = :t and action = 'avisos_por_canal_bandera'", t=a)
    for rol in ("owner", "colaborador"):
        email = f"{uniq(rol)}@example.test"
        async with tenant_scope(a):
            async with get_session() as session:
                admin = Admin(email=email, password_hash=hash_password("Avisos-Canal-2026!"), role="colaborador")
                session.add(admin)
                await session.flush()
                session.add(TenantUser(tenant_id=a, admin_id=admin.id, role=rol))
        h = await api_headers(app_client, a, email=email, password="Avisos-Canal-2026!")
        assert (await app_client.get(url_bandera(a), headers=h)).status_code == 403, rol
        assert (await app_client.put(url_bandera(a), headers=h, json={"habilitada": True})).status_code == 403, rol
    (despues,) = await _sql(a, "select count(*) from audit_log where tenant_id = :t and action = 'avisos_por_canal_bandera'", t=a)
    assert despues[0] == antes[0]  # ninguno de los intentos dejó rastro: no se escribió nada
