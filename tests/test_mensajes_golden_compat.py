"""ADR-0178 · D1 · compatibilidad byte a byte del módulo común de mensajes con lo que había antes.

Las funciones `_antiguo_*` son copias TEXTUALES del código previo a D1 (`gateway/event_notices.py` y
`kernel/opciones/mapeo.py`). Los nombres públicos de siempre (reexportados) y el módulo nuevo deben dar
exactamente la misma salida sobre un corpus que mezcla los textos reales de cada evento, los de ejemplo de las
plantillas con opciones y valores hostiles (saltos de línea, espacios, llaves, tipos no simples).
"""

from __future__ import annotations

import inspect
import itertools
import re
import string
from typing import Any

import pytest

import core.gateway.event_notices as notices
from core.kernel import mensajes
from core.kernel.opciones import mapeo
from core.kernel.opciones.definicion import Definicion

# --------------------------------------------------------------------------- código anterior (copia fiel)
_ANTIGUO_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
_ANTIGUO_LLAVE = re.compile(r"\{([^{}]*)\}")


def _antiguo_clean_param(valor: str, limite: int = 1024) -> str:
    texto = re.sub(r"[\r\n\t]+", " ", str(valor))
    texto = re.sub(r" {2,}", " ", texto).strip()
    return texto[:limite].rstrip()


def _antiguo_lookup(context: dict[str, Any], ruta: str) -> str | None:
    actual: Any = context
    for parte in ruta.split("."):
        if not isinstance(actual, dict) or parte not in actual:
            return None
        actual = actual[parte]
    if actual is None or isinstance(actual, dict | list):
        return None
    texto = _antiguo_clean_param(str(actual))
    return texto or None


def _antiguo_render_fallback(texto: str, context: dict[str, Any], event_key: str) -> str:
    permitidas = notices.EVENTOS.get(event_key, {})

    def _sub(m: re.Match[str]) -> str:
        ruta = m.group(1)
        if ruta not in permitidas:
            return m.group(0)
        return _antiguo_lookup(context, ruta) or ""

    return re.sub(r"\{([a-z_]+\.[a-z_]+)\}", _sub, texto)


def _antiguo_renderizar(texto: str, valores: dict[str, str]) -> str:
    return _ANTIGUO_LLAVE.sub(
        lambda m: (
            _antiguo_clean_param(valores[m.group(1)]) if m.group(1) in valores else m.group(0)
        ),
        texto,
    )


def _antiguo_cuerpo_para_meta(cuerpo: str) -> str:
    return _ANTIGUO_LLAVE.sub(lambda m: "{{" + m.group(1).replace(".", "_") + "}}", cuerpo)


def _antiguo_render_body(components: Any, values: dict[str, str]) -> str:
    cuerpo = next(
        (
            c
            for c in components or []
            if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY"
        ),
        None,
    )
    return _ANTIGUO_PLACEHOLDER.sub(
        lambda m: values.get(m.group(1), m.group(0)), str((cuerpo or {}).get("text", ""))
    )


def _antiguo_placeholders(components: Any) -> list[str]:
    cuerpo = next(
        (
            c
            for c in components or []
            if isinstance(c, dict) and str(c.get("type", "")).upper() == "BODY"
        ),
        None,
    )
    vistos: list[str] = []
    for nombre in _ANTIGUO_PLACEHOLDER.findall(str((cuerpo or {}).get("text", ""))):
        if nombre not in vistos:
            vistos.append(nombre)
    return vistos


def _antiguo_fuera_del_cuerpo(components: Any) -> bool:
    for comp in components or []:
        if not isinstance(comp, dict) or str(comp.get("type", "")).upper() == "BODY":
            continue
        if _ANTIGUO_PLACEHOLDER.search(str(comp.get("text", ""))):
            return True
        for boton in comp.get("buttons") or []:
            if isinstance(boton, dict) and _ANTIGUO_PLACEHOLDER.search(str(boton.get("url", ""))):
                return True
    return False


# --------------------------------------------------------------------------- corpus
VALORES_HOSTILES: list[Any] = [
    "Ana",
    "Ana\r\nPérez\t\tde  la   Cruz",
    "  espacios   al borde  ",
    "x" * 5000,
    "y" * 1024 + "   ",
    "{cliente.nombre}",  # un valor con llaves no se vuelve a interpretar
    "a}{b{{1}}",
    "{{1}}",
    "",
    "   ",
    None,
    12345,
    3.5,
    True,
    {"a": 1},
    ["x"],
    "ñandú «comillas» 😀",
]


