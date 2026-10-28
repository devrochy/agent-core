"""Política de datos del cliente por módulo (HU-CRM-13, ADR-0179): evaluador, ajustes y puente."""

from __future__ import annotations

import pytest

from core.datos_cliente import AccionDatos, CampoPolitica, Nivel, evaluar, niveles_efectivos
from core.datos_cliente.ajustes import con_politica, limpiar, politica_de
from core.datos_cliente.legado import aplicar_niveles
from core.datos_cliente.registro import RegistroDeDatos

R, REC, NO = Nivel.REQUERIDO, Nivel.RECOMENDADO, Nivel.NO_PEDIR

ACCION = AccionDatos(
    id="ventas.pedido",
    modulo="ventas",
    pack="pack_x",
    etiqueta="Pedido",
    campos=(
        CampoPolitica("name", "Nombre", R),
        CampoPolitica("id_document", "Documento", R),
        CampoPolitica("email", "Correo", REC),
        CampoPolitica("address", "Dirección", NO),
        CampoPolitica("phone", "Teléfono", R, ajustable=False),
    ),
)


def test_sin_ajustes_ni_base_rigen_los_defectos_del_modulo() -> None:
    ev = evaluar(ACCION, {"name": "Ana"})
    assert ev.faltan == ["id_document", "phone"]
    assert ev.recomendados_pendientes == ["email"]
    assert ev.ya_registrados == ["name"]
    assert not ev.puede_continuar


def test_los_alias_del_perfil_cuentan_como_el_dato_canonico() -> None:
    ev = evaluar(ACCION, {"full_name": "Ana", "documento": "123", "Teléfono": "3001112233", "correo": "a@b.co"})
    assert ev.faltan == []
    assert ev.recomendados_pendientes == []
    assert set(ev.ya_registrados) == {"name", "id_document", "phone", "email"}
    assert ev.puede_continuar


def test_un_valor_vacio_no_cuenta_como_dato() -> None:
    ev = evaluar(ACCION, {"name": "  ", "id_document": None, "phone": "3001112233"})
    assert ev.faltan == ["name", "id_document"]


def test_el_ajuste_del_dueno_gana_a_la_base_del_modulo_y_al_defecto() -> None:
    base = {"name": R, "id_document": R, "email": R}
    niveles = niveles_efectivos(ACCION, {"id_document": "no_pedir", "address": "recomendado"}, base)
    assert niveles["id_document"] is NO
    assert niveles["address"] is REC
    assert niveles["email"] is R  # la base sube el defecto «recomendado»


def test_la_base_baja_lo_que_el_modulo_ya_no_exige() -> None:
    niveles = niveles_efectivos(ACCION, None, {"name": R})
    assert niveles["id_document"] is NO
    assert niveles["email"] is REC  # un recomendado no se toca


def test_el_piso_tecnico_no_se_puede_bajar() -> None:
    niveles = niveles_efectivos(ACCION, {"phone": "no_pedir"}, {"name": R})
    assert niveles["phone"] is R


def test_lo_que_no_se_pide_no_aparece_en_la_evaluacion() -> None:
    ev = evaluar(ACCION, {}, {"id_document": "no_pedir", "email": "no_pedir"})
    assert "id_document" not in ev.faltan
    assert "email" not in ev.recomendados_pendientes


def test_el_sobre_no_lleva_valores() -> None:
    sobre = evaluar(ACCION, {"name": "Ana Secreta", "phone": "3001112233"}).como_sobre()
    assert sobre["accion"] == "ventas.pedido"
    assert sobre["ya_registrados"] == ["name", "phone"]
    assert "Ana Secreta" not in str(sobre)


def test_un_nivel_invalido_en_el_ajuste_se_ignora_al_evaluar() -> None:
    assert niveles_efectivos(ACCION, {"name": "obligatorio"})["name"] is R


