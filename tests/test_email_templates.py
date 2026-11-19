"""Diseño HTML de los correos (plantillas MJML compiladas + render con Jinja2).

El texto plano sigue siendo el respaldo; estos tests cubren que las cinco piezas
tengan diseño, escapen lo que viene de fuera y no filtren marcas técnicas.
"""

from pathlib import Path

import pytest

from core.gateway.channels.email import _send_sync as _channel_send
from core.gateway.channels.email import render_outbound_html
from core.gateway.email_templates import (
    html_acceso,
    html_cita,
    html_generico,
    html_informe_qa,
    html_password_reset,
    html_renovacion,
)
from core.gateway.system_email import _build_message

_PLANTILLAS = Path(__file__).parents[1] / "core" / "gateway" / "email_templates"


def _acceso(**extra: str) -> str:
    datos = {
        "titulo": "Tu acceso a SolversHub",
        "preview": "Tu negocio ya está creado.",
        "eyebrow": "Tu acceso a SolversHub",
        "saludo": "Hola Ana,",
        "mensaje": "Tu negocio «Inmobiliaria Norte» ya está creado en SolversHub.",
        "cta": "Definir mi contraseña",
        "enlace": "https://panel.solvershub.test/invitacion#token",
        "detalle": "El enlace es de un solo uso y vence el 01/01/2027.",
        "aviso": "Si no fuiste tú, ignora este correo.",
    }
    datos.update(extra)
    return html_acceso(**datos)


def _informe(*, enlace: str | None = None) -> str:
    return html_informe_qa(
        titulo="Informe de pruebas de Ana · Ciclo 1",
        ciclo="Ciclo 1",
        resumen="Ana envió su informe: 3 de 5 terminadas.",
        terminadas=3,
        total=5,
        ok=2,
        falla=1,
        bloqueada=2,
        fallas=[{"estado": "falla", "etiqueta": "Falla", "titulo": "Alta sin correo"}],
        enlace=enlace,
    )


def _todos_los_html() -> dict[str, str]:
    return {
        "password_reset": html_password_reset(codigo="ABCD2345", minutos=15),
        "access_invite": _acceso(),
        "renewal": html_renovacion(
            titulo="Tu plan Premium de SolversHub vence el 01/01/2027",
            mensaje="El período de tu plan Premium vence el 01/01/2027.",
            vence="01/01/2027",
            monto="$ 600.000",
            referencia="REF-001",
            enlace="https://panel.solvershub.test/plan",
        ),
        "qa_report": _informe(enlace="https://panel.solvershub.test/pruebas/1"),
        "appointment": html_cita(
            asunto="Tu cita quedó agendada — Inmobiliaria Norte",
            accion="confirmed",
            negocio="Inmobiliaria Norte",
            cita="Visita al apartamento modelo",
            fecha="21/11/2026",
            hora="10:00",
            enlace="https://calendar.google.com/calendar/render?action=TEMPLATE&text=x",
        ),
        "generic": html_generico(asunto="Tu cita", cuerpo="Hola,\n\nTe espero mañana."),
    }


@pytest.mark.parametrize(
    "nombre", ["password_reset", "access_invite", "renewal", "qa_report", "appointment", "generic"]
)
def test_cada_correo_tiene_diseno_propio(nombre: str) -> None:
    html = _todos_los_html()[nombre]
    assert html.lstrip().lower().startswith("<!doctype html")
    assert "SOLVERS" in html and "HUB" in html
    assert "max-width:600px" in html


@pytest.mark.parametrize(
    "nombre", ["password_reset", "access_invite", "renewal", "qa_report", "appointment", "generic"]
)
def test_no_quedan_marcadores_ni_marcas_tecnicas(nombre: str) -> None:
    html = _todos_los_html()[nombre]
    assert "{{" not in html and "{%" not in html
    assert "agent-core" not in html


def test_toda_plantilla_mjml_tiene_html_compilado() -> None:
    fuentes = {p.stem for p in (_PLANTILLAS / "src").glob("*.mjml")}
    compiladas = {p.stem for p in (_PLANTILLAS / "html").glob("*.html")}
    assert fuentes == compiladas


