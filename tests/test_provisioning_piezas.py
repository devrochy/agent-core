"""HU-SH-03 (ADR-0121): piezas puras del aprovisionamiento automático, sin BD.

La allowlist de vendedores (solo UUID), la identidad determinista del negocio (id y slug),
la limpieza de los datos del pedido (sin enlaces), el enlace de un solo uso de la
invitación, el aviso neutro por WhatsApp y los campos del checkout de un plan. Los
escenarios Gherkin contra la base están en `tests/integration/test_provisioning.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

VENDEDOR = uuid.UUID("20000000-0000-0000-0000-000000000001")
OTRO_VENDEDOR = uuid.UUID("20000000-0000-0000-0000-000000000002")


@pytest.fixture
def settings_limpios(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    from core.config import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


# ------------------------------------------------------------------ allowlist
def test_allowlist_vacia_apaga_el_alta(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.provisioning.vendedores import vendedores_configurados

    settings_limpios.setenv("PROVISIONING_SELLER_TENANTS", "")
    assert vendedores_configurados() == frozenset()


def test_allowlist_solo_acepta_uuid(settings_limpios: pytest.MonkeyPatch) -> None:
    """Un slug se ignora: el de un negocio borrado podría volver a usarse."""
    from core.provisioning.vendedores import vendedores_configurados

    settings_limpios.setenv(
        "PROVISIONING_SELLER_TENANTS", f" solvershub , {str(VENDEDOR).upper()},, no-es-uuid "
    )
    assert vendedores_configurados() == frozenset({VENDEDOR})


# ------------------------------------------------------------------ identidad del negocio
def test_id_del_negocio_es_determinista_por_vendedor_y_referencia() -> None:
    from core.provisioning.consumer import id_del_negocio

    assert id_del_negocio(VENDEDOR, "SH-0007") == id_del_negocio(VENDEDOR, "SH-0007")
    assert id_del_negocio(VENDEDOR, "SH-0007") != id_del_negocio(VENDEDOR, "SH-0008")
    assert id_del_negocio(VENDEDOR, "SH-0007") != id_del_negocio(OTRO_VENDEDOR, "SH-0007")


def test_slug_determinista_valido_y_legible() -> None:
    from core.onboarding import _validate_slug
    from core.provisioning.consumer import slug_del_negocio

    slug = slug_del_negocio("Pizzería Roma & Cía.", VENDEDOR, "SH-0007")
    assert slug == slug_del_negocio("Pizzería Roma & Cía.", VENDEDOR, "SH-0007")
    assert slug.startswith("pizzeria-roma-cia-")
    _validate_slug(slug)  # no lanza
    # Otra referencia u otro vendedor: otro negocio, otro slug aunque el nombre sea igual.
    assert slug != slug_del_negocio("Pizzería Roma & Cía.", VENDEDOR, "SH-0008")
    assert slug != slug_del_negocio("Pizzería Roma & Cía.", OTRO_VENDEDOR, "SH-0007")


@pytest.mark.parametrize("nombre", ["", "   ", "###", "日本語", "a" * 300])
def test_slug_de_nombres_raros_sigue_siendo_valido(nombre: str) -> None:
    from core.onboarding import _validate_slug
    from core.provisioning.consumer import slug_del_negocio

    slug = slug_del_negocio(nombre, VENDEDOR, "SH-0001")
    _validate_slug(slug)
    assert len(slug) <= 64


def test_nombre_del_negocio_se_limpia_y_tiene_respaldo() -> None:
    from core.provisioning.consumer import nombre_del_negocio

    assert nombre_del_negocio({"negocio": "  Pizzería\n\tRoma  "}, "SH-1") == "Pizzería Roma"
    assert nombre_del_negocio({"negocio": "", "nombre": "Ana Pérez"}, "SH-1") == "Ana Pérez"
    assert nombre_del_negocio({}, "SH-0042") == "Negocio SH-0042"
    assert len(nombre_del_negocio({"negocio": "x" * 500}, "SH-1")) == 80


@pytest.mark.parametrize(
    "nombre", ["Entra a www.evil.co", "https://phish.example", "Pizzería roma-pagos.com", "a@b.co"]
)
def test_nombre_del_negocio_con_un_enlace_se_descarta(nombre: str) -> None:
    """El nombre va en el correo y en el aviso: con un enlace sirve para phishing."""
    from core.provisioning.consumer import nombre_del_negocio

    assert nombre_del_negocio({"negocio": nombre, "nombre": "Ana"}, "SH-1") == "Ana"
    assert nombre_del_negocio({"negocio": nombre, "nombre": "visita x.com"}, "SH-1") == "Negocio SH-1"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Pizzería Roma S.A.S.", False),
        ("Distribuidora 2.0", False),
        ("Café & Cía. Ltda", False),
        ("www.algo", True),
        ("http://x", True),
        ("mi-negocio.co", True),
        ("Tienda Gmail.Com", True),
        ("visita x.com", True),
        ("a@b.co", True),
        # Homógrafos y dominios Unicode: «а» cirílica, puntos ideográficos y de ancho completo.
        ("p\u0430ypal.com", True),
        ("evil\u3002com", True),
        ("evil\uff0ecom", True),
        ("evil\uff61com", True),
        ("\uff45\uff56\uff49\uff4c\uff0e\uff43\uff4f\uff4d", True),  # todo de ancho completo
        ("пример.рф", True),
        ("\uff57\uff57\uff57.algo", True),
        # Nombres legítimos con tildes y «ñ» pasan.
        ("Panadería La Ñapa", False),
        ("Señor Café S.A.S.", False),
        ("Peluquería Muñoz & Hijos Ltda.", False),
    ],
)
def test_contiene_url(texto: str, esperado: bool) -> None:
    from core.shared.texto_seguro import contiene_url

    assert contiene_url(texto) is esperado


def test_correo_enmascarado() -> None:
    from core.shared.texto_seguro import enmascarar_correo

    assert enmascarar_correo("ana.perez@correo.co") == "a***@correo.co"
    assert enmascarar_correo("x@y.co") == "x***@y.co"
    assert enmascarar_correo("sin-arroba") == "***"
    assert enmascarar_correo(None) == "***"


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("  Ana@Correo.CO ", "ana@correo.co"),
        ("sin-arroba.co", None),
        ("a@b", None),
        ("dos@@correo.co", None),
        ("con espacio@correo.co", None),
        (None, None),
        ("x" * 250 + "@correo.co", None),
    ],
)
def test_correo_del_owner(valor: str | None, esperado: str | None) -> None:
    from core.provisioning.consumer import correo_valido

    assert correo_valido(valor) == esperado


def test_huella_del_correo_no_guarda_el_correo() -> None:
    from core.provisioning.consumer import huella_del_correo

    huella = huella_del_correo("ana@correo.co")
    assert huella == huella_del_correo("ana@correo.co")
    assert "ana" not in huella and len(huella) == 64


@pytest.mark.parametrize("referencia", ["", "   ", None])
def test_activacion_sin_pedido_usa_la_suscripcion_como_referencia(referencia: str | None) -> None:
    """El emisor manda `referencia` nula cuando no hubo pedido (ADR-0121)."""
    from core.provisioning.consumer import referencia_del_alta

    sub = uuid.uuid4()
    assert referencia_del_alta(referencia, sub) == (f"sub:{sub}", True)
    assert referencia_del_alta(" SH-0003 ", sub) == ("SH-0003", False)


# ------------------------------------------------------------------ enlace de la invitación
def test_token_de_invitacion_firma_y_rechaza_alteraciones() -> None:
    from core.admin.invitations_service import partes_del_token, token_de_invitacion

    iid = uuid.uuid4()
    token = token_de_invitacion(iid, "nonce-1")
    assert token.startswith(f"{iid}.")
    assert partes_del_token(token) == (iid, token.split(".", 1)[1])
    # Mismo id y nonce: mismo token (un reintento reenvía el mismo enlace).
    assert token == token_de_invitacion(iid, "nonce-1")
    # Otro nonce u otro id: otra firma.
    assert token != token_de_invitacion(iid, "nonce-2")
    assert token.split(".", 1)[1] != token_de_invitacion(uuid.uuid4(), "nonce-1").split(".", 1)[1]
    for malo in ("", "sin-punto", "no-es-uuid.firma", f"{iid}.", ".firma", f"{iid}." + "x" * 300):
        assert partes_del_token(malo) is None


def test_el_token_depende_del_secreto_de_la_app(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.admin.invitations_service import token_de_invitacion
    from core.config import get_settings

    iid = uuid.uuid4()
    settings_limpios.setenv("APP_SECRET_KEY", "secreto-a")
    a = token_de_invitacion(iid, "n")
    get_settings.cache_clear()
    settings_limpios.setenv("APP_SECRET_KEY", "secreto-b")
    assert token_de_invitacion(iid, "n") != a


def test_el_enlace_lleva_el_token_en_el_fragmento(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.admin.invitations_service import enlace_de_invitacion

    settings_limpios.setenv("PANEL_BASE_URL", "https://panel.ejemplo.co/")
    assert enlace_de_invitacion("abc.def") == "https://panel.ejemplo.co/invitacion#abc.def"


def test_la_cuenta_sin_contrasena_no_puede_entrar() -> None:
    from core.admin.auth import verify_password
    from core.admin.invitations_service import SIN_CONTRASENA

    for intento in ("", "!", SIN_CONTRASENA, "admin123"):
        assert verify_password(intento, SIN_CONTRASENA) is False


# ------------------------------------------------------------------ correo y aviso
def _invitacion(con_enlace: bool):  # type: ignore[no-untyped-def]
    from core.provisioning.owner import InvitacionOwner

    return InvitacionOwner(
        invitation_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        email="ana@correo.co",
        con_enlace=con_enlace,
        enlace="https://panel/invitacion#t.k" if con_enlace else "https://panel",
        vence=datetime(2026, 10, 16, tzinfo=UTC) if con_enlace else None,
    )


@pytest.mark.parametrize("con_enlace", [True, False])
def test_el_asunto_del_correo_no_lleva_el_nombre_del_negocio(con_enlace: bool) -> None:
    from core.provisioning.owner import asunto_y_cuerpo

    asunto, cuerpo = asunto_y_cuerpo(_invitacion(con_enlace), negocio="Pizzería Roma", nombre="Ana")
    assert "Pizzería" not in asunto
    assert "Pizzería Roma" in cuerpo
    assert ("#t.k" in cuerpo) is con_enlace


def test_el_aviso_de_whatsapp_no_lleva_enlace_ni_token_y_enmascara_el_correo() -> None:
    """El enlace va SOLO por correo y el aviso es el mismo en todos los casos (ADR-0121)."""
    from core.gateway.event_notices import EVENTOS, render_fallback
    from core.provisioning.owner import EVENTO_INVITACION, TEXTO_WHATSAPP, contexto_del_aviso

    assert set(EVENTOS[EVENTO_INVITACION]) == {"cliente.nombre", "negocio.nombre", "invitacion.correo"}
    contexto = contexto_del_aviso(negocio="Pizzería Roma", nombre="Ana", email="ana.perez@correo.co")
    assert "enlace" not in contexto["invitacion"]
    texto = render_fallback(TEXTO_WHATSAPP, contexto, EVENTO_INVITACION)
    assert "{" not in texto, texto
    assert "Pizzería Roma" in texto and "a***@correo.co" in texto
    assert "ana.perez" not in texto and "http" not in texto
    # Una plantilla del vendedor que pida el enlace no lo obtiene: la ruta no existe.
    assert render_fallback("{invitacion.enlace}", contexto, EVENTO_INVITACION) == "{invitacion.enlace}"


def test_invitacion_vence_en_siete_dias() -> None:
    from core.provisioning.owner import VIGENCIA_ENLACE

    assert VIGENCIA_ENLACE.days == 7


# ------------------------------------------------------------------ checkout (pack_commerce)
def test_la_tool_crear_pedido_ya_no_declara_los_datos_del_alta() -> None:
    """HU-COM-17 (SPEC-43): `business_name`, `vertical` y `acepta_terminos` eran del pedido de un plan;
    ahora el alta la pide Suscripciones, así que `crear_pedido` no los declara."""
    from pack_commerce.skills import OrderSkill

    tool = next(t for t in OrderSkill().get_tools() if t.name == "crear_pedido")
    datos = tool.parameters["properties"]["customer_data"]
    for campo in ("business_name", "vertical", "acepta_terminos"):
        assert campo not in datos["properties"]
    assert "required" not in datos
    assert tool.parameters["required"] == ["quote_id", "payment_method", "customer_data"]


# ------------------------------------------------------------------ fuente de datos de alta
def test_registro_de_fuentes_es_idempotente() -> None:
    from core.provisioning.fuentes import FuenteDeAltas, FuentesDeAltasRegistry

    class _Fuente:
        provider_id = "pack_x"

        async def datos_de_alta(self, referencia: str, *, variant_id: str | None = None):  # type: ignore[no-untyped-def]
            return None

    registro = FuentesDeAltasRegistry()
    fuente = _Fuente()
    assert isinstance(fuente, FuenteDeAltas)
    registro.register("pack_x", fuente)
    registro.register("pack_x", fuente)
    assert registro.provider_ids() == ["pack_x"]
    registro.unregister("pack_x")
    assert registro.provider_ids() == []


def test_la_identidad_de_servicio_no_tiene_credenciales() -> None:
    """`provisioning` solo firma la auditoría: no hay token, contraseña ni config de servicio."""
    import core.provisioning as provisioning
    from core.config import Settings

    assert provisioning.ACTOR == "provisioning"
    assert not [n for n in vars(provisioning) if "token" in n.lower() or "secret" in n.lower()]
    assert [c for c in Settings.model_fields if "provision" in c] == ["provisioning_seller_tenants"]


def test_contexto_del_aviso_acepta_cualquier_invitacion() -> None:
    """El aviso no depende de la invitación (con o sin enlace, o sin invitación)."""
    from core.provisioning.owner import contexto_del_aviso

    assert contexto_del_aviso(negocio="N", nombre=None, email="x@y.co")["cliente"]["nombre"] == "cliente"
