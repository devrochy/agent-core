"""Centro de pruebas (SPEC-30): parser del catálogo, reglas de estado e informe (sin BD)."""

from __future__ import annotations

import pytest

from core.qa.catalogo import CatalogoInvalido, parse_catalogo
from core.qa.estados import consolidar, estado_de_prueba, progreso
from core.qa.informe import agrupar_por_seccion, armar_informe, cuerpo_correo, informe_markdown

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


CATALOGO_V6 = """
version: 6
fecha: '2026-10-13'
items:
- id: pruebas-cartera
  seccion: pruebas
  modulo: Cobranza
  entorno: apk
  area: Cartera del gestor
  grupo: Visitas
  titulo: Registrar un pago en la ruta
  requiere: [datos-cuentas-propias]
  pasos:
  - accion: Abrir la ruta.
    esperado: Aparece Juana Pérez en mora.
- id: datos-cuentas-propias
  seccion: datos
  grupo: Cuentas
  titulo: Tus cuentas de prueba
  porque: Cada tester prueba con su propio negocio.
  como: En Pruebas, pestaña Datos de prueba, botón Preparar mis datos.
"""


def test_v6_entorno_area_requiere_y_guia_de_datos():
    cat = parse_catalogo(CATALOGO_V6)
    prueba, guia = cat.items
    assert prueba.entorno == "apk" and prueba.area == "Cartera del gestor"
    assert prueba.requiere == ["datos-cuentas-propias"]
    assert guia.seccion == "datos" and guia.pasos == []


def test_v6_rechaza_entorno_desconocido_y_requiere_inexistente():
    malo = CATALOGO_V6.replace("entorno: apk", "entorno: tablet").replace(
        "requiere: [datos-cuentas-propias]", "requiere: [datos-que-no-esta]"
    )
    with pytest.raises(CatalogoInvalido) as exc:
        parse_catalogo(malo)
    texto = " ".join(exc.value.errores)
    assert "entorno 'tablet'" in texto and "datos-que-no-esta" in texto


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
    md = informe_markdown(inf, enlace_adjunto="/api/qa/attachments/")
    assert "# Informe de pruebas · Ciclo 1" in md
    assert "### Falla · Prueba B" in md
    assert "[pantalla.png](/api/qa/attachments/abc)" in md
    assert "## Sin correr (1)" in md
    correo = cuerpo_correo(inf, "https://panel/pruebas/informes/r1")
    assert "Falla: Prueba B" in correo and "https://panel/pruebas/informes/r1" in correo


def test_captura_con_contenido_que_no_coincide_se_rechaza(tmp_path, monkeypatch):
    import uuid

    from core.config import get_settings
    from core.qa import evidencias

    monkeypatch.setattr(get_settings(), "tenants_root", str(tmp_path))
    run = uuid.uuid4()
    with pytest.raises(evidencias.EvidenciaInvalida):
        evidencias.guardar(run, "image/png", b"<html>no es png</html>")
    ruta, tipo, tamano = evidencias.guardar(run, "image/png; charset=binary", b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    assert tipo == "image/png" and tamano == 18 and evidencias.resolver(ruta) is not None
    assert evidencias.resolver("../../etc/passwd") is None


CATALOGO_V15 = """
version: 15
fecha: '2026-10-21'
items:
- id: pruebas-cajon
  seccion: pruebas
  modulo: App móvil
  entorno: apk
  area: Sesión y permisos
  subseccion: Cajón y tema
  titulo: El cajón muestra solo lo permitido
  contexto: El menú de la app es el del panel.
  precondiciones: [Tu dueño de prueba]
  pasos:
  - accion: Tocar la hamburguesa.
    esperado: Se abre el cajón.
  hu: [HU-DS-11]
- id: test-smtp-v15
  seccion: test
  grupo: Correo
  titulo: SMTP de test
  porque: Sin SMTP no llegan correos.
  como: Cargar las variables.
"""


def test_v15_subseccion_y_grupo_solo_para_las_demas_secciones():
    cat = parse_catalogo(CATALOGO_V15)
    prueba, smtp = cat.items
    assert prueba.subseccion == "Cajón y tema" and prueba.grupo == ""
    assert smtp.grupo == "Correo" and smtp.subseccion is None


def test_v15_otras_secciones_siguen_exigiendo_grupo():
    malo = CATALOGO_V15.replace("  grupo: Correo\n", "")
    with pytest.raises(CatalogoInvalido) as exc:
        parse_catalogo(malo)
    assert any("falta grupo" in e for e in exc.value.errores)


def test_agrupar_por_seccion_cuenta_por_nivel():
    items = [
        {"slug": "a", "entorno": "panel", "area": "Conexiones", "subseccion": "Números y plantillas"},
        {"slug": "b", "entorno": "panel", "area": "Conexiones", "subseccion": "Números y plantillas"},
        {"slug": "c", "entorno": "panel", "area": "Conexiones", "subseccion": "WhatsApp Web"},
        {"slug": "d", "entorno": "apk", "area": "Sesión y permisos", "subseccion": "Cajón y tema"},
    ]
    resultados = {"a": {"estado": "ok"}, "b": {"estado": "falla"}, "c": {"estado": "sin_correr"}}
    secciones = agrupar_por_seccion(items, resultados)
    assert len(secciones) == 2
    panel, apk = secciones[0], secciones[1]
    assert panel["entorno"] == "panel" and panel["total"] == 3 and panel["ok"] == 1 and panel["falla"] == 1
    area = panel["areas"][0]
    assert area["area"] == "Conexiones" and len(area["subsecciones"]) == 2
    subs = {s["subseccion"]: s for s in area["subsecciones"]}
    assert subs["Números y plantillas"]["total"] == 2 and subs["Números y plantillas"]["falla"] == 1
    assert apk["total"] == 1 and apk["sin_correr"] == 1


def test_informe_formato_2_agrupa_por_seccion():
    items = [
        {"slug": "a", "titulo": "A", "grupo": "", "entorno": "panel", "area": "Conexiones",
         "subseccion": "Números y plantillas", "pasos": [{"accion": "Hacer", "esperado": "Pasa"}]},
        {"slug": "b", "titulo": "B", "grupo": "", "entorno": "panel", "area": "Conexiones",
         "subseccion": "WhatsApp Web", "pasos": [{"accion": "Otra", "esperado": None}]},
    ]
    resultados = {"a": {"estado": "ok", "pasos": {1: {"estado": "ok"}}, "adjuntos": {}}}
    inf = armar_informe(
        ciclo={"id": "c1", "nombre": "Ciclo 1"},
        catalogo={"version": 7, "fecha": "2026-10-21"},
        tester={"id": "t1", "nombre": "ana", "email": "ana@x.com"},
        iniciada_en="2026-10-21T10:00:00",
        enviada_en="2026-10-21T11:00:00",
        comentario=None,
        progreso=progreso({"a": "ok"}, 2),
        items=items,
        resultados=resultados,
    )
    assert inf["formato"] == 2
    assert inf["secciones"][0]["entorno"] == "panel"
    assert inf["secciones"][0]["areas"][0]["subsecciones"][0]["subseccion"] == "Números y plantillas"
    md = informe_markdown(inf)
    assert "## Resultados por sección" in md and "Conexiones" in md
    assert "Números y plantillas: OK 1" in md