def test_el_html_escapa_lo_que_viene_de_fuera() -> None:
    html = _acceso(
        saludo="Hola <script>alert(1)</script>,",
        mensaje="Negocio «A & B <img src=x>».",
        enlace="https://panel.test/invitacion?a=1&b=2",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "A &amp; B" in html
    assert "<img src=x>" not in html
    assert "https://panel.test/invitacion?a=1&amp;b=2" in html


def test_el_correo_del_asistente_conserva_parrafos_y_saltos() -> None:
    html = html_generico(asunto="Tu cita", cuerpo="Hola,\n\nTe espero <b>mañana</b>.\nTrae el documento.")
    assert html.count('<p style="margin:0 0 12px;') == 2
    assert "<br />" in html
    assert "&lt;b&gt;mañana&lt;/b&gt;" in html
    assert "<b>mañana</b>" not in html


def test_el_informe_muestra_el_boton_solo_si_hay_enlace() -> None:
    assert "Ver el informe completo" not in _informe()
    assert "Ver el informe completo" in _informe(enlace="https://panel.test/pruebas/1")


def test_el_informe_escapa_los_titulos_de_las_pruebas() -> None:
    html = html_informe_qa(
        titulo="Informe",
        ciclo="Ciclo",
        resumen="Resumen",
        terminadas=0,
        total=1,
        ok=1,
        falla=0,
        bloqueada=0,
        fallas=[{"estado": "falla", "etiqueta": "Falla", "titulo": "<img src=x onerror=1>"}],
        enlace=None,
    )
    assert "<img src=x" not in html


def test_correo_de_sistema_lleva_texto_y_html() -> None:
    msg = _build_message("no-reply@solvershub.test", "ana@example.com", "Hola", "Texto plano", "<p>HTML</p>")
    assert msg.is_multipart()
    tipos = [parte.get_content_type() for parte in msg.iter_parts()]
    assert tipos == ["text/plain", "text/html"]
    partes = list(msg.iter_parts())
    assert partes[0].get_content().strip() == "Texto plano"
    assert partes[1].get_content().strip() == "<p>HTML</p>"


def test_correo_de_sistema_sin_html_sigue_siendo_texto_plano() -> None:
    msg = _build_message("no-reply@solvershub.test", "ana@example.com", "Hola", "Texto plano")
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"


def test_el_canal_envuelve_el_texto_del_asistente_en_el_diseno(monkeypatch: pytest.MonkeyPatch) -> None:
    credenciales = {"host": "smtp.test", "port": 587, "from_address": "negocio@test.co"}
    capturado: list[dict] = []

    class _Smtp:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "_Smtp":
            return self

        def __exit__(self, *args) -> None:
            return None

        def starttls(self, context=None) -> None:
            return None

        def login(self, username: str, password: str) -> None:
            return None

        def send_message(self, msg) -> None:
            capturado.append({"multipart": msg.is_multipart(), "tipos": [p.get_content_type() for p in msg.iter_parts()]})

    monkeypatch.setattr("core.gateway.channels.email.smtplib.SMTP", _Smtp)
    html = render_outbound_html("Tu cita", "Hola,\n\nTe espero.")
    assert html and "SOLVERS" in html
    _channel_send(credenciales, "ana@example.com", "Tu cita", "Hola,\n\nTe espero.", html)
    assert capturado == [{"multipart": True, "tipos": ["text/plain", "text/html"]}]


def test_todos_los_correos_llevan_el_isotipo() -> None:
    for html in _todos_los_html().values():
        assert '<img src="https://www.solvershub.site/logo.png"' in html


def test_el_correo_de_cita_convierte_el_enlace_de_calendario_en_boton() -> None:
    enlace = (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        "&text=Cita+en+Inmobiliaria+Norte&dates=20261121T150000Z/20261121T160000Z"
    )
    html = html_generico(
        asunto="Tu cita quedó agendada — Inmobiliaria Norte",
        cuerpo=(
            "Hola Laura,\n\nTu cita quedó agendada para el 21/11/2026 a las 10:00.\n\n"
            f"Agregá la cita a tu calendario: {enlace}\n\n— Inmobiliaria Norte"
        ),
    )
    assert "Agregar a mi calendario" in html
    assert "calendar/render?action=TEMPLATE" in html
    assert "Agregá la cita a tu calendario" not in html


def test_un_correo_sin_enlace_de_calendario_no_muestra_el_boton() -> None:
    html = html_generico(asunto="Hola", cuerpo="Un mensaje cualquiera.")
    assert "Agregar a mi calendario" not in html


def test_la_ficha_de_cita_lleva_los_datos_y_el_boton() -> None:
    html = html_cita(
        asunto="Tu cita quedó agendada — Inmobiliaria Norte",
        accion="confirmed",
        negocio="Inmobiliaria Norte",
        cita="Visita al apartamento modelo",
        fecha="21/11/2026",
        hora="10:00",
        enlace="https://calendar.google.com/calendar/render?action=TEMPLATE&text=x",
    )
    assert "Confirmada" in html
    assert "Visita al apartamento modelo" in html
    assert "21/11/2026" in html and "10:00" in html
    assert "Inmobiliaria Norte" in html
    assert "Agregar a mi calendario" in html


def test_la_cita_cancelada_no_muestra_el_boton_de_calendario() -> None:
    html = html_cita(
        asunto="Tu cita fue cancelada — Inmobiliaria Norte",
        accion="cancelled",
        negocio="Inmobiliaria Norte",
        cita="Visita",
        fecha="21/11/2026",
        hora="10:00",
    )
    assert "Cancelada" in html
    assert "Agregar a mi calendario" not in html


def test_la_ficha_de_cita_escapa_lo_que_viene_de_fuera() -> None:
    html = html_cita(
        asunto="Tu cita",
        accion="confirmed",
        negocio="A & B <script>x</script>",
        cita="<img src=x onerror=1>",
        fecha="21/11/2026",
        hora="10:00",
    )
    assert "<script>" not in html and "<img src=x" not in html


def test_el_canal_puede_enviar_texto_y_html_desde_la_cola() -> None:
    import asyncio

    from core.gateway.channels.base import OutboundAdapter

    class _Canal(OutboundAdapter):
        provider = "prueba"

        async def send_text(self, credentials, to_external_id, text) -> str:
            return f"texto:{text}"

        async def send_image(self, credentials, to_external_id, image_url, caption="") -> str:
            return "imagen"

    adapter = _Canal()
    assert asyncio.run(adapter.send_text_with_html({}, "a@b.co", "hola", "<p>hola</p>")) == "texto:hola"
