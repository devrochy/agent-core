"""ADR-0178 D2 · avisos por evento por canal de texto: la decisión de vía, los textos por canal y la bandera.

Sin base de datos: la decisión es pura y el envío se prueba con dobles (`tests/integration/test_avisos_por_canal_db.py`
cubre Postgres real). Aquí también está la prueba de regresión de la bandera APAGADA: con `por_canal=False` los 8
eventos de `EVENTOS` se comportan igual que antes de D2 (el cuerpo de `send_event_notice` no cambió).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import core.gateway.avisos_canal as ac
import core.gateway.event_notices as notices
import core.gateway.sender as sender_mod
from core.gateway import avisos_bandera
from core.gateway.connection_types import capacidades_de, tipo_de_conexion
from core.shared.tenant import tenant_scope

TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
EVENTO = "appointment.reminder"
CONTEXTO = {"cliente": {"nombre": "Ana"}, "cita": {"titulo": "Corte", "fecha": "2026-11-02", "hora": "10:00"}}
TEXTO = "Hola {cliente.nombre}, recuerda tu cita {cita.titulo} el {cita.fecha} a las {cita.hora}."
ESPERADO = "Hola Ana, recuerda tu cita Corte el 2026-11-02 a las 10:00."


# ---------------------------------------------------------------- la decisión (pura)
def _decidir(canal: str, proveedor: str = "", *, evento: str = EVENTO, texto: bool = True, ventana: bool = False):
    return ac.decidir_via_aviso(
        canal=canal, proveedor=proveedor, evento=evento, texto_definido=texto, ventana_abierta=ventana
    )


@pytest.mark.parametrize(
    ("canal", "proveedor", "tipo"),
    [
        ("whatsapp", "waweb", ac.QR),
        ("web", "", ac.WEB),
        ("web", "web", ac.WEB),
        ("messenger", "", ac.MESSENGER),
        ("instagram", "meta", ac.INSTAGRAM),
        ("whatsapp", "meta", "whatsapp_meta"),
        ("whatsapp", "", None),  # varios tipos y proveedor desconocido
        ("whatsapp", "otro", None),
        ("canal_inventado", "", None),
    ],
)
def test_el_tipo_de_conexion_sale_de_canal_y_proveedor(canal: str, proveedor: str, tipo: str | None) -> None:
    assert tipo_de_conexion(canal, proveedor) == tipo


def test_las_claves_validas_son_tipos_sin_plantillas() -> None:
    """Un texto por canal solo tiene sentido donde NO hay plantilla aprobada: la tabla de C1 lo garantiza."""
    for tipo in ac.TIPOS_DE_TEXTO:
        canal, proveedor = {"whatsapp_waweb": ("whatsapp", "waweb"), "webchat": ("web", ""),
                            "messenger_meta": ("messenger", ""), "instagram_meta": ("instagram", "")}[tipo]
        assert tipo_de_conexion(canal, proveedor) == tipo
        assert capacidades_de(canal, proveedor).plantilla_aprobada is False


def test_whatsapp_meta_sigue_el_camino_de_siempre() -> None:
    for ventana in (True, False):
        for texto in (True, False):
            d = _decidir("whatsapp", "meta", texto=texto, ventana=ventana)
            assert d.via == ac.LEGACY and d.motivo == ""


@pytest.mark.parametrize("canal", ["web", "messenger", "instagram"])
def test_sin_texto_definido_el_canal_sin_camino_previo_se_omite_con_motivo(canal: str) -> None:
    d = _decidir(canal, texto=False, ventana=True)
    assert (d.via, d.motivo) == (ac.OMITIDO, ac.SIN_RESPALDO)


def test_qr_sin_texto_conserva_su_camino_previo_pero_deja_rastro() -> None:
    d = _decidir("whatsapp", "waweb", texto=False)
    assert (d.via, d.motivo) == (ac.LEGACY, ac.SIN_RESPALDO)


def test_con_texto_el_chat_web_envia_sin_mirar_la_ventana() -> None:
    assert _decidir("web", ventana=False).via == ac.TEXTO
    assert _decidir("web", ventana=True).via == ac.TEXTO


@pytest.mark.parametrize("canal", ["messenger", "instagram"])
def test_messenger_e_instagram_solo_dentro_de_la_ventana(canal: str) -> None:
    assert _decidir(canal, ventana=True).via == ac.TEXTO
    cerrada = _decidir(canal, ventana=False)  # ventana desconocida o vencida: el asistente nunca escribe fuera
    assert (cerrada.via, cerrada.motivo) == (ac.OMITIDO, ac.VENTANA_CERRADA)


def test_qr_con_texto_envia_salvo_la_cobranza() -> None:
    assert _decidir("whatsapp", "waweb", evento="appointment.reminder").via == ac.TEXTO
    assert _decidir("whatsapp", "waweb", evento="provisioning.invitation").via == ac.TEXTO
    assert _decidir("whatsapp", "waweb", evento="subscription.trial_ending").via == ac.TEXTO
    for evento in ac.COBRANZA:
        d = _decidir("whatsapp", "waweb", evento=evento)
        assert (d.via, d.motivo) == (ac.LEGACY, ac.COBRANZA_SIN_HORARIO), evento
    # La cobranza sí puede salir por los canales que entregan al instante.
    for canal in ("web", "messenger", "instagram"):
        assert _decidir(canal, evento="subscription.charge_reminder", ventana=True).via == ac.TEXTO


def test_la_cobranza_excluida_son_exactamente_los_eventos_de_cobro() -> None:
    assert ac.COBRANZA <= set(notices.EVENTOS)
    assert ac.COBRANZA == {k for k in notices.EVENTOS if k.startswith("subscription.") and k != "subscription.trial_ending"}


def test_texto_del_canal_ignora_lo_que_no_es_un_texto() -> None:
    assert ac.texto_del_canal({"fallback_text_by_channel": {ac.WEB: "  hola  "}}, ac.WEB) == "hola"
    assert ac.texto_del_canal({"fallback_text_by_channel": {ac.WEB: "x"}}, ac.QR) == ""
    assert ac.texto_del_canal({"fallback_text_by_channel": {ac.WEB: 5}}, ac.WEB) == ""
    assert ac.texto_del_canal({"fallback_text_by_channel": "hola"}, ac.WEB) == ""
    assert ac.texto_del_canal({"fallback_text": "hola"}, ac.WEB) == ""  # el texto general NO vale para otros canales
    assert ac.texto_del_canal({}, None) == ""


# ---------------------------------------------------------------- la bandera
@pytest.mark.parametrize(
    ("settings", "esperado"),
    [
        (None, False), ({}, False), ({"avisos_por_canal": None}, False), ({"avisos_por_canal": {}}, False),
        ({"avisos_por_canal": True}, False), ({"avisos_por_canal": {"habilitada": "true"}}, False),
        ({"avisos_por_canal": {"habilitada": 1}}, False), ({"avisos_por_canal": {"habilitada": False}}, False),
        ({"opciones_directas": {"habilitada": True}}, False),  # la de opciones directas no enciende esta
        ({"avisos_por_canal": {"habilitada": True}}, True),
    ],
)
def test_la_bandera_esta_apagada_salvo_el_literal_true(settings: Any, esperado: bool) -> None:
    assert avisos_bandera.habilitada_desde(settings) is esperado


@pytest.mark.asyncio
async def test_leer_la_bandera_sin_negocio_o_con_error_es_apagada(monkeypatch) -> None:
    assert await avisos_bandera.habilitada_actual() is False  # sin tenant_scope

    class _Rota:
        async def __aenter__(self):
            raise RuntimeError("sin base")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("core.db.session.get_session", lambda: _Rota())
    async with tenant_scope(TENANT):
        assert await avisos_bandera.habilitada_actual() is False


def test_nadie_fuera_de_la_ruta_master_escribe_la_bandera() -> None:
    """`avisos_bandera.fijar` solo la llama la ruta de plataforma master-only, tras el guard de master; los demás
    módulos solo la LEEN y nadie más la escribe con SQL."""
    import ast
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    permitidos = {"core/gateway/avisos_bandera.py", "core/api/routers/platform.py"}
    lectores = {"core/events/core_handlers.py", "core/provisioning/owner.py"}
    for ruta in (raiz / "core").rglob("*.py"):
        rel = ruta.relative_to(raiz).as_posix()
        texto = ruta.read_text(encoding="utf-8")
        importa = "import avisos_bandera" in texto or "gateway.avisos_bandera" in texto or "avisos_bandera import" in texto
        if rel not in permitidos | lectores and importa:
            raise AssertionError(f"{rel} importa la bandera de avisos por canal")
        if rel in lectores:
            assert "avisos_bandera.fijar" not in texto, f"{rel} solo lee la bandera"
        if rel != "core/gateway/avisos_bandera.py" and "{avisos_por_canal}" in texto:
            raise AssertionError(f"{rel} escribe la bandera de avisos por canal con SQL")
    arbol = ast.parse((raiz / "core/api/routers/platform.py").read_text(encoding="utf-8"))
    rutas = {n.name: n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)}
    for nombre in ("get_avisos_por_canal", "put_avisos_por_canal"):
        llamadas = [
            c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
            for c in sorted(
                (c for st in rutas[nombre].body for c in ast.walk(st) if isinstance(c, ast.Call)),
                key=lambda c: (c.lineno, c.col_offset),
            )
        ]
        assert llamadas[0] == "_require_master_agent_os", f"{nombre}: el guard de master va primero"
    fijar = [n for n in ast.walk(arbol) if isinstance(n, ast.Attribute) and n.attr == "fijar" and getattr(n.value, "id", "") == "avisos_bandera"]
    assert len(fijar) == 1


# ---------------------------------------------------------------- send_event_notice con la bandera
class _Sesion:
    def __init__(self, conversacion) -> None:
        self.conversacion = conversacion

    async def get(self, modelo, id_):
        return self.conversacion

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _conversacion(canal: str, *, ventana_horas: float | None = None):
    """`ventana_horas`: horas que le quedan a la ventana de 24 h (None = desconocida)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        channel=canal,
        connection_id=None,
        meta_connection_id=None,
        service_window_expires_at=None if ventana_horas is None else datetime.now(UTC) + timedelta(hours=ventana_horas),
    )


