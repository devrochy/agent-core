"""Perfil canónico del cliente: aliases, teléfono y validación del panel (tanda 1A)."""

from __future__ import annotations

from core.memory import perfil
from core.shared.telefono import dividir_telefono, normalizar_telefono


def test_telefono_separa_indicativo_y_local() -> None:
    assert dividir_telefono("+57 300 123 4567") == {
        "e164": "+573001234567",
        "pais": "CO",
        "indicativo": "+57",
        "local": "3001234567",
    }
    assert dividir_telefono("3001234567")["e164"] == "+573001234567"
    assert dividir_telefono("+1 305 555 0100")["pais"] == "US"
    assert normalizar_telefono("") is None


def test_lee_las_claves_viejas_sin_migracion() -> None:
    datos = perfil.leer_perfil(
        {
            "full_name": "Ana María",
            "contact_phone": "+57 300 123 4567",
            "city": "Cali",
            "cedula": "123456",
            "lat": "3.45",
            "lng": "-76.53",
        },
        external_id="whatsapp:+573001234567",
    )
    assert datos["name"] == "Ana María"
    assert datos["phone_e164"] == "+573001234567"
    assert datos["pais_telefono"] == "CO"
    assert datos["municipio"] == "Cali"
    assert datos["id_document"] == "123456"
    assert datos["mapa_url"].startswith("https://www.openstreetmap.org/?mlat=3.45")


def test_la_clave_canonica_manda_sobre_el_alias() -> None:
    datos = perfil.leer_perfil({"full_name": "Alias viejo", "name": "Nombre nuevo"})
    assert datos["name"] == "Nombre nuevo"


def test_nombre_visible_usa_alias_y_no_duplica_apellido() -> None:
    assert perfil.nombre_visible(name=None, alias="Anita", external_id="whatsapp:+57") == "Anita"
    assert perfil.nombre_visible(name="Ana", apellido="Pérez") == "Ana Pérez"
    assert perfil.nombre_visible(name="Ana Pérez", apellido="Pérez") == "Ana Pérez"
    assert perfil.nombre_visible(name=None, alias=None, external_id="whatsapp:+57") == "whatsapp:+57"


def test_valida_y_normaliza_los_campos_del_panel() -> None:
    valores, errores = perfil.validar_perfil(
        {
            "name": "  Ana  ",
            "phone": "300 123 4567",
            "email": "ana@example.co",
            "tipo_documento": "CI",
            "clasificacion": "Nuevo",
            "latitud": "3.45",
            "longitud": "-76.53",
        }
    )
    assert errores == {}
    assert valores["name"] == "Ana"
    assert valores["phone"] == "+573001234567"
    assert valores["pais_telefono"] == "CO"
    assert valores["tipo_documento"] == "cc"
    assert valores["clasificacion"] == "nuevo"
    assert valores["latitud"] == 3.45 and valores["longitud"] == -76.53


def test_rechaza_datos_invalidos_y_claves_desconocidas() -> None:
    valores, errores = perfil.validar_perfil(
        {
            "name": "",
            "email": "no-es-correo",
            "clasificacion": "inventada",
            "latitud": 100,
            "tarjeta": "4111",
        }
    )
    assert valores == {}
    assert set(errores) == {"name", "email", "clasificacion", "latitud", "tarjeta"}


def test_borrar_un_dato_escribir_vacio_y_latitud_sola_falla() -> None:
    valores, errores = perfil.validar_perfil({"address": "", "latitud": 1})
    assert valores["address"] == ""
    assert valores["latitud"] == 1.0
    assert "latitud" in errores


def test_campos_editables_traen_etiquetas_y_opciones() -> None:
    campos = {c["key"]: c for c in perfil.campos_editables()}
    assert campos["name"]["required"] is True
    assert {o["value"] for o in campos["clasificacion"]["options"]} == {
        "nuevo", "prospecto", "cliente", "vip", "inactivo", "bloqueado"
    }
    assert {o["value"] for o in campos["tipo_documento"]["options"]} >= {"cc", "nit", "pasaporte"}
    assert any(o["value"] == "CO" for o in campos["pais_telefono"]["options"])


def test_el_pais_elegido_manda_al_normalizar_un_numero_nacional() -> None:
    valores, errores = perfil.validar_perfil({"phone": "55 1234 5678", "pais_telefono": "MX"})
    assert errores == {}
    assert valores["pais_telefono"] == "MX"
    assert valores["phone"] == "+525512345678"


def test_un_telefono_internacional_manda_sobre_el_pais_guardado() -> None:
    datos = perfil.leer_perfil({"phone": "+57 300 123 4567", "pais_telefono": "MX"})
    assert datos["pais_telefono"] == "CO"