# ------------------------------------------------------------------ ajustes guardados
def test_limpiar_descarta_acciones_claves_pisos_y_niveles_desconocidos() -> None:
    crudo = {
        "ventas.pedido": {"id_document": "no_pedir", "phone": "no_pedir", "zzz": "requerido", "email": "mal"},
        "otra.accion": {"name": "requerido"},
    }
    assert limpiar(crudo, [ACCION]) == {"ventas.pedido": {"id_document": "no_pedir"}}
    assert limpiar("no es un dict", [ACCION]) == {}


def test_con_politica_conserva_el_resto_del_behavior_y_quita_el_bloque_vacio() -> None:
    behavior = {"palabras_de_baja": ["baja"], "datos_cliente": {"otra": 1}}
    nuevo = con_politica(behavior, {"a.b": {"name": "no_pedir"}})
    assert nuevo["palabras_de_baja"] == ["baja"]
    assert nuevo["datos_cliente"] == {"otra": 1, "politica": {"a.b": {"name": "no_pedir"}}}
    assert behavior["datos_cliente"] == {"otra": 1}  # no muta la entrada
    assert "datos_cliente" not in con_politica({"datos_cliente": {"politica": {"a.b": {}}}}, {})


def test_politica_de_sin_bloque_es_vacia() -> None:
    assert politica_de(None) == {}
    assert politica_de({"datos_cliente": "x"}) == {}


# ------------------------------------------------------------------ puente con las listas legadas
def test_sin_cambios_la_lista_legada_sale_igual() -> None:
    legado = ["full_name", "id_document", "address", "municipality", "city", "phone"]
    niveles = {"name": R, "id_document": R, "address": R, "municipio": R, "phone": R, "email": NO}
    assert aplicar_niveles(legado, niveles) == legado


def test_bajar_municipio_quita_municipality_y_city() -> None:
    legado = ["full_name", "municipality", "city", "phone"]
    niveles = {"name": R, "municipio": NO, "phone": R}
    assert aplicar_niveles(legado, niveles) == ["full_name", "phone"]


def test_subir_un_dato_lo_agrega_con_el_nombre_del_modulo() -> None:
    niveles = {"name": R, "email": R}
    assert aplicar_niveles(["full_name"], niveles, {"name": "full_name"}) == ["full_name", "email"]


def test_una_clave_legada_que_la_politica_no_conoce_se_conserva() -> None:
    assert aplicar_niveles(["business_name", "full_name"], {"name": NO}) == ["business_name"]


# ------------------------------------------------------------------ registro
def test_el_registro_filtra_por_packs_habilitados_y_rechaza_campos_repetidos() -> None:
    reg = RegistroDeDatos()
    reg.registrar(ACCION)
    assert reg.de_packs({"pack_x"}) == [ACCION]
    assert reg.de_packs({"pack_y"}) == []
    assert reg.de_packs(None) == [ACCION]
    repetida = AccionDatos("a.b", "a", "pack_x", "x", (CampoPolitica("name", "N", R), CampoPolitica("name", "N", R)))
    with pytest.raises(ValueError):
        reg.registrar(repetida)


def test_las_acciones_de_los_packs_declaran_claves_canonicas_del_perfil() -> None:
    """Toda clave de la política que sea del perfil está escrita en su forma canónica."""
    import pack_calendar.datos  # noqa: F401
    import pack_commerce.datos  # noqa: F401
    import pack_subscriptions.datos  # noqa: F401

    from core.datos_cliente import registro_datos
    from core.memory.perfil import normalizar_clave

    acciones = registro_datos.de_packs(None)
    assert {a.id for a in acciones} >= {
        "ventas.pedido_domicilio",
        "ventas.pedido_retiro",
        "ventas.plan_legado",
        "suscripciones.inscribirse",
        "agenda.agendar_cita",
    }
    for accion in acciones:
        for campo in accion.campos:
            assert normalizar_clave(campo.clave) == campo.clave, (accion.id, campo.clave)
    agendar = registro_datos.obtener("agenda.agendar_cita")
    assert agendar is not None
    assert agendar.campo("email") is not None and not agendar.campo("email").ajustable