def _contexto(valor: Any) -> dict[str, Any]:
    """Contexto con `valor` en cada ruta de cada evento (anidado como lo arma el servidor)."""
    ctx: dict[str, Any] = {}
    for rutas in notices.EVENTOS.values():
        for ruta in rutas:
            nodo = ctx
            *cabeza, hoja = ruta.split(".")
            for parte in cabeza:
                nodo = nodo.setdefault(parte, {})
            nodo[hoja] = valor
    return ctx


def _textos_de_evento(event_key: str) -> list[str]:
    rutas = list(notices.EVENTOS[event_key])
    return [
        " / ".join("{" + r + "}" for r in rutas),  # todas las rutas permitidas
        *("Hola {" + r + "}, gracias." for r in rutas),
        "{cliente.nombre} {ruta.fuera} {a.b.c} {x} {} {{1}} { cliente.nombre } {Cliente.nombre} {cliente.nombre",
        "sin variables",
        "",
    ]


EJEMPLOS_DE_OPCIONES = [
    "Hola {cliente.nombre}, tu pedido {pedido.numero} está listo.",
    "Hola {cliente.nombre}",
    "Hola {negocio.nombre} y {cliente.nombre}: {pedido.numero}{pedido.estado}",
    "{cliente.nombre}\n{cliente.nombre}",
    "Sin variables",
    "Llaves {} sueltas { y } y {a b}",
    "{ctx.cita.hora} {result.estado}",
    "",
]
VALORES_DE_OPCIONES = [
    {},
    {"cliente.nombre": "Ana\nPérez", "pedido.numero": "A-1"},
    {
        "cliente.nombre": "{pedido.numero}",
        "pedido.numero": "A-1",
        "negocio.nombre": "  Roma   ",
        "pedido.estado": "ok",
    },
    {"cliente.nombre": "x" * 3000, "ctx.cita.hora": "10:00", "result.estado": "\tlisto"},
    {"cliente.nombre": ""},
]


# --------------------------------------------------------------------------- saneo
@pytest.mark.parametrize(
    "valor",
    [v for v in VALORES_HOSTILES if isinstance(v, str)] + ["a" + chr(160) * 2 + "b", "\n\n\n"],
)
@pytest.mark.parametrize("limite", [None, 0, 1, 2, 60, 1024])
def test_clean_param_igual_al_anterior(valor: str, limite: int | None) -> None:
    args = () if limite is None else (limite,)
    esperado = _antiguo_clean_param(valor, *args)
    assert mensajes.clean_param(valor, *args) == esperado
    assert notices.clean_param(valor, *args) == esperado


def test_clean_param_y_max_param_siguen_siendo_los_mismos_objetos() -> None:
    assert notices.clean_param is mensajes.clean_param
    assert notices.MAX_PARAM == mensajes.MAX_PARAM == 1024


# --------------------------------------------------------------------------- avisos por evento
@pytest.mark.parametrize("event_key", sorted(notices.EVENTOS))
def test_render_fallback_de_cada_evento_igual_al_anterior(event_key: str) -> None:
    for valor, texto in itertools.product(VALORES_HOSTILES, _textos_de_evento(event_key)):
        ctx = _contexto(valor)
        assert notices.render_fallback(texto, ctx, event_key) == _antiguo_render_fallback(
            texto, ctx, event_key
        ), (
            event_key,
            texto,
            valor,
        )


def test_render_fallback_con_evento_desconocido_o_contexto_vacio() -> None:
    for texto in ["{cliente.nombre}", "x"]:
        for ctx in ({}, {"cliente": None}, {"cliente": "texto"}, {"cliente": {"nombre": None}}):
            for evento in ("no.existe", "subscription.charge_created"):
                assert notices.render_fallback(texto, ctx, evento) == _antiguo_render_fallback(
                    texto, ctx, evento
                )


def test_texto_de_respaldo_real_de_la_invitacion() -> None:
    from core.provisioning.owner import EVENTO_INVITACION, TEXTO_WHATSAPP, contexto_del_aviso

    ctx = contexto_del_aviso(
        negocio="Pizzería\nRoma", nombre="Ana  Pérez", email="ana.perez@correo.co"
    )
    assert notices.render_fallback(
        TEXTO_WHATSAPP, ctx, EVENTO_INVITACION
    ) == _antiguo_render_fallback(TEXTO_WHATSAPP, ctx, EVENTO_INVITACION)