@pytest.fixture
def entorno(monkeypatch):
    estado: dict[str, Any] = {
        "textos": [], "plantillas": [], "sin_plantilla": [], "omitidos": [], "proveedor": "", "bloqueo": "",
        "mapa": {EVENTO: {"fallback_text_by_channel": {ac.WEB: TEXTO, ac.QR: TEXTO, ac.MESSENGER: TEXTO, ac.INSTAGRAM: TEXTO}}},
    }

    async def _sin_pausa(_meta_connection_id):
        return None

    async def _ya(_k):
        return False

    async def _mapa(_t):
        return estado["mapa"]

    async def _aprobada(_c, _n, _i):
        return None

    async def _registrar(_cid, _key, _ev, detalle):
        estado["sin_plantilla"].append(detalle)

    async def _texto(cid, texto, **kw):
        estado["textos"].append((texto, kw))
        return estado.get("enviado", True)

    async def _plantilla(cid, **kw):
        estado["plantillas"].append(kw)
        return True

    async def _proveedor(_c):
        return estado["proveedor"]

    async def _bloqueo(_t):
        return estado["bloqueo"]

    monkeypatch.setattr("core.gateway.meta_connection_state.motivo_pausa_de", _sin_pausa)
    monkeypatch.setattr(notices, "_ya_enviado", _ya)
    monkeypatch.setattr(notices, "load_templates_map", _mapa)
    monkeypatch.setattr(notices, "approved_template_for", _aprobada)
    monkeypatch.setattr(notices, "_registrar_sin_plantilla", _registrar)
    monkeypatch.setattr(sender_mod, "send_to_conversation", _texto)
    monkeypatch.setattr(sender_mod, "send_template_to_conversation", _plantilla)
    monkeypatch.setattr(ac, "proveedor_de_conversacion", _proveedor)
    monkeypatch.setattr(ac, "waweb_bloqueo", _bloqueo)
    monkeypatch.setattr(
        ac, "aviso_omitido", lambda evento, canal, motivo, **kw: estado["omitidos"].append((evento, canal, motivo))
    )

    def _con(conversacion):
        monkeypatch.setattr(notices, "get_session", lambda: _Sesion(conversacion))

    estado["con"] = _con
    return estado


