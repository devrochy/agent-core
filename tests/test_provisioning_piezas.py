"""HU-SH-03 (ADR-0120): piezas puras del aprovisionamiento automático, sin BD.

La allowlist de vendedores, la identidad determinista del negocio (id y slug), la
limpieza de los datos del pedido, el enlace de un solo uso de la invitación y los campos
nuevos del checkout de un plan. Los escenarios Gherkin contra la base están en
`tests/integration/test_provisioning.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace

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


def test_allowlist_acepta_slugs_y_uuid_sin_espacios_ni_mayusculas(
    settings_limpios: pytest.MonkeyPatch,
) -> None:
    from core.provisioning.vendedores import vendedores_configurados

    settings_limpios.setenv(
        "PROVISIONING_SELLER_TENANTS", f" SolversHub , {str(VENDEDOR).upper()},, "
    )
    assert vendedores_configurados() == frozenset({"solvershub", str(VENDEDOR)})


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


@pytest.mark.parametrize("referencia", ["", "admin-manual", "journey", None])
def test_activacion_sin_pedido_usa_la_suscripcion_como_referencia(referencia: str | None) -> None:
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
    for malo in ("", "sin-punto", "no-es-uuid.firma", f"{iid}.", ".firma"):
        assert partes_del_token(malo) is None


def test_el_token_depende_del_secreto_de_la_app(settings_limpios: pytest.MonkeyPatch) -> None:
    from core.admin.invitations_service import token_de_invitacion

    iid = uuid.uuid4()
    settings_limpios.setenv("APP_SECRET_KEY", "secreto-a")
    a = token_de_invitacion(iid, "n")
    from core.config import get_settings

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


# ------------------------------------------------------------------ checkout del plan (pack_commerce)
def test_checkout_de_un_plan_acepta_negocio_y_vertical() -> None:
    from pack_commerce.config import CRMConfig

    cfg = CRMConfig(
        checkout_fields={"plan": ["full_name", "email", "phone", "business_name", "vertical"]}
    )
    assert "vertical" in cfg.checkout_fields["plan"]


def test_crear_pedido_ofrece_los_verticales_de_los_presets() -> None:
    from pack_commerce.skills import OrderSkill

    from core.presets import list_presets

    tool = next(t for t in OrderSkill().get_tools() if t.name == "crear_pedido")
    props = tool.parameters["properties"]["customer_data"]["properties"]
    assert props["vertical"]["enum"] == [p.id for p in list_presets()]
    assert "business_name" in props


def test_datos_alta_del_checkout_solo_guarda_lo_que_hay() -> None:
    from pack_commerce.skills import _datos_alta

    datos = _datos_alta(
        {
            "email": " Ana@Correo.co ",
            "phone": "+57 300 000 0000",
            "business_name": "  Pizzería   Roma ",
            "vertical": "restaurante",
            "full_name": "Ana",
        }
    )
    assert datos == {
        "email": "ana@correo.co",
        "telefono": "+57 300 000 0000",
        "negocio": "Pizzería Roma",
        "vertical": "restaurante",
    }
    assert _datos_alta({}) is None


def test_vertical_invalido_se_rechaza_con_las_opciones() -> None:
    from pack_commerce.skills import _vertical_invalido

    assert _vertical_invalido("restaurante") is None
    assert _vertical_invalido("") is None  # no es obligatorio salvo que el negocio lo pida
    rechazo = _vertical_invalido("Restaurante de comida rápida")
    assert rechazo is not None
    assert rechazo["field"] == "vertical"
    assert {o["id"] for o in rechazo["opciones"]} >= {"restaurante", "comercio"}


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


def test_identidad_de_servicio_no_tiene_credenciales() -> None:
    """La identidad `provisioning` solo firma la auditoría: no hay token ni contraseña."""
    import dataclasses

    from core.config import Settings
    from core.provisioning import ACTOR, PROVISIONING

    assert ACTOR == "provisioning"
    assert PROVISIONING.tipo == "service"
    assert {f.name for f in dataclasses.fields(PROVISIONING)} == {"nombre", "tipo"}
    campos = [c for c in Settings.model_fields if "provision" in c]
    assert campos == ["provisioning_seller_tenants"]


def test_invitacion_vence_en_siete_dias() -> None:
    from core.provisioning.owner import VIGENCIA_ENLACE

    assert VIGENCIA_ENLACE.days == 7


def test_el_aviso_de_whatsapp_solo_usa_rutas_permitidas() -> None:
    """Las variables del aviso salen del contexto del servidor (ADR-0108)."""
    from core.gateway.event_notices import EVENTOS, render_fallback
    from core.provisioning.owner import (
        EVENTO_INVITACION,
        TEXTO_WHATSAPP_CON_ENLACE,
        TEXTO_WHATSAPP_EN_APP,
        contexto_del_aviso,
    )

    assert EVENTO_INVITACION in EVENTOS
    inv = SimpleNamespace(
        enlace="https://panel/invitacion#t.k", email="ana@correo.co", vence_texto="16/10/2026"
    )
    ctx = contexto_del_aviso(inv, negocio="Pizzería Roma", nombre="Ana")  # type: ignore[arg-type]
    for plantilla in (TEXTO_WHATSAPP_CON_ENLACE, TEXTO_WHATSAPP_EN_APP):
        texto = render_fallback(plantilla, ctx, EVENTO_INVITACION)
        assert "{" not in texto, texto
        assert "Pizzería Roma" in texto
        assert "https://panel/invitacion#t.k" in texto
