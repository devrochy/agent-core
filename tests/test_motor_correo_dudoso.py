"""Correos con el dominio mal escrito (smoke de test 2026-10-20: el acceso del negocio nuevo se
fue a `…@gmai.com`) y códigos de error de los comandos en la auditoría."""

from __future__ import annotations

import pytest

from core.kernel.procedures import comandos, motor
from core.kernel.procedures.textos import codigo_de_error, mensaje


@pytest.mark.parametrize(
    ("correo", "sugerido"),
    [
        ("devrochy26@gmai.com", "gmail.com"),
        ("a@GMIAL.com", "gmail.com"),
        ("b@hotmial.com", "hotmail.com"),
        ("c@outlok.com", "outlook.com"),
        ("d@gmail.co", "gmail.com"),
    ],
)
def test_dominios_mal_escritos(correo: str, sugerido: str) -> None:
    assert motor.dominio_sugerido(correo) == sugerido


@pytest.mark.parametrize(
    "correo",
    ["a@gmail.com", "a@hotmail.com", "a@hotmail.es", "a@outlook.es", "a@tienda.co", "a@empresa.com.co", "sin-arroba"],
)
def test_los_dominios_validos_no_se_tocan(correo: str) -> None:
    assert motor.dominio_sugerido(correo) is None


def test_la_tabla_es_coherente() -> None:
    tabla = motor.dominios_mal_escritos()
    assert tabla and all(bueno not in tabla and malo != bueno for malo, bueno in tabla.items())


def test_la_correccion_sugerida_cuenta_como_dicha_si_el_cliente_la_acepta() -> None:
    escrito = "5. devrochy26@gmai.com. 6. 3184935933"
    # El modelo no corrige solo: el cliente tiene que nombrar el proveedor correcto.
    assert not motor.dicho_por_el_cliente(["devrochy26@gmail.com"], [escrito])
    textos = [escrito, "sí, era gmail"]
    assert motor.dicho_por_el_cliente(["devrochy26@gmail.com"], textos)
    # Solo la corrección de la tabla y con la misma parte local.
    assert not motor.dicho_por_el_cliente(["otro@gmail.com"], textos)
    assert not motor.dicho_por_el_cliente(["devrochy26@hotmail.com"], textos)


def test_el_mensaje_del_motor_lleva_la_sugerencia() -> None:
    texto = mensaje("correo_dudoso", dato="email", sugerido="gmail.com")
    assert "@gmail.com" in texto
    assert codigo_de_error(texto) == "correo_dudoso"


def test_codigos_de_error_sin_textos() -> None:
    errores = [
        mensaje("dato_del_servidor", dato="quote_id"),
        mensaje("resolver_rechazo", dato="vertical", error="Tipo de negocio no válido."),
        comandos.texto("comando_no_disponible", comando="actualizar_tarea"),
        comandos.texto("argumentos_invalidos", comando="actualizar_tarea"),
        "algo que no es del motor",
    ]
    assert comandos.codigos_de_errores(errores) == [
        "dato_del_servidor", "resolver_rechazo", "comando_no_disponible", "argumentos_invalidos", "otro",
    ]