async def _enviar(entorno, conversacion, *, por_canal: bool = True, evento: str = EVENTO, key: str = "k:1"):
    entorno["con"](conversacion)
    async with tenant_scope(TENANT):
        return await notices.send_event_notice(
            conversacion.id, evento, CONTEXTO, notice_key=key, por_canal=por_canal
        )


@pytest.mark.asyncio
async def test_chat_web_con_texto_sale_como_texto_con_variables_del_evento(entorno) -> None:
    r = await _enviar(entorno, _conversacion("web"))
    assert (r.modo, r.detalle) == ("texto", ac.WEB)
    [(texto, kw)] = entorno["textos"]
    assert texto == ESPERADO and kw == {"client_message_id_prefix": "k:1", "raise_on_window_expired": True}
    assert entorno["plantillas"] == [] and entorno["omitidos"] == []


@pytest.mark.asyncio
async def test_una_variable_sin_dato_queda_vacia_como_en_los_demas_avisos(entorno) -> None:
    entorno["mapa"] = {EVENTO: {"fallback_text_by_channel": {ac.WEB: "Cita {cita.titulo}{cita.enlace}."}}}
    r = await _enviar(entorno, _conversacion("web"))
    assert r.modo == "texto" and entorno["textos"][0][0] == "Cita Corte."


