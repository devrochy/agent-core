"""Centro de pruebas (SPEC-30): parser del catálogo, reglas de estado e informe (sin BD)."""

from __future__ import annotations

import pytest

from core.qa.catalogo import CatalogoInvalido, parse_catalogo
from core.qa.estados import consolidar, estado_de_prueba, progreso
from core.qa.informe import armar_informe, cuerpo_correo, informe_markdown

CATALOGO_V2 = """
version: 2
fecha: '2026-10-13'
items:
- id: pruebas-paleta
  seccion: pruebas
  modulo: Panel
  grupo: Navegación
  titulo: Ir a una sección con Ctrl+K
  prioridad: P0
  plataforma: panel
  precondiciones: [Sesión iniciada]
  pasos:
  - accion: Pulsar Ctrl+K.
    esperado: Se abre la paleta.
  - accion: Escribir «prom» y Enter.
    esperado: Llega a Promesas.
  hu: [HU-DS-05]
- id: pruebas-v1
  seccion: pruebas
  grupo: Viejo
  titulo: Prueba en formato v1
  pasos: [Abrir, Mirar]
  esperado: Todo bien
- id: test-smtp
  seccion: test
  grupo: Correo
  titulo: SMTP de test
  porque: Sin SMTP no llegan correos.
  como: Cargar las variables.
  vars: [SYSTEM_SMTP_HOST]
"""


def test_parsea_v1_y_v2():
    cat = parse_catalogo(CATALOGO_V2)
    assert cat.version == 2 and len(cat.items) == 3
    paleta, v1, smtp = cat.items
    assert paleta.plataforma == ["panel"] and paleta.prioridad == "P0"
    assert paleta.pasos[1] == {"accion": "Escribir «prom» y Enter.", "esperado": "Llega a Promesas."}
    assert v1.pasos == [{"accion": "Abrir", "esperado": None}, {"accion": "Mirar", "esperado": None}]
    assert v1.esperado == "Todo bien"
    assert smtp.vars == ["SYSTEM_SMTP_HOST"] and smtp.pasos == []
    assert len(cat.sha256) == 64


def test_mismo_contenido_mismo_hash():
    assert parse_catalogo(CATALOGO_V2).sha256 == parse_catalogo(CATALOGO_V2.encode()).sha256


@pytest.mark.parametrize(
    ("yaml_txt", "fragmento"),
    [
        ("version: 1\nfecha: x\nitems: []", "falta la lista `items`"),
        ("fecha: x\nitems: [{id: a, seccion: test, grupo: g, titulo: t, porque: p, como: c}]", "version"),
        ("version: 1\nfecha: x\nitems: [{id: A_B, seccion: test, grupo: g, titulo: t, porque: p, como: c}]", "id ausente"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: otra, grupo: g, titulo: t}]", "seccion"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: pruebas, grupo: g, titulo: t, pasos: [{accion: x}]}]", "sin `esperado`"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: pruebas, grupo: g, titulo: t, pasos: [{accion: x, esperado: y, foto: z}]}]", "desconocidos"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: prod, grupo: g, titulo: t, porque: p}]", "falta como"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: test, grupo: g, titulo: t, porque: p, como: c}, {id: a, seccion: test, grupo: g, titulo: t, porque: p, como: c}]", "duplicado"),
        ("version: 1\nfecha: x\nitems: [{id: a, seccion: pruebas, grupo: g, titulo: t, esperado: e, pasos: [x], prioridad: P9}]", "prioridad"),
        ("[: no es yaml", "YAML inválido"),
    ],
)
def test_rechaza_catalogos_invalidos(yaml_txt, fragmento):
    with pytest.raises(CatalogoInvalido) as exc:
        parse_catalogo(yaml_txt)
    assert any(fragmento in e for e in exc.value.errores), exc.value.errores


