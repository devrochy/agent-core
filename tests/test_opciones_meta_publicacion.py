"""SPEC-40 · lo puro de publicar una versión en Meta (HU-OPC-04, ADR-0172 §2): nombre `<clave>_v<n>`, categoría,
componentes y la prueba de que lo que se crea lo puede enviar el PR 5 (`compatibilidad_plantilla`).

Sin base ni red: la parte con base, Meta simulado y candado está en
`tests/integration/test_opciones_meta_publicacion_db.py`."""

from __future__ import annotations

import pytest

from core.gateway.meta_templates import validate_template_components
from core.kernel.opciones import envio, mapeo
from core.kernel.opciones import meta as m
from core.kernel.opciones.definicion import Definicion, leer


def definicion(n: int = 3, *, body: str = "Hola {cliente.nombre}, ¿qué hacemos?", footer: str | None = None) -> Definicion:
    return leer(
        {
            "body": body,
            "footer": footer,
            "variables": ["cliente.nombre"],
            "exclusivo": True,
            "ttl_horas": 24,
            "opciones": [
                {
                    "id": f"o{i}",
                    "titulo": f"Opción {i}",
                    "accion": {"tool": "x_tool", "args_fijos": {}, "args_contexto": {}, "confirmar": False},
                    "respuesta_ok": "Listo.",
                    "respuesta_error": "No pude.",
                    "result_keys": [],
                }
                for i in range(1, n + 1)
            ],
        }
    )


# ------------------------------------------------------------------ nombre, categoría, idioma
def test_el_nombre_es_clave_v_n_y_cada_version_es_otro_nombre() -> None:
    assert m.nombre_en_meta("recordatorio", 1) == "recordatorio_v1"
    assert m.nombre_en_meta("recordatorio", 2) == "recordatorio_v2"


@pytest.mark.parametrize(("clave", "n"), [("", 1), ("Con Mayus", 1), ("a" * 41, 1), ("ok", 0), ("con-guion", 1)])
def test_una_clave_o_version_invalida_no_arma_un_nombre(clave: str, n: int) -> None:
    with pytest.raises(ValueError):
        m.nombre_en_meta(clave, n)


def test_el_nombre_mas_largo_cabe_en_lo_que_meta_acepta() -> None:
    assert len(m.nombre_en_meta("a" * 40, 999999)) <= 512


@pytest.mark.parametrize("entrada", ["utility", "UTILITY", " Marketing "])
def test_solo_utilidad_y_marketing(entrada: str) -> None:
    assert m.categoria_valida(entrada) in ("UTILITY", "MARKETING")


@pytest.mark.parametrize("entrada", ["AUTHENTICATION", "", "otra", "util"])
def test_autenticacion_y_lo_desconocido_se_rechazan(entrada: str) -> None:
    assert m.categoria_valida(entrada) is None


@pytest.mark.parametrize(("idioma", "ok"), [("es", True), ("es_AR", True), ("en_US", True), ("ES", False), ("es-AR", False), ("", False)])
def test_idioma(idioma: str, ok: bool) -> None:
    assert m.idioma_valido(idioma) is ok


# ------------------------------------------------------------------ componentes: lo que se crea lo puede enviar el PR 5
@pytest.mark.parametrize("n", [1, 3, 4, 10])
@pytest.mark.parametrize("footer", [None, "Responde con un toque"])
@pytest.mark.parametrize("body", ["Hola {cliente.nombre}, ¿qué hacemos?", "Sin variables"])
def test_la_plantilla_creada_pasa_la_compatibilidad_del_envio_y_la_validacion_de_meta(n: int, footer: str | None, body: str) -> None:
    d = definicion(n, body=body, footer=footer)
    componentes = m.componentes_para_publicar(d)
    assert envio.compatibilidad_plantilla(componentes, d) is None
    validate_template_components("UTILITY", componentes, m.FORMATO_PARAMETROS)
    validate_template_components("MARKETING", componentes, m.FORMATO_PARAMETROS)
    botones = next(c for c in componentes if c["type"] == "BUTTONS")["buttons"]
    assert [b["type"] for b in botones] == ["QUICK_REPLY"] * n
    assert [b["text"] for b in botones] == [o.titulo for o in d.opciones]
    assert [c["type"] for c in componentes] == ["BODY", *(["FOOTER"] if footer else []), "BUTTONS"]