@pytest.mark.asyncio
async def test_una_ruta_ajena_al_evento_no_llega_cruda_al_cliente(entorno) -> None:
    entorno["mapa"] = {EVENTO: {"fallback_text_by_channel": {ac.WEB: "Tu saldo es {cargo.monto}"}}}  # no es de las citas
    r = await _enviar(entorno, _conversacion("web"))
    assert (r.modo, r.detalle) == ("omitido_canal", ac.VARIABLES_SIN_RESOLVER)
    assert entorno["textos"] == [] and entorno["omitidos"][0][2] == ac.VARIABLES_SIN_RESOLVER


@pytest.mark.asyncio
async def test_un_texto_que_queda_vacio_se_omite(entorno) -> None:
    entorno["mapa"] = {EVENTO: {"fallback_text_by_channel": {ac.WEB: "{cita.enlace}"}}}
    r = await _enviar(entorno, _conversacion("web"))
    assert (r.modo, r.detalle) == ("omitido_canal", ac.TEXTO_VACIO) and entorno["textos"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["web", "messenger", "instagram"])
async def test_canal_sin_texto_definido_no_envia_y_deja_rastro(entorno, canal: str) -> None:
    entorno["mapa"] = {EVENTO: {"fallback_text": "Texto general de WhatsApp"}}
    r = await _enviar(entorno, _conversacion(canal, ventana_horas=5))
    assert (r.modo, r.detalle) == ("omitido_canal", ac.SIN_RESPALDO)
    assert entorno["textos"] == [] and entorno["plantillas"] == []
    assert entorno["omitidos"] == [(EVENTO, canal, ac.SIN_RESPALDO)]


