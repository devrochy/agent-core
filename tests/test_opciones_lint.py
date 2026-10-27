"""SPEC-40 · lint de la definición de una plantilla con opciones (unitarios, sin BD)."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from core.kernel.opciones import lint as L
from core.kernel.opciones.definicion import Accion

VARIABLES = {"cliente.nombre", "pedido.numero", "pedido.id"}
SCHEMA = {
    "type": "object",
    "properties": {
        "pedido_id": {"type": "string"},
        "detalle": {"type": "boolean"},
        "limite": {"type": "integer", "minimum": 1, "maximum": 10},
        "modo": {"type": "string", "enum": ["resumen", "completo"]},
    },
    "required": ["pedido_id"],
    "additionalProperties": False,
}


def ficha(nombre: str = "consultar_estado_pedido", **kw: Any) -> L.FichaTool:
    base = dict(nombre=nombre, parameters=SCHEMA, apta_para_opcion=True, pack_activo=True, binding_habilitado=True)
    return L.FichaTool(**{**base, **kw})


def contexto(**fichas: L.FichaTool) -> L.ContextoLint:
    return L.ContextoLint(herramientas=fichas or {"consultar_estado_pedido": ficha()}, variables=VARIABLES)


def valida() -> dict[str, Any]:
    return {
        "body": "Hola {cliente.nombre}, tu pedido {pedido.numero} está listo.",
        "footer": "Gracias por elegirnos",
        "variables": ["cliente.nombre", "pedido.numero"],
        "exclusivo": True,
        "ttl_horas": 24,
        "opciones": [
            {
                "id": "estado",
                "titulo": "Ver estado",
                "accion": {
                    "tool": "consultar_estado_pedido",
                    "args_fijos": {"detalle": True},
                    "args_contexto": {"pedido_id": "pedido.id"},
                    "confirmar": False,
                },
                "respuesta_ok": "Tu pedido {ctx.pedido.numero} está {result.estado}.",
                "respuesta_error": "No pude consultarlo ahora. Un asesor te ayuda en breve.",
                "result_keys": ["estado"],
            }
        ],
    }


def codigos(raw: Any, ctx: L.ContextoLint | None = None) -> set[str]:
    return {h.code for h in L.lintar(raw, ctx or contexto())}


def errores(raw: Any, ctx: L.ContextoLint | None = None) -> list[L.Hallazgo]:
    return [h for h in L.lintar(raw, ctx or contexto()) if h.severity == "error"]


def con(**cambios: Any) -> dict[str, Any]:
    raw = copy.deepcopy(valida())
    raw.update(cambios)
    return raw


def test_una_definicion_valida_no_tiene_errores() -> None:
    assert errores(valida()) == []
    assert not L.hay_errores(L.lintar(valida(), contexto()))


def test_hallazgo_tiene_la_forma_path_code_severity_mensaje() -> None:
    h = L.lintar(con(opciones=[]), contexto())[0]
    assert set(h.as_dict()) == {"path", "code", "severity", "mensaje"}


def test_limites_un_hallazgo_por_cada_falla() -> None:
    raw = valida()
    opcion = raw["opciones"][0]
    raw["opciones"] = [{**copy.deepcopy(opcion), "id": "a", "titulo": "x" * 21}] + [
        {**copy.deepcopy(opcion), "id": "a" if i < 2 else f"o{i}", "titulo": f"Opción {i}"} for i in range(10)
    ]
    cs = [(h.code, h.path) for h in errores(raw)]
    assert ("demasiadas_opciones", "opciones") in cs
    assert ("titulo_largo", "opciones[0].titulo") in cs
    assert [c for c, _ in cs].count("id_repetido") >= 2


def test_cuerpo_pie_ttl_y_vacios() -> None:
    assert "cuerpo_vacio" in codigos(con(body=" ", variables=[]))
    assert "cuerpo_largo" in codigos(con(body="x" * 1025, variables=[]))
    assert "pie_largo" in codigos(con(footer="x" * 61))
    assert "ttl_invalido" in codigos(con(ttl_horas=721)) and "ttl_invalido" in codigos(con(ttl_horas=0))
    assert "sin_opciones" in codigos(con(opciones=[]))


def test_forma_invalida_devuelve_hallazgos_de_esquema_con_path() -> None:
    hs = L.lintar({"body": "x", "opciones": [{"id": "a"}], "extra": 1}, contexto())
    assert {h.code for h in hs} == {"esquema"}
    assert {h.path for h in hs} >= {"extra", "opciones[0].titulo", "opciones[0].accion"}
    assert [h.code for h in L.lintar("no es un objeto", contexto())] == ["esquema"]


def test_variables_catalogo_declaracion_y_colision() -> None:
    assert "variable_fuera_de_catalogo" in codigos(con(variables=["cliente.nombre", "pedido.numero", "otro.dato"]))
    assert "variable_no_declarada" in codigos(con(variables=["cliente.nombre"]))
    assert "variable_repetida" in codigos(con(variables=["cliente.nombre", "cliente.nombre", "pedido.numero"]))
    assert "llaves_sueltas" in codigos(con(body="Hola {cliente.nombre y {pedido.numero}"))
    assert "variable_invalida" in codigos(con(body="Hola {Cliente Nombre} {pedido.numero}"))
    sin_usar = [h for h in L.lintar(con(variables=["cliente.nombre", "pedido.numero", "pedido.id"]), contexto())]
    assert any(h.code == "variable_sin_usar" and h.severity == "warning" for h in sin_usar)
    # a.b_c y a_b.c darían el mismo parámetro NAMED en Meta
    ctx = L.ContextoLint(herramientas=contexto().herramientas, variables={"a.b_c", "a_b.c"})
    assert "variable_colision" in codigos(con(body="{a.b_c} {a_b.c}", variables=["a.b_c", "a_b.c"]), ctx)


@pytest.mark.parametrize(
    ("cambio", "codigo"),
    [
        ({"apta_para_opcion": False}, "tool_no_apta"),
        ({"pack_activo": False}, "pack_inactivo"),
        ({"binding_habilitado": None}, "tool_sin_binding"),
        ({"binding_habilitado": False}, "tool_binding_deshabilitado"),
        ({"gobernada_por_procedimiento": True}, "tool_en_procedimiento"),
    ],
)
def test_una_tool_no_elegible_se_rechaza_con_su_motivo(cambio: dict[str, Any], codigo: str) -> None:
    ctx = contexto(consultar_estado_pedido=ficha(**cambio))
    hs = [h for h in errores(valida(), ctx) if h.code == codigo]
    assert hs and hs[0].path == "opciones[0].accion.tool"


def test_tool_inexistente_o_nombre_invalido() -> None:
    raw = valida()
    raw["opciones"][0]["accion"]["tool"] = "no_existe"
    assert "tool_inexistente" in codigos(raw)
    raw["opciones"][0]["accion"]["tool"] = "mala tool!"
    assert "tool_invalida" in codigos(raw)


def test_argumentos_contra_el_json_schema_de_la_tool() -> None:
    def con_args(fijos: dict[str, Any], contexto_: dict[str, str]) -> set[tuple[str, str]]:
        raw = valida()
        raw["opciones"][0]["accion"]["args_fijos"] = fijos
        raw["opciones"][0]["accion"]["args_contexto"] = contexto_
        return {(h.code, h.path) for h in errores(raw)}

    base = "opciones[0].accion"
    assert con_args({"limite": 99}, {"pedido_id": "pedido.id"}) == {("arg_invalido", f"{base}.args_fijos.limite")}
    assert ("arg_invalido", f"{base}.args_fijos.detalle") in con_args({"detalle": "si"}, {"pedido_id": "pedido.id"})
    assert ("arg_invalido", f"{base}.args_fijos.modo") in con_args({"modo": "otro"}, {"pedido_id": "pedido.id"})
    assert ("arg_desconocido", f"{base}.args_fijos.inventado") in con_args({"inventado": 1}, {"pedido_id": "pedido.id"})
    assert ("arg_desconocido", f"{base}.args_contexto.inventado") in con_args({}, {"pedido_id": "pedido.id", "inventado": "pedido.id"})
    assert ("arg_requerido_faltante", f"{base}.args_fijos.pedido_id") in con_args({}, {})
    assert ("arg_duplicado", f"{base}.args_contexto.pedido_id") in con_args({"pedido_id": "1"}, {"pedido_id": "pedido.id"})
    assert ("variable_fuera_de_catalogo", f"{base}.args_contexto.pedido_id") in con_args({}, {"pedido_id": "cliente.telefono"})
    assert con_args({"pedido_id": "A-1", "modo": "completo", "limite": 3}, {}) == set()


def test_confirmacion_obligatoria_es_un_aviso_que_el_resolver_impone() -> None:
    for cambio in ({"sensible": True}, {"binding_pide_confirmacion": True}):
        ctx = contexto(consultar_estado_pedido=ficha(**cambio))
        avisos = [h for h in L.lintar(valida(), ctx) if h.code == "confirmacion_obligatoria"]
        assert avisos and avisos[0].severity == "warning"
        f = ctx.herramientas["consultar_estado_pedido"]
        assert L.confirmacion_efectiva(Accion(tool="t", confirmar=False), f) is True
    # sin ficha: fail-closed
    assert L.confirmacion_efectiva(Accion(tool="t"), None) is True
    # nada la exige: no se impone, ni se avisa
    assert L.confirmacion_efectiva(Accion(tool="t"), ficha()) is False
    assert not [h for h in L.lintar(valida(), contexto()) if h.code == "confirmacion_obligatoria"]
    assert L.confirmacion_efectiva(Accion(tool="t", confirmar=True), ficha()) is True


def test_respuestas_y_result_keys() -> None:
    def con_resp(**campos: Any) -> set[str]:
        raw = valida()
        raw["opciones"][0].update(campos)
        return codigos(raw)

    assert "result_no_declarado" in con_resp(respuesta_ok="Está {result.otro}.")
    assert "result_en_error" in con_resp(respuesta_error="Falló: {result.estado}")
    assert "variable_fuera_de_catalogo" in con_resp(respuesta_ok="Hola {ctx.cliente.cedula}")
    assert "placeholder_desconocido" in con_resp(respuesta_ok="Hola {nombre}")
    assert "llaves_sueltas" in con_resp(respuesta_ok="Hola {ctx.cliente.nombre")
    assert "respuesta_vacia" in con_resp(respuesta_error=" ")
    assert "respuesta_larga" in con_resp(respuesta_ok="x" * 1025)
    assert "result_keys_demasiadas" in con_resp(result_keys=[f"k{i}" for i in range(6)])
    assert "result_key_repetida" in con_resp(result_keys=["estado", "estado"])
    assert "result_key_invalida" in con_resp(result_keys=["Mal Nombre"])


def test_reglas_de_cuerpo_de_meta_pasan_por_validate_template_components() -> None:
    from core.gateway import meta_templates

    assert not [h for h in L.lintar(valida(), contexto()) if h.code == "meta_componentes"]
    original = meta_templates.validate_template_components

    def rechaza(*a: Any, **k: Any) -> None:
        raise meta_templates.TemplateValidationError("BODY: regla de Meta")

    meta_templates.validate_template_components = rechaza  # type: ignore[assignment]
    try:
        hs = [h for h in L.lintar(valida(), contexto()) if h.code == "meta_componentes"]
    finally:
        meta_templates.validate_template_components = original  # type: ignore[assignment]
    assert hs and hs[0].mensaje == "BODY: regla de Meta"


def test_fichas_de_tools_desde_mcp_tool_y_bindings() -> None:
    tool = SimpleNamespace(
        name="t1", parameters=SCHEMA, plugin_id="pack_x", apta_para_opcion=True, sensible=True
    )
    sin_marca = SimpleNamespace(name="t2", parameters={}, plugin_id="pack_y")
    bindings = [SimpleNamespace(tool="t1", enabled=True, requiresConfirmation=True)]
    f = L.fichas_de_tools([tool, sin_marca], bindings, {"pack_x"}, gobernadas={"t2"})
    assert f["t1"].apta_para_opcion and f["t1"].sensible and f["t1"].pack_activo
    assert f["t1"].binding_habilitado is True and f["t1"].binding_pide_confirmacion
    assert f["t2"].apta_para_opcion is False and f["t2"].binding_habilitado is None
    assert f["t2"].gobernada_por_procedimiento and not f["t2"].pack_activo


def test_acciones_de_procedimientos_junta_etapas_precondiciones_y_confirmaciones() -> None:
    etapa = SimpleNamespace(actions=["a"], preconditions=["b"], confirmActions=["c"])
    proc = SimpleNamespace(stages=[etapa])
    assert L.acciones_de_procedimientos([proc]) == {"a", "b", "c"}
    assert L.acciones_de_procedimientos([]) == frozenset()