@pytest.mark.parametrize("event_key", sorted(notices.EVENTOS))
def test_resolve_values_igual_al_anterior(event_key: str) -> None:
    """Resolución con ruta permitida, fuera de la lista y sin dato: mismos valores y MISMO mensaje de error."""
    rutas = list(notices.EVENTOS[event_key])

    def antiguo(variables: Any, placeholders: list[str], ctx: dict[str, Any]) -> Any:
        permitidas = notices.EVENTOS.get(event_key, {})
        mapeo_ = (
            {str(k): str(v) for k, v in variables.items()}
            if isinstance(variables, dict)
            else {n: str(r) for n, r in zip(placeholders, variables, strict=False)}
        )
        if set(mapeo_) != set(placeholders):
            return (
                "error",
                f"La plantilla pide {len(placeholders)} variables y el mapeo tiene {len(mapeo_)}.",
            )
        valores = {}
        for nombre in placeholders:
            ruta = mapeo_[nombre]
            if ruta not in permitidas:
                return ("error", f"La ruta «{ruta}» no está permitida para {event_key}.")
            valor = _antiguo_lookup(ctx, ruta)
            if valor is None:
                return ("error", f"Falta el dato «{ruta}» en el evento.")
            valores[nombre] = valor
        return ("ok", valores)

    def nuevo(variables: Any, placeholders: list[str], ctx: dict[str, Any]) -> Any:
        try:
            return ("ok", notices.resolve_values(event_key, variables, placeholders, ctx))
        except notices.NoticeConfigError as exc:
            return ("error", str(exc))

    casos = [
        (rutas[:2], ["1", "2"]),
        ({"nombre": rutas[0]}, ["nombre"]),
        (["no.permitida"], ["1"]),
        (rutas[:1], ["1", "2"]),
        ([], []),
    ]
    for valor in VALORES_HOSTILES:
        ctx = _contexto(valor)
        for variables, placeholders in casos:
            assert nuevo(variables, placeholders, ctx) == antiguo(variables, placeholders, ctx)
    assert nuevo(rutas[:1], ["1"], {}) == antiguo(rutas[:1], ["1"], {})


# --------------------------------------------------------------------------- plantillas de Meta
COMPONENTES = [
    None,
    [],
    [{"type": "BODY", "text": "Hola {{1}}, tu cargo de {{ 2 }} vence {{1}}"}],
    [{"type": "body", "text": "Hola {{nombre}} {{cargo_monto}}"}],
    [{"type": "BODY", "text": "sin variables"}],
    [{"type": "HEADER", "text": "Cargo {{1}}"}, {"type": "BODY", "text": "x {{1}}"}],
    [
        {"type": "BODY", "text": "x"},
        {"type": "BUTTONS", "buttons": [{"type": "URL", "url": "https://a.co/{{1}}"}]},
    ],
    [
        {"type": "BODY", "text": "x"},
        {"type": "BUTTONS", "buttons": [{"type": "QUICK_REPLY", "text": "Sí"}, "raro"]},
    ],
    ["no es dict", {"type": "BODY"}, {"type": "FOOTER", "text": "{{ a }}"}],
]


@pytest.mark.parametrize("componentes", COMPONENTES)
def test_lectura_de_componentes_igual_a_la_anterior(componentes: Any) -> None:
    assert notices.template_placeholders(componentes) == _antiguo_placeholders(componentes)
    assert notices.template_has_non_body_variables(componentes) == _antiguo_fuera_del_cuerpo(
        componentes
    )
    valores = {"1": "A", "2": "B", "nombre": "Ana", "cargo_monto": "$5"}
    plantilla = notices.MessageTemplate(components=componentes, parameter_format="POSITIONAL")
    assert notices.render_body(plantilla, valores) == _antiguo_render_body(componentes, valores)
    assert notices.render_body(plantilla, {}) == _antiguo_render_body(componentes, {})


def test_build_components_no_cambia() -> None:
    comps = [{"type": "BODY", "text": "Hola {{nombre}} {{monto}}"}]
    plantilla = notices.MessageTemplate(components=comps, parameter_format="NAMED")
    assert notices.build_components(plantilla, {"nombre": "Ana\nPérez", "monto": "$ 5"}) == [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": "Ana Pérez", "parameter_name": "nombre"},
                {"type": "text", "text": "$ 5", "parameter_name": "monto"},
            ],
        }
    ]


