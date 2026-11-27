"""HU-AOS-104 · piezas puras del arreglo sugerido y del informe de las pruebas del agente (sin base ni red)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.pruebas.arreglo import Arreglo, ArregloNoAplicable, a_json, leer, preparar_arreglo
from core.pruebas.informe import porcentaje

REGLA = {"name": "No prometer hoy", "kind": "forbidden_phrase", "action": "retry", "phrases": ["hoy mismo"]}


def _regla(**extra: object) -> Arreglo:
    return Arreglo.model_validate({"tipo": "regla", "porque": "Porque sí.", "regla": REGLA, **extra})


@pytest.mark.parametrize(
    "pasan, fallan, esperado",
    [(0, 0, None), (3, 0, 100), (0, 2, 0), (1, 1, 50), (1, 2, 33), (2, 1, 67)],
)
def test_porcentaje_cuenta_lo_que_se_pudo_juzgar(pasan: int, fallan: int, esperado: int | None) -> None:
    assert porcentaje(pasan, fallan) == esperado


@pytest.mark.parametrize(
    "crudo",
    [
        {"tipo": "regla", "porque": "x"},  # sin regla
        {"tipo": "regla", "porque": "x", "regla": REGLA, "texto": "otra cosa"},  # campos de otro tipo
        {"tipo": "regla", "porque": "x", "regla": {**REGLA, "kind": "required_tool", "tool": None}},  # contrato
        {"tipo": "regla", "porque": "x", "regla": {**REGLA, "ejecutar": "rm -rf"}},  # clave desconocida
        {"tipo": "respuesta", "porque": "x"},  # sin texto
        {"tipo": "respuesta", "porque": "x", "texto": "t", "regla": REGLA},
        {"tipo": "dato", "porque": "x", "texto": "t"},  # sin sección
        {"tipo": "dato", "porque": "x", "texto": "t", "seccion": "nomina"},  # sección fuera de la lista
        {"tipo": "dato", "porque": "x", "texto": "t", "seccion": "catalogo", "procedimiento": "a.b"},
        {"tipo": "otro", "porque": "x", "texto": "t"},
        {"tipo": "regla", "porque": "", "regla": REGLA},
        {"tipo": "regla", "porque": "x", "regla": REGLA, "publicar": True},
        {"tipo": "regla", "porque": "x", "regla": {"name": "Corto", "kind": "max_length", "value": 1}},  # demasiado amplia
        {"tipo": "regla", "porque": "x", "regla": {**REGLA, "phrases": ["a"]}},
        {"tipo": "respuesta", "porque": "x", "texto": "t", "procedimiento": "no es un id"},
    ],
)
def test_un_arreglo_mal_formado_no_valida(crudo: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Arreglo.model_validate(crudo)


def test_solo_la_regla_es_aplicable_y_leer_no_se_fia_de_la_base() -> None:
    assert _regla().aplicable
    assert not Arreglo.model_validate({"tipo": "dato", "porque": "x", "texto": "t", "seccion": "horario"}).aplicable
    assert not Arreglo.model_validate({"tipo": "respuesta", "porque": "x", "texto": "t"}).aplicable
    assert leer(a_json(_regla())) == _regla()
    assert leer(None) is None and leer({}) is None
    assert leer({"tipo": "regla", "porque": "x", "regla": {**REGLA, "kind": "inventada"}}) is None


def test_preparar_agrega_la_regla_sin_tocar_el_resto_ni_la_entrada() -> None:
    contenido = {"description": "d", "guardrails": [{"name": "Otra", "kind": "max_length", "action": "retry", "value": 300}]}

    nuevo, cambios = preparar_arreglo(contenido, _regla())

    assert [g["name"] for g in nuevo["guardrails"]] == ["Otra", "No prometer hoy"] and nuevo["description"] == "d"
    assert cambios == [{"ruta": "guardrails[1]", "antes": None, "despues": nuevo["guardrails"][1]}]
    assert len(contenido["guardrails"]) == 1, "la entrada no se muta"


def test_preparar_es_idempotente_y_no_duplica_la_misma_regla() -> None:
    nuevo, _ = preparar_arreglo({}, _regla())
    otra, cambios = preparar_arreglo(nuevo, _regla())
    assert cambios == [] and otra == nuevo
    # Aunque el dueño la haya renombrado, la misma regla no se agrega otra vez.
    renombrada = {"guardrails": [{**nuevo["guardrails"][0], "name": "Mi nombre"}]}
    assert preparar_arreglo(renombrada, _regla())[1] == []


def test_preparar_cambia_el_nombre_si_ya_hay_una_regla_distinta_con_ese_nombre() -> None:
    contenido = {"guardrails": [{"name": "No prometer hoy", "kind": "max_length", "action": "retry", "value": 5}]}
    nuevo, cambios = preparar_arreglo(contenido, _regla())
    assert [g["name"] for g in nuevo["guardrails"]] == ["No prometer hoy", "No prometer hoy (2)"] and len(cambios) == 1


@pytest.mark.parametrize("crudo", [
    {"tipo": "dato", "porque": "x", "texto": "t", "seccion": "catalogo"},
    {"tipo": "respuesta", "porque": "x", "texto": "t"},
])
def test_una_respuesta_o_un_dato_no_se_preparan(crudo: dict[str, object]) -> None:
    with pytest.raises(ArregloNoAplicable) as exc:
        preparar_arreglo({}, Arreglo.model_validate(crudo))
    assert exc.value.tipo == crudo["tipo"]