@pytest.mark.asyncio
@pytest.mark.parametrize("canal", ["messenger", "instagram"])
async def test_messenger_e_instagram_respetan_la_ventana(entorno, canal: str) -> None:
    r = await _enviar(entorno, _conversacion(canal, ventana_horas=5), key="k:ok")
    assert r.modo == "texto" and entorno["textos"][0][0] == ESPERADO
    entorno["textos"].clear()
    for ventana in (-2, None):  # vencida y desconocida
        r = await _enviar(entorno, _conversacion(canal, ventana_horas=ventana), key=f"k:{ventana}")
        assert (r.modo, r.detalle) == ("omitido_canal", ac.VENTANA_CERRADA)
    assert entorno["textos"] == []


@pytest.mark.asyncio
async def test_si_la_ventana_vence_en_la_carrera_se_omite_y_no_se_reintenta(entorno, monkeypatch) -> None:
    from core.shared.exceptions import OutboundWindowExpiredError

    async def _vencida_si_se_pide(*_a, raise_on_window_expired=False, **_k):
        assert raise_on_window_expired is True  # el sender real solo lanza si se lo piden; si no, guarda `failed` y da False
        raise OutboundWindowExpiredError("vencida")

    monkeypatch.setattr(sender_mod, "send_to_conversation", _vencida_si_se_pide)
    r = await _enviar(entorno, _conversacion("messenger", ventana_horas=1))
    assert (r.modo, r.detalle) == ("omitido_canal", ac.VENTANA_CERRADA)  # sin NoticeNotSentError (el outbox no reintenta)


@pytest.mark.asyncio
async def test_un_envio_que_no_se_encola_se_reintenta(entorno) -> None:
    entorno["enviado"] = False
    with pytest.raises(notices.NoticeNotSentError):
        await _enviar(entorno, _conversacion("web"))


@pytest.mark.asyncio
async def test_qr_envia_solo_si_la_politica_lo_permite(entorno) -> None:
    entorno["proveedor"] = "waweb"
    assert (await _enviar(entorno, _conversacion("whatsapp"), key="k:a")).modo == "texto"
    for bloqueo in (ac.SOLO_RESPUESTA, ac.TOPE_O_PAUSA):
        entorno["textos"].clear()
        entorno["bloqueo"] = bloqueo
        r = await _enviar(entorno, _conversacion("whatsapp"), key=f"k:{bloqueo}")
        assert (r.modo, r.detalle) == ("omitido_canal", bloqueo)
        assert entorno["textos"] == [] and (EVENTO, "whatsapp", bloqueo) in entorno["omitidos"]


@pytest.mark.asyncio
async def test_qr_no_entrega_cobranza_aunque_haya_texto(entorno) -> None:
    """Sin garantía de horario legal en la entrega por QR, la cobranza sigue el camino de antes (sin plantilla:
    ventana desconocida = cerrada → `sin_plantilla` visible), nunca el texto."""
    entorno["proveedor"] = "waweb"
    evento = "subscription.charge_reminder"
    entorno["mapa"] = {evento: {"fallback_text_by_channel": {ac.QR: "Debes {cargo.monto}"}}}
    r = await _enviar(entorno, _conversacion("whatsapp"), evento=evento)
    assert r.modo == notices.SIN_PLANTILLA and entorno["textos"] == []
    assert (evento, "whatsapp", ac.COBRANZA_SIN_HORARIO) in entorno["omitidos"]