def test_los_ejemplos_del_dueno_viajan_en_los_parametros_nombrados() -> None:
    componentes = m.componentes_para_publicar(definicion(), {"cliente.nombre": "Mariana"})
    ejemplo = componentes[0]["example"]["body_text_named_params"]
    assert ejemplo == [{"param_name": "cliente_nombre", "example": "Mariana"}]
    assert "{{cliente_nombre}}" in componentes[0]["text"]


def test_dos_versiones_con_distinto_cuerpo_dan_componentes_distintos_pero_ambos_compatibles() -> None:
    v1, v2 = definicion(2), definicion(3, body="Otro texto {cliente.nombre}")
    c1, c2 = m.componentes_para_publicar(v1), m.componentes_para_publicar(v2)
    assert c1 != c2
    assert envio.compatibilidad_plantilla(c1, v1) is None and envio.compatibilidad_plantilla(c2, v2) is None
    # La plantilla de la v1 NO sirve para enviar la v2 (otros botones): el envío no la usaría.
    assert envio.compatibilidad_plantilla(c1, v2) == envio.PLANTILLA_DISTINTA


# ------------------------------------------------------------------ adoptar una plantilla que no creó esta versión
def test_mismos_textos_acepta_exactamente_lo_que_esta_version_diria() -> None:
    d = definicion(3, footer="Pie")
    assert m.mismos_textos(mapeo.componentes_meta(d), d)


def test_mismos_textos_rechaza_un_cuerpo_distinto_aunque_botones_y_variables_coincidan() -> None:
    d = definicion(3)
    ajeno = mapeo.componentes_meta(definicion(3, body="Texto viejo de otra clave {cliente.nombre}"))
    assert envio.compatibilidad_plantilla(ajeno, d) is None  # lo que mira el envío no lo distingue…
    assert not m.mismos_textos(ajeno, d)  # …por eso adoptar compara también los textos


def test_mismos_textos_rechaza_otro_pie_u_otros_botones() -> None:
    d = definicion(3, footer="Pie")
    assert not m.mismos_textos(mapeo.componentes_meta(definicion(3, footer="Otro pie")), d)
    assert not m.mismos_textos(mapeo.componentes_meta(definicion(2, footer="Pie")), d)
    assert not m.mismos_textos(None, d)


@pytest.mark.parametrize(
    "mala",
    [
        definicion(0),
        definicion(11),
        definicion(2, body="x" * 1025),
        definicion(2, body="   "),
        definicion(2, footer="p" * 61),
    ],
    ids=["sin opciones", "11 opciones", "cuerpo largo", "cuerpo vacío", "pie largo"],
)
def test_lo_que_meta_rechazaria_no_se_arma(mala: Definicion) -> None:
    with pytest.raises(ValueError):
        m.componentes_para_publicar(mala)


def test_un_titulo_de_opcion_demasiado_largo_no_se_arma() -> None:
    d = definicion(2)
    largo = d.model_copy(update={"opciones": [d.opciones[0].model_copy(update={"titulo": "t" * 21}), d.opciones[1]]})
    with pytest.raises(ValueError):
        m.componentes_para_publicar(largo)


def test_una_definicion_cuya_plantilla_no_coincidiria_no_se_manda_a_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envio, "compatibilidad_plantilla", lambda *_a, **_k: envio.PLANTILLA_DISTINTA)
    with pytest.raises(ValueError):
        m.componentes_para_publicar(definicion())


# ------------------------------------------------------------------ resultado
def test_solo_los_fallos_que_se_pueden_repetir_son_reintentables() -> None:
    for outcome in (m.ERROR_DE_RED, m.LIMITE_DE_CREACION, m.EN_CURSO, m.ERROR_INTERNO):
        assert m.ResultadoPublicacion(False, outcome).reintentable
    for outcome in (m.RECHAZADA_POR_META, m.DEFINICION_INVALIDA, m.CATEGORIA_INVALIDA, m.NOMBRE_OCUPADO, m.OTRA_CUENTA, m.PLANTILLA_BORRADA):
        assert not m.ResultadoPublicacion(False, outcome).reintentable
    assert not m.ResultadoPublicacion(True, m.PUBLICADA).reintentable
