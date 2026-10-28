"""ADR-0178 · D1 · módulo común de variables y render (`core/kernel/mensajes`): unidades y límites de capa."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.kernel import mensajes
from core.kernel.mensajes import (
    DatoFaltante,
    RutaNoPermitida,
    a_llaves,
    a_meta,
    clean_param,
    placeholders_del_cuerpo,
    render_fallback,
    render_llaves,
    render_meta,
    resolver_ruta,
    tiene_variables_fuera_del_cuerpo,
    valor_de_ruta,
    variables_de,
)

PERMITIDAS = {"cliente.nombre": "Nombre", "cargo.monto": "Monto"}


# ------------------------------------------------------------------ saneo
def test_clean_param_quita_saltos_tabulaciones_y_espacios_repetidos() -> None:
    assert clean_param("Ana\r\n\r\nPérez\t\tde   la Cruz") == "Ana Pérez de la Cruz"
    assert clean_param("  Ana  ") == "Ana"


def test_clean_param_acota_el_largo_y_no_deja_espacio_final() -> None:
    assert len(clean_param("x" * 5000)) == mensajes.MAX_PARAM
    assert clean_param("ab cd", 3) == "ab"
    assert clean_param("Ana", 0) == ""


# ------------------------------------------------------------------ rutas
def test_valor_de_ruta_solo_devuelve_datos_simples_y_saneados() -> None:
    ctx = {
        "cliente": {
            "nombre": "Ana\nPérez",
            "id": 7,
            "tags": ["a"],
            "extra": {"x": 1},
            "vacio": "  ",
        }
    }
    assert valor_de_ruta(ctx, "cliente.nombre") == "Ana Pérez"
    assert valor_de_ruta(ctx, "cliente.id") == "7"
    for ruta in (
        "cliente.tags",
        "cliente.extra",
        "cliente.vacio",
        "cliente.no",
        "otro.nombre",
        "cliente.nombre.x",
    ):
        assert valor_de_ruta(ctx, ruta) is None


def test_resolver_ruta_rechaza_lo_que_no_esta_en_la_lista_permitida() -> None:
    ctx = {"cliente": {"nombre": "Ana"}, "secreto": {"token": "abc"}}
    assert resolver_ruta(ctx, "cliente.nombre", PERMITIDAS) == "Ana"
    with pytest.raises(RutaNoPermitida) as exc:
        resolver_ruta(ctx, "secreto.token", PERMITIDAS)
    assert exc.value.ruta == "secreto.token"


def test_resolver_ruta_permitida_sin_dato_es_dato_faltante() -> None:
    with pytest.raises(DatoFaltante) as exc:
        resolver_ruta({"cliente": {}}, "cliente.nombre", PERMITIDAS)
    assert exc.value.ruta == "cliente.nombre"
    # La lista permitida manda sobre el dato: una ruta fuera de ella no se mira ni existiendo.
    with pytest.raises(RutaNoPermitida):
        resolver_ruta({"cargo": {"link": "x"}}, "cargo.link", PERMITIDAS)


def test_ruta_variable_acepta_la_forma_de_una_variable() -> None:
    for ok in ("cliente", "cliente.nombre", "a.b.c.d", "pedido_1.n2"):
        assert mensajes.RUTA_VARIABLE.match(ok), ok
    for mal in ("Cliente.nombre", "a..b", "1a", "a.b.c.d.e", "a b", "{a}", ""):
        assert not mensajes.RUTA_VARIABLE.match(mal), mal


# ------------------------------------------------------------------ render
def test_render_fallback_variable_faltante_queda_vacia_y_la_no_permitida_queda_tal_cual() -> None:
    ctx = {"cliente": {"nombre": "Ana"}, "secreto": {"token": "abc"}}
    assert render_fallback("Hola {cliente.nombre}", ctx, PERMITIDAS) == "Hola Ana"
    assert render_fallback("Debes {cargo.monto}.", ctx, PERMITIDAS) == "Debes ."
    assert render_fallback("{secreto.token}", ctx, PERMITIDAS) == "{secreto.token}"
    assert render_fallback("{x} {a.b.c} {{1}}", ctx, PERMITIDAS) == "{x} {a.b.c} {{1}}"


def test_los_valores_con_llaves_no_se_reinterpretan() -> None:
    """Inyección: el nombre «{cargo.monto}» sale literal; una sola pasada, ni el render ni Meta lo re-expanden."""
    ctx = {"cliente": {"nombre": "{cargo.monto}"}, "cargo": {"monto": "$ 999"}}
    assert render_fallback("Hola {cliente.nombre}", ctx, PERMITIDAS) == "Hola {cargo.monto}"
    assert render_llaves("Hola {cliente.nombre}", {"cliente.nombre": "{{1}}}{"}) == "Hola {{1}}}{"
    assert render_meta("Hola {{1}} {{2}}", {"1": "{{2}}", "2": "B"}) == "Hola {{2}} B"


def test_render_llaves_sin_valor_deja_la_variable_visible_y_sanea_el_resto() -> None:
    assert render_llaves("Hola {cliente.nombre}", {}) == "Hola {cliente.nombre}"
    assert render_llaves("{a}-{b}", {"a": "x\ny"}) == "x y-{b}"


def test_render_meta_acepta_espacios_y_nombres_y_deja_los_desconocidos() -> None:
    assert (
        render_meta("{{ 1 }} y {{nombre}} y {{9}}", {"1": "A", "nombre": "Ana"})
        == "A y Ana y {{9}}"
    )
    assert render_meta("{{a b}} {{}}", {}) == "{{a b}} {{}}"


# ------------------------------------------------------------------ plantillas de Meta
def test_lectura_de_componentes() -> None:
    comps = [
        {"type": "HEADER", "text": "Cargo {{1}}"},
        {"type": "body", "text": "Hola {{1}} {{nombre}} {{1}}"},
        {"type": "BUTTONS", "buttons": [{"type": "URL", "url": "https://a.co/{{1}}"}]},
    ]
    assert placeholders_del_cuerpo(comps) == ["1", "nombre"]
    assert tiene_variables_fuera_del_cuerpo(comps) is True
    assert tiene_variables_fuera_del_cuerpo([comps[1]]) is False
    assert placeholders_del_cuerpo(None) == [] and tiene_variables_fuera_del_cuerpo(None) is False


# ------------------------------------------------------------------ conversor
def test_variables_de_acepta_los_tres_dialectos() -> None:
    assert variables_de(
        "Hola {cliente.nombre}, {total} y {{1}} ({{ nombre }}) {cliente.nombre} {}"
    ) == [
        "cliente.nombre",
        "total",
        "1",
        "nombre",
    ]
    assert variables_de("sin variables") == []


def test_a_meta_convierte_las_llaves_y_respeta_lo_que_ya_es_meta() -> None:
    assert (
        a_meta("Hola {cliente.nombre}, tu pedido {pedido.numero}")
        == "Hola {{cliente_nombre}}, tu pedido {{pedido_numero}}"
    )
    assert a_meta("Hola {{1}} y {total}") == "Hola {{1}} y {{total}}"
    assert a_meta("Hola {{ 1 }}") == "Hola {{ 1 }}"
    assert a_meta("sin nada") == "sin nada"


def test_a_llaves_convierte_los_placeholders_de_meta() -> None:
    assert (
        a_llaves("Hola {{1}}, {{ nombre }} y {cliente.nombre}")
        == "Hola {1}, {nombre} y {cliente.nombre}"
    )
    assert a_llaves("{{no valido!}}") == "{{no valido!}}"


def test_ida_y_vuelta_de_un_texto_con_rutas() -> None:
    texto = "Hola {cliente_nombre}, tu cargo {cargo_monto}"
    assert a_llaves(a_meta(texto)) == texto


# ------------------------------------------------------------------ capa
PROHIBIDOS = (
    "core.gateway",
    "core.db",
    "core.orchestrator",
    "core.admin",
    "core.api",
    "core.mcp",
    "pack_",
    "packages",
)


def test_el_modulo_comun_es_puro_y_no_importa_de_gateway_ni_de_packs() -> None:
    """Invariantes 2 y 5 de agent-core y `agentcore-conventions`: el kernel no sube a gateway/db/packs."""
    carpeta = Path(mensajes.__file__).parent
    archivos = sorted(carpeta.glob("*.py"))
    assert len(archivos) >= 5
    for archivo in archivos:
        for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
            if isinstance(nodo, ast.Import):
                nombres = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom):
                nombres = [nodo.module or ""]
            else:
                continue
            for nombre in nombres:
                assert not nombre.startswith(PROHIBIDOS), f"{archivo.name} importa {nombre}"
                assert nombre.split(".")[0] not in {"sqlalchemy", "httpx", "redis", "os"}, (
                    f"{archivo.name} importa {nombre}"
                )


def _importes_del_gateway(archivo: Path) -> set[str]:
    """Nombres que el archivo trae de `core.gateway` (en cualquier parte, también imports perezosos)."""
    traidos: set[str] = set()
    for nodo in ast.walk(ast.parse(archivo.read_text(encoding="utf-8"))):
        if isinstance(nodo, ast.ImportFrom) and (nodo.module or "").startswith("core.gateway"):
            traidos.update(a.name for a in nodo.names)
    return traidos


def test_opciones_ya_no_sube_al_gateway_para_sanear_ni_renderizar() -> None:
    """El saneo y el render de `kernel/opciones` vienen de `kernel.mensajes`. Lo único que mapeo todavía le pide a
    `gateway.event_notices` es armar los componentes de envío y el cuerpo de una fila `MessageTemplate`."""
    base = Path(mensajes.__file__).parent.parent / "opciones"
    for nombre in ("contexto.py", "argumentos.py", "definicion.py"):
        assert _importes_del_gateway(base / nombre) == set(), nombre
    assert _importes_del_gateway(base / "mapeo.py") == {"build_components", "render_body"}