@pytest.mark.asyncio
async def test_whatsapp_meta_no_cambia_con_la_bandera_encendida(entorno) -> None:
    entorno["proveedor"] = "meta"
    entorno["mapa"] = {EVENTO: {"fallback_text": "Texto de siempre", "fallback_text_by_channel": {ac.WEB: "otro"}}}
    r = await _enviar(entorno, _conversacion("whatsapp", ventana_horas=3))
    assert r.modo == "texto" and entorno["textos"][0][0] == "Texto de siempre"  # el de WhatsApp, no el del canal web
    r = await _enviar(entorno, _conversacion("whatsapp", ventana_horas=-3), key="k:2")
    assert r.modo == notices.SIN_PLANTILLA  # ventana cerrada sin plantilla: igual que antes
    assert entorno["omitidos"] == []


@pytest.mark.asyncio
async def test_con_la_bandera_apagada_un_canal_de_texto_no_usa_el_camino_nuevo(entorno) -> None:
    """`por_canal=False` (el valor por defecto): ni se mira el texto por canal ni el proveedor."""
    entorno["proveedor"] = "waweb"
    r = await _enviar(entorno, _conversacion("whatsapp"), por_canal=False)
    assert r.modo == notices.SIN_PLANTILLA and entorno["textos"] == [] and entorno["omitidos"] == []


