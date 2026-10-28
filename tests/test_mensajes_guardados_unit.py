"""Pista D4 · reglas puras de los mensajes guardados (ADR-0178): texto, variables, canales, estado y vista previa.

Sin base ni red. El comportamiento con Postgres, RLS y permisos está en `tests/integration/test_mensajes_guardados_api.py`.
"""

from __future__ import annotations

import pytest

from core.admin import mensajes_guardados_service as svc
from core.admin.catalogo_mensajes_service import canales_de, canales_de_guardado
from core.gateway.connection_types import connection_types
from core.kernel.opciones.contexto import VARIABLES_BASE


def _codigo(exc: pytest.ExceptionInfo[svc.ErrorMensajeGuardado]) -> tuple[int, str]:
    return exc.value.status, exc.value.code


# ---------------------------------------------------------------- texto
def test_el_texto_conserva_los_saltos_de_linea_y_normaliza() -> None:
    texto, variables = svc.normalizar_texto("  Hola {cliente.nombre},\r\ngracias por escribir a {negocio.nombre}.\r\n\r\nUn saludo  ")
    assert texto == "Hola {cliente.nombre},\ngracias por escribir a {negocio.nombre}.\n\nUn saludo"
    assert variables == ["cliente.nombre", "negocio.nombre"]


def test_quita_caracteres_de_control_pero_no_el_salto_ni_la_tabulacion() -> None:
    texto, _ = svc.normalizar_texto("a\x00b\x07c\x1bd\te\nf\x7f")
    assert texto == "abcd\te\nf"


def test_las_variables_no_se_repiten_y_siguen_el_orden_de_aparicion() -> None:
    _, variables = svc.normalizar_texto("{negocio.nombre} {cliente.nombre} {negocio.nombre}")
    assert variables == ["negocio.nombre", "cliente.nombre"]


def test_texto_sin_variables_es_valido_y_llaves_vacias_son_literales() -> None:
    texto, variables = svc.normalizar_texto("Respuesta con {} literal")
    assert variables == [] and texto == "Respuesta con {} literal"


@pytest.mark.parametrize("texto", ["", "   ", "\n\t "])
def test_texto_vacio_se_rechaza(texto: str) -> None:
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_texto(texto)
    assert _codigo(exc) == (422, "texto_invalido")


def test_el_tope_de_largo_es_exacto() -> None:
    assert svc.normalizar_texto("a" * svc.MAX_TEXTO)[0] == "a" * svc.MAX_TEXTO
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_texto("a" * (svc.MAX_TEXTO + 1))
    assert _codigo(exc) == (422, "texto_invalido")


def test_una_variable_fuera_de_la_lista_se_rechaza_y_se_nombra() -> None:
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_texto("Hola {cliente.nombre}, tu cita {cita.fecha} y {x}")
    assert _codigo(exc) == (422, "variable_no_permitida")
    assert exc.value.details == {"desconocidas": ["cita.fecha", "x"], "permitidas": list(VARIABLES_BASE)}


def test_el_dialecto_de_meta_no_vale_aqui() -> None:
    for texto in ("Hola {{1}}", "Hola {{cliente_nombre}}"):
        with pytest.raises(svc.ErrorMensajeGuardado) as exc:
            svc.normalizar_texto(texto)
        assert _codigo(exc) == (422, "variable_no_permitida"), texto


def test_los_nombres_desconocidos_se_acotan_en_el_error() -> None:
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_texto("{" + "x" * 500 + "}")
    assert all(len(n) <= 40 for n in exc.value.details["desconocidas"])  # type: ignore[index]


# ---------------------------------------------------------------- título, estado, canales
def test_titulo_se_compacta_y_se_acota() -> None:
    assert svc.normalizar_titulo("  Horario   de\n atención ") == "Horario de atención"
    for malo in ("", "  ", "x" * (svc.MAX_TITULO + 1)):
        with pytest.raises(svc.ErrorMensajeGuardado) as exc:
            svc.normalizar_titulo(malo)
        assert _codigo(exc) == (422, "titulo_invalido")


def test_estado_solo_acepta_los_tres() -> None:
    assert [svc.normalizar_estado(e) for e in ("borrador", "activo", "archivado")] == ["borrador", "activo", "archivado"]
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_estado("publicado")
    assert _codigo(exc) == (422, "estado_invalido")


def test_canales_vacio_o_todos_significa_todos() -> None:
    assert svc.normalizar_canales(None) == []
    assert svc.normalizar_canales([]) == []
    assert svc.normalizar_canales(["todos"]) == []
    assert svc.normalizar_canales(["webchat", "todos"]) == []


def test_canales_se_deduplican_y_siguen_el_orden_del_catalogo() -> None:
    orden = svc.canales_permitidos()
    pedidos = [orden[-1], orden[0], orden[-1], f" {orden[0]} "]
    assert svc.normalizar_canales(pedidos) == [orden[0], orden[-1]]


def test_canales_desconocidos_o_sin_conversaciones_se_rechazan() -> None:
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.normalizar_canales(["whatsapp_meta", "telegram", "playground"])
    assert _codigo(exc) == (422, "canales_invalidos")
    assert exc.value.details["invalidos"] == ["playground", "telegram"]  # type: ignore[index]


def test_los_canales_permitidos_excluyen_el_playground_y_todo_tipo_sin_conversaciones() -> None:
    permitidos = set(svc.canales_permitidos())
    assert "playground" not in permitidos and "whatsapp_meta" in permitidos and "webchat" in permitidos
    assert permitidos == {t.id for t in connection_types() if t.produces_conversations and t.channel}


# ---------------------------------------------------------------- vista previa
def test_vista_previa_usa_muestras_y_los_ejemplos_pedidos() -> None:
    r = svc.vista_previa("Hola {cliente.nombre}, soy de {negocio.nombre}.\nSaludos")
    assert r["texto_renderizado"] == "Hola Ana, soy de Mi negocio.\nSaludos"
    assert r["variables"] == ["cliente.nombre", "negocio.nombre"]
    r = svc.vista_previa("Hola {cliente.nombre}", {"cliente.nombre": "Luis", "otra.cosa": "ignorada"})
    assert r["texto_renderizado"] == "Hola Luis" and r["ejemplos_usados"] == {"cliente.nombre": "Luis"}


def test_vista_previa_trata_el_valor_como_dato() -> None:
    r = svc.vista_previa("Hola {cliente.nombre}", {"cliente.nombre": "{negocio.nombre}\n\n  x"})
    assert r["texto_renderizado"] == "Hola {negocio.nombre} x", "un valor con llaves o saltos no se reinterpreta ni rompe el mensaje"


def test_vista_previa_rechaza_lo_mismo_que_guardar() -> None:
    with pytest.raises(svc.ErrorMensajeGuardado) as exc:
        svc.vista_previa("Hola {nadie.sabe}")
    assert _codigo(exc) == (422, "variable_no_permitida")


# ---------------------------------------------------------------- canales del catálogo
def test_canales_del_catalogo_vacio_son_todos_los_de_clientes_sin_repetir() -> None:
    todos = canales_de_guardado([])
    assert len(todos) == len(set(todos)) and "whatsapp" in todos and "web" in todos
    assert canales_de("guardado") == todos


def test_canales_del_catalogo_siguen_los_tipos_elegidos() -> None:
    assert canales_de_guardado(["whatsapp_meta", "whatsapp_waweb"]) == ["whatsapp"]
    assert canales_de_guardado(["webchat"]) == ["web"]
    assert canales_de_guardado(["playground"]) == [], "un tipo que no atiende clientes no suma canales"