@pytest.mark.parametrize(
    ("pasos", "esperado"),
    [
        ({}, "sin_correr"),
        ({1: "ok"}, "en_curso"),
        ({1: "ok", 2: "ok", 3: "ok"}, "ok"),
        ({1: "ok", 2: "falla"}, "falla"),
        ({1: "bloqueado"}, "bloqueada"),
        ({1: "bloqueado", 2: "falla"}, "falla"),
        ({1: "omitido", 2: "omitido", 3: "omitido"}, "omitida"),
        ({1: "omitido", 2: "ok", 3: "omitido"}, "ok"),
        ({1: None, 2: None}, "sin_correr"),
        ({7: "ok"}, "sin_correr"),
    ],
)
def test_estado_de_prueba(pasos, esperado):
    assert estado_de_prueba(3, pasos) == esperado


def test_consolidar_toma_el_peor():
    assert consolidar([]) == "sin_correr"
    assert consolidar(["ok", "falla"]) == "falla"
    assert consolidar(["ok", "en_curso"]) == "ok"
    assert consolidar(["omitida", "sin_correr"]) == "omitida"
    assert consolidar(["ok", "bloqueada"]) == "bloqueada"


def test_progreso_cuenta_las_que_faltan_como_sin_correr():
    p = progreso({"a": "ok", "b": "falla", "c": "en_curso"}, 5)
    assert p["total"] == 5 and p["terminadas"] == 2
    assert p["sin_correr"] == 2 and p["en_curso"] == 1 and p["ok"] == 1 and p["falla"] == 1


def _informe():
    items = [
        {"slug": "a", "titulo": "Prueba A", "grupo": "G", "pasos": [{"accion": "Hacer", "esperado": "Pasa"}], "hu": ["HU-X-1"]},
        {"slug": "b", "titulo": "Prueba B", "grupo": "G", "pasos": [{"accion": "Otra", "esperado": None}]},
        {"slug": "c", "titulo": "Prueba C", "grupo": "G", "pasos": [{"accion": "Nada", "esperado": None}]},
    ]
    resultados = {
        "a": {"estado": "ok", "nota": None, "pasos": {1: {"estado": "ok", "nota": None}}, "adjuntos": {}},
        "b": {
            "estado": "falla",
            "nota": "Se cae",
            "pasos": {1: {"estado": "falla", "nota": "Error 500"}},
            "adjuntos": {1: [{"id": "abc", "nombre": "pantalla.png", "mimetype": "image/png"}]},
        },
    }
    return armar_informe(
        ciclo={"id": "c1", "nombre": "Ciclo 1"},
        catalogo={"version": 2, "fecha": "2026-10-13"},
        tester={"id": "t1", "nombre": "ana", "email": "ana@x.com"},
        iniciada_en="2026-10-13T10:00:00",
        enviada_en="2026-10-13T11:00:00",
        comentario="Todo listo",
        progreso=progreso({"a": "ok", "b": "falla"}, 3),
        items=items,
        resultados=resultados,
    )


def test_informe_ordena_fallas_primero_y_separa_sin_correr():
    inf = _informe()
    assert [p["slug"] for p in inf["pruebas"]] == ["b", "a"]
    assert inf["sin_correr"] == [{"slug": "c", "titulo": "Prueba C", "grupo": "G"}]
    assert inf["pruebas"][0]["pasos"][0]["adjuntos"][0]["id"] == "abc"


def test_informe_markdown_y_correo():
    inf = _informe()
    md = informe_markdown(inf, enlace_adjunto="/api/qa/adjuntos/")
    assert "# Informe de pruebas · Ciclo 1" in md
    assert "### Falla · Prueba B" in md
    assert "[pantalla.png](/api/qa/adjuntos/abc)" in md
    assert "## Sin correr (1)" in md
    correo = cuerpo_correo(inf, "https://panel/pruebas/informes/r1")
    assert "Falla: Prueba B" in correo and "https://panel/pruebas/informes/r1" in correo