# ---------------------------------------------------------------- regresión: bandera apagada, los 8 eventos
CONTEXTO_COMPLETO = {
    "cliente": {"nombre": "Ana"}, "suscripcion": {"concepto": "Plan Pro"},
    "cargo": {"monto": "$1", "moneda": "COP", "periodo": "2026-10", "fecha_vencimiento": "2026-11-01", "referencia": "r",
              "link": "https://pago", "datos_transferencia": "cta", "forma_de_pago": "link"},
    "recordatorio": {"dias": "hoy"}, "mora": {"dias": 3}, "prueba": {"termina": "2026-11-05"},
    "cita": {"titulo": "Corte", "fecha": "2026-11-02", "hora": "10:00", "zona": "America/Bogota", "enlace": ""},
    "negocio": {"nombre": "Negocio"}, "invitacion": {"correo": "a***@x.com"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("evento", sorted(notices.EVENTOS))
async def test_regresion_bandera_apagada_los_8_eventos_se_comportan_como_antes(entorno, monkeypatch, evento: str) -> None:
    assert len(notices.EVENTOS) == 8
    plantilla = SimpleNamespace(
        id=uuid.uuid4(), name="aviso", language="es", category="UTILITY", parameter_format="POSITIONAL",
        components=[{"type": "BODY", "text": "Hola {{1}}"}],
    )

    async def _aprobada(_c, _n, _i):
        return plantilla

    monkeypatch.setattr(notices, "approved_template_for", _aprobada)
    mapa = {evento: {"template": "aviso", "language": "es", "variables": ["cliente.nombre"],
                     "fallback_text": "Aviso para {cliente.nombre}", "fallback_text_by_channel": {ac.WEB: "no se usa"}}}
    entorno["mapa"] = mapa

    # Ventana abierta → texto de respaldo (el general, nunca el del canal).
    entorno["con"](_conversacion("whatsapp", ventana_horas=2))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:a")
    assert r.modo == "texto" and r.detalle == ""
    assert entorno["textos"] == [("Aviso para Ana", {"client_message_id_prefix": f"{evento}:a"})]

    # Ventana cerrada con plantilla aprobada → plantilla.
    entorno["con"](_conversacion("whatsapp", ventana_horas=-5))
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:b")
    assert r.modo == "plantilla" and len(entorno["plantillas"]) == 1

    # Ventana cerrada sin plantilla mapeada → `sin_plantilla`.
    entorno["mapa"] = {evento: {"fallback_text": "x", "fallback_text_by_channel": {ac.WEB: "no se usa"}}}
    async with tenant_scope(TENANT):
        r = await notices.send_event_notice(uuid.uuid4(), evento, CONTEXTO_COMPLETO, notice_key=f"{evento}:c")
    assert (r.modo, r.detalle) == (notices.SIN_PLANTILLA, "evento sin plantilla")
    assert entorno["omitidos"] == []
    assert len(entorno["textos"]) == 1 and len(entorno["plantillas"]) == 1  # nada más salió


# ---------------------------------------------------------------- el guardado de los textos por canal
def test_la_cobranza_no_acepta_texto_para_qr() -> None:
    from core.api.routers.settings import _textos_por_canal

    for evento in sorted(ac.COBRANZA):
        vacio, error = _textos_por_canal(evento, {ac.QR: "Debes {cargo.monto}"})
        assert vacio == {} and "QR" in error, evento
        ok, error = _textos_por_canal(evento, {ac.WEB: "Debes {cargo.monto}"})
        assert ok and not error, evento


def test_los_textos_por_canal_se_validan_al_guardar() -> None:
    from core.api.routers.settings import _textos_por_canal

    ok, error = _textos_por_canal(EVENTO, {ac.WEB: "  Hola {cliente.nombre}  ", ac.QR: "   "})
    assert (ok, error) == ({ac.WEB: "Hola {cliente.nombre}"}, "")  # recorta y descarta lo vacío
    for malo, parte in (
        ({"email_smtp": "x"}, "Canal sin texto"),  # solo los tipos sin plantillas
        ({"whatsapp_meta": "x"}, "Canal sin texto"),  # WhatsApp Meta usa su plantilla
        ({ac.WEB: "Debes {cargo.monto}"}, "Variables no permitidas"),  # ruta de otro evento
        ({ac.WEB: "Hola {nombre}"}, "Variables no permitidas"),  # el dialecto de una palabra no es de eventos
        ({ac.WEB: "x" * 1001}, "1000"),
    ):
        vacio, error = _textos_por_canal(EVENTO, malo)
        assert vacio == {} and parte in error, malo


@pytest.mark.asyncio
async def test_el_handler_con_la_bandera_apagada_no_busca_fuera_de_whatsapp(monkeypatch) -> None:
    import core.events.core_handlers as handlers

    buscadas: list[str] = []

    async def _sin_whatsapp(_customer_id):
        return None

    async def _apagada():
        return False

    async def _no_deberia(*_a, **_k):
        buscadas.append("fuera de whatsapp")

    monkeypatch.setattr(handlers, "_whatsapp_conversation_for", _sin_whatsapp)
    monkeypatch.setattr(avisos_bandera, "habilitada_actual", _apagada)
    monkeypatch.setattr(ac, "conversacion_de_texto_para", _no_deberia)
    async with tenant_scope(TENANT):
        await handlers._avisar_suscripcion("appointment.reminder", {"customer_id": str(uuid.uuid4())})
    assert buscadas == []


@pytest.mark.asyncio
async def test_el_handler_con_la_bandera_encendida_busca_web_messenger_e_instagram(monkeypatch) -> None:
    import core.events.core_handlers as handlers

    vistas: list[tuple[str, str]] = []
    conversacion = _conversacion("web")

    async def _sin_whatsapp(_customer_id):
        return None

    async def _encendida():
        return True

    async def _busca(customer_id, evento):
        vistas.append(("busca", evento))
        return conversacion

    async def _envia(conversation_id, topic, context, **kw):
        vistas.append(("envia", str(kw["por_canal"])))
        return notices.NoticeResult("texto")

    class _Sesion2:
        async def get(self, modelo, id_):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(handlers, "_whatsapp_conversation_for", _sin_whatsapp)
    monkeypatch.setattr(handlers, "get_session", lambda: _Sesion2())
    monkeypatch.setattr(avisos_bandera, "habilitada_actual", _encendida)
    monkeypatch.setattr(ac, "conversacion_de_texto_para", _busca)
    monkeypatch.setattr(notices, "send_event_notice", _envia)
    async with tenant_scope(TENANT):
        await handlers._avisar_suscripcion("appointment.reminder", {"customer_id": str(uuid.uuid4())})
    assert vistas == [("busca", "appointment.reminder"), ("envia", "True")]
