"""SPEC-40 · piezas puras del resolver del toque (HU-OPC-03): argumentos, textos fijos, elegibilidad, códigos de fallo
y descarte de payloads que no valen. Sin base ni red; la ruta completa está en `tests/integration/test_opciones_toque*.py`."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from core.kernel.confirmacion import Dato, armar
from core.kernel.opciones import payload as p
from core.kernel.opciones import textos
from core.kernel.opciones.argumentos import (
    ArgumentosInvalidos,
    renderizar_respuesta,
    resolver,
    usa_resultado,
    validar,
)
from core.kernel.opciones.definicion import Accion
from core.kernel.opciones.lint import FichaTool, motivos_de_rechazo
from core.kernel.opciones.resolver import Bordes, Toque, _codigo_de_fallo, atender_toque
from core.kernel.tarjeta import RotulosInvalidos

PARAMETROS = {
    "type": "object",
    "properties": {"pedido": {"type": "string"}, "cantidad": {"type": "integer"}},
    "required": ["pedido"],
    "additionalProperties": False,
}


# ------------------------------------------------------------------ argumentos
def test_los_argumentos_son_los_fijos_mas_los_del_contexto() -> None:
    accion = Accion(tool="t", args_fijos={"cantidad": 2}, args_contexto={"pedido": "pedido.id"})
    assert resolver(accion, {"pedido.id": "A-1", "otro": "x"}) == {"cantidad": 2, "pedido": "A-1"}


def test_una_ruta_que_el_envio_no_resolvio_es_contexto_faltante() -> None:
    accion = Accion(tool="t", args_contexto={"pedido": "pedido.id"})
    with pytest.raises(ArgumentosInvalidos) as exc:
        resolver(accion, {})
    assert exc.value.codigo == "contexto_faltante" and exc.value.rutas == ["args_contexto.pedido"]


def test_un_nombre_en_las_dos_fuentes_no_se_resuelve() -> None:
    accion = Accion(tool="t", args_fijos={"pedido": "fijo"}, args_contexto={"pedido": "pedido.id"})
    with pytest.raises(ArgumentosInvalidos):
        resolver(accion, {"pedido.id": "A-1"})


def test_los_argumentos_no_comparten_estructura_con_la_definicion() -> None:
    accion = Accion(tool="t", args_fijos={"filtro": {"a": [1]}})
    args = resolver(accion, {})
    args["filtro"]["a"].append(2)
    assert accion.args_fijos == {"filtro": {"a": [1]}}


def test_validar_acepta_lo_que_el_schema_declara() -> None:
    validar({"pedido": "A-1", "cantidad": 3}, PARAMETROS)


@pytest.mark.parametrize(
    ("args", "ruta"),
    [
        ({}, "args.pedido"),  # falta un requerido
        ({"pedido": 5}, "args.pedido"),  # tipo
        ({"pedido": "A", "extra": 1}, "args.extra"),  # clave que el schema no declara
    ],
)
def test_validar_reporta_la_deriva_con_rutas_y_sin_valores(args: dict[str, Any], ruta: str) -> None:
    with pytest.raises(ArgumentosInvalidos) as exc:
        validar(args, PARAMETROS)
    assert exc.value.codigo == "schema" and ruta in exc.value.rutas
    assert "A-1" not in str(exc.value)


def test_una_clave_no_declarada_es_deriva_aunque_el_schema_no_cierre_los_adicionales() -> None:
    with pytest.raises(ArgumentosInvalidos):
        validar({"pedido": "A", "extra": 1}, {"type": "object", "properties": {"pedido": {"type": "string"}}})


# ------------------------------------------------------------------ respuesta fija
def test_la_respuesta_cita_contexto_y_resultado() -> None:
    texto = "{ctx.nombre}: tu pedido {result.estado} ({result.eta} días)."
    out = renderizar_respuesta(texto, {"nombre": "Ana"}, {"estado": "va en camino", "eta": 2}, ["estado", "eta"])
    assert out == "Ana: tu pedido va en camino (2 días)."


def test_solo_se_citan_las_claves_declaradas_del_resultado() -> None:
    assert renderizar_respuesta("{result.secreto}", {}, {"secreto": "x"}, ["estado"]) is None


@pytest.mark.parametrize("valor", [True, None, [1], {"a": 1}, ""])
def test_un_valor_no_citable_deja_sin_respuesta_en_vez_de_un_hueco(valor: Any) -> None:
    assert renderizar_respuesta("Estado: {result.k}.", {}, {"k": valor}, ["k"]) is None


def test_un_placeholder_de_otro_origen_no_se_interpreta() -> None:
    assert renderizar_respuesta("{env.SECRETO}", {"env.SECRETO": "x"}) is None


def test_los_valores_citados_se_sanean() -> None:
    out = renderizar_respuesta("Dice: {result.k}", {}, {"k": "uno\ndos\r\ntres"}, ["k"])
    assert out is not None and "\n" not in out and "\r" not in out


def test_una_respuesta_sin_placeholders_se_devuelve_tal_cual() -> None:
    assert renderizar_respuesta("Listo.", {}) == "Listo."


def test_usa_resultado_solo_si_cita_el_resultado() -> None:
    assert usa_resultado("Estado {result.k}") and not usa_resultado("Hola {ctx.nombre}")


# ------------------------------------------------------------------ textos fijos del sistema
def test_el_playbook_del_kernel_trae_todos_los_textos() -> None:
    t = textos.textos()
    assert set(t) == set(textos.CLAVES) and all(t[c].strip() for c in textos.CLAVES)
    assert len(t["boton_confirmar"]) <= 20 and len(t["boton_cancelar"]) <= 20


def _contenido(**cambios: str) -> str:
    valores = {c: ("Botón" if c.startswith("boton_") else f"texto {c}") for c in textos.CLAVES} | cambios
    return "\n".join(f"## {c}\n{v}\n" for c, v in valores.items())


def test_el_parseo_es_fail_closed() -> None:
    assert textos.parsear(_contenido())["cancelado"] == "texto cancelado"
    with pytest.raises(RotulosInvalidos):
        textos.parsear(_contenido().replace("## cancelado\ntexto cancelado\n", ""))
    with pytest.raises(RotulosInvalidos):
        textos.parsear(_contenido(cancelado="línea uno\nlínea dos"))
    with pytest.raises(RotulosInvalidos):
        textos.parsear(_contenido(boton_confirmar="x" * 21))


# ------------------------------------------------------------------ elegibilidad al ejecutar
def _ficha(**cambios: Any) -> FichaTool:
    base: dict[str, Any] = {"nombre": "t", "apta_para_opcion": True, "pack_activo": True, "binding_habilitado": True}
    return FichaTool(**(base | cambios))


def test_una_tool_elegible_no_tiene_motivos() -> None:
    assert motivos_de_rechazo("t", _ficha()) == []


@pytest.mark.parametrize(
    ("cambio", "codigo"),
    [
        ({"apta_para_opcion": False}, "tool_no_apta"),
        ({"pack_activo": False}, "pack_inactivo"),
        ({"binding_habilitado": None}, "tool_sin_binding"),  # sin fail-open
        ({"binding_habilitado": False}, "tool_binding_deshabilitado"),
        ({"gobernada_por_procedimiento": True}, "tool_en_procedimiento"),
    ],
)
def test_cada_motivo_de_rechazo(cambio: dict[str, Any], codigo: str) -> None:
    assert [c for c, _ in motivos_de_rechazo("t", _ficha(**cambio))] == [codigo]


# ------------------------------------------------------------------ clasificación del resultado de la tool
@pytest.mark.parametrize(
    ("resultado", "esperado"),
    [
        ({"estado": "ok"}, ""),
        ({"ok": True}, ""),
        ({"ok": False}, "tool_error"),
        ({"error": "sin_stock"}, "sin_stock"),
        ({"error": "No pude: datos de Ana 3001234567"}, "tool_error"),  # el texto del error no se propaga
        ({"error": {"x": 1}}, "tool_error"),
        ({"status": "confirmation_required"}, "confirmacion_requerida"),
        ("texto", "resultado_invalido"),
        (None, "resultado_invalido"),
    ],
)
def test_codigo_de_fallo(resultado: Any, esperado: str) -> None:
    assert _codigo_de_fallo(resultado) == esperado


# ------------------------------------------------------------------ descarte de payloads que no valen
class _Salida:
    def __init__(self) -> None:
        self.mensajes: list[Any] = []

    async def texto(self, cuerpo: str) -> bool:
        self.mensajes.append(cuerpo)
        return True

    async def confirmacion(self, interactivo: dict[str, Any]) -> bool:
        self.mensajes.append(interactivo)
        return True


def _entorno_prohibido() -> Any:
    raise AssertionError("un toque descartado no abre el entorno")


async def _tocar(payload: str, tenant: uuid.UUID, conv: uuid.UUID) -> tuple[str, _Salida]:
    salida = _Salida()
    bordes = Bordes(abrir_entorno=_entorno_prohibido, enviar_texto=salida.texto, enviar_confirmacion=salida.confirmacion, secreto="s1")
    return await atender_toque(Toque(tenant_id=tenant, conversation_id=conv, payload=payload), bordes), salida


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["", "opt:xyz", "opt:" + "a" * 32 + ":ver", "otro:" + "a" * 32 + ":ver:" + "b" * 16])
async def test_un_payload_con_otra_forma_se_descarta_sin_responder(payload: str) -> None:
    outcome, salida = await _tocar(payload, uuid.uuid4(), uuid.uuid4())
    assert outcome == "bad_signature" and salida.mensajes == []


@pytest.mark.asyncio
async def test_una_firma_de_otro_negocio_o_conversacion_se_descarta_sin_responder() -> None:
    t, c, s = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    bueno = p.construir("s1", "opt", t, c, s, "ver")
    for tenant, conv in ((uuid.uuid4(), c), (t, uuid.uuid4())):
        outcome, salida = await _tocar(bueno, tenant, conv)
        assert outcome == "bad_signature" and salida.mensajes == []
    forjado = p.construir("otro-secreto", "opt", t, c, s, "ver")
    assert (await _tocar(forjado, t, c))[0] == "bad_signature"


# ------------------------------------------------------------------ la confirmación de una opción
def test_la_confirmacion_usa_las_instrucciones_de_las_opciones() -> None:
    conf = armar(
        "Consultar pedido", [Dato("pedido", "A-1")], "optc:x", completo=True,
        instrucciones=("Toca «Confirmar» o «Cancelar».", "Responde Confirmar o Cancelar."),
    )
    assert "Toca «Confirmar» o «Cancelar»." in conf.cuerpo
    assert "Responde Confirmar o Cancelar." in conf.texto and "Toca «Confirmar»" not in conf.texto


def test_la_metrica_del_toque_lleva_la_via(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`option_taps_total` separa el botón (`button`) de la respuesta a un menú numerado (`text`)."""
    from core.kernel.opciones import metricas

    class _Instrumento:
        def __init__(self) -> None:
            self.puntos: list[dict] = []

        def add(self, _n, etiquetas) -> None:  # type: ignore[no-untyped-def]
            self.puntos.append(etiquetas)

        def record(self, _v, etiquetas) -> None:  # type: ignore[no-untyped-def]
            self.puntos.append(etiquetas)

    toques, latencia = _Instrumento(), _Instrumento()
    monkeypatch.setattr(metricas, "_toques", toques)
    monkeypatch.setattr(metricas, "_latencia", latencia)
    metricas.toque("ok", 0.1)
    metricas.toque("ok", 0.1, "text")
    assert toques.puntos == [{"outcome": "ok", "via": "button"}, {"outcome": "ok", "via": "text"}]
    assert latencia.puntos == toques.puntos