# --------------------------------------------------------------------------- plantillas con opciones
@pytest.mark.parametrize("texto", EJEMPLOS_DE_OPCIONES)
@pytest.mark.parametrize("valores", VALORES_DE_OPCIONES)
def test_renderizar_de_opciones_igual_al_anterior(texto: str, valores: dict[str, str]) -> None:
    assert mapeo.renderizar(texto, valores) == _antiguo_renderizar(texto, valores)
    assert mensajes.render_llaves(texto, valores) == _antiguo_renderizar(texto, valores)


@pytest.mark.parametrize("texto", EJEMPLOS_DE_OPCIONES)
def test_cuerpo_para_meta_igual_al_anterior(texto: str) -> None:
    assert mapeo.cuerpo_para_meta(texto) == _antiguo_cuerpo_para_meta(texto)


def test_los_ejemplos_de_los_tests_de_opciones_dan_lo_mismo_de_punta_a_punta() -> None:
    """Definición completa: render del cuerpo, componentes de Meta y parámetros de envío."""
    d = Definicion.model_validate(
        {
            "body": "Hola {cliente.nombre}, tu pedido {pedido.numero} está listo.",
            "footer": "Gracias",
            "variables": ["cliente.nombre", "pedido.numero"],
            "opciones": [
                {
                    "id": "o1",
                    "titulo": "Ver",
                    "accion": {
                        "tool": "consultar_estado_pedido",
                        "args_fijos": {},
                        "args_contexto": {},
                        "confirmar": False,
                    },
                    "respuesta_ok": "Listo.",
                    "respuesta_error": "No pude.",
                    "result_keys": [],
                }
            ],
        }
    )
    valores = {"cliente.nombre": "Ana\nPérez", "pedido.numero": "A-1"}
    assert mapeo.renderizar(d.body, valores) == "Hola Ana Pérez, tu pedido A-1 está listo."
    comps = mapeo.componentes_meta(d)
    assert comps[0]["text"] == "Hola {{cliente_nombre}}, tu pedido {{pedido_numero}} está listo."
    assert (
        mapeo.cuerpo_de_plantilla_renderizado(d, valores)
        == "Hola Ana Pérez, tu pedido A-1 está listo."
    )
    assert mapeo.parametros_de_cuerpo_meta(d, valores)[0]["parameters"] == [
        {"type": "text", "text": "Ana Pérez", "parameter_name": "cliente_nombre"},
        {"type": "text", "text": "A-1", "parameter_name": "pedido_numero"},
    ]


# --------------------------------------------------------------------------- conversor sobre los textos de los packs
def _textos_de_packs() -> list[str]:
    import pack_calendar.config as cal
    import pack_commerce.config as com
    import pack_subscriptions.config as sub
    from pydantic import BaseModel

    textos: list[str] = []
    for modulo in (cal, com, sub):
        for _, clase in inspect.getmembers(modulo, inspect.isclass):
            if not issubclass(clase, BaseModel) or clase.__module__ != modulo.__name__:
                continue
            for campo in clase.model_fields.values():
                extra = campo.json_schema_extra
                if (
                    isinstance(extra, dict)
                    and "mensaje" in extra
                    and isinstance(campo.default, str)
                    and campo.default
                ):
                    textos.append(campo.default)
    return textos


def test_variables_de_reconoce_lo_que_str_format_de_los_packs_considera_campo() -> None:
    """Los packs rellenan sus textos con `str.format`; el conversor debe ver las mismas variables."""
    textos = _textos_de_packs()
    assert textos, "no se encontró ningún texto de mensaje en los packs"
    for texto in textos:
        esperadas: list[str] = []
        for _, campo, _, _ in string.Formatter().parse(texto):
            if campo and campo not in esperadas:
                esperadas.append(campo)
        assert mensajes.variables_de(texto) == esperadas, texto


def test_unica_diferencia_consciente_un_cuerpo_ya_en_formato_meta_no_se_dobla() -> None:
    """Antes `{{1}}` salía como `{{{1}}}`; el lint de opciones rechaza ese cuerpo (llaves sueltas), así que no se
    alcanza. Se fija el nuevo resultado para que un cambio futuro sea deliberado."""
    assert _antiguo_cuerpo_para_meta("Hola {{1}}") == "Hola {{{1}}}"
    assert mapeo.cuerpo_para_meta("Hola {{1}}") == "Hola {{1}}"
