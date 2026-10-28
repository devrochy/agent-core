"""SPEC-40 · validador de argumentos, campos nuevos de MCPTool, `option_safe` y bandera (unitarios)."""

from __future__ import annotations

import pytest

from core.kernel.opciones import bandera
from core.kernel.opciones.esquema import errores
from core.mcp import declarative
from core.mcp.types import MCPTool


def _ok(valor, schema) -> bool:
    return errores(valor, schema) == []


def test_validador_de_tipos_rangos_enum_y_objetos() -> None:
    assert _ok("a", {"type": "string", "minLength": 1, "maxLength": 3})
    assert not _ok("abcd", {"type": "string", "maxLength": 3})
    assert not _ok(True, {"type": "integer"}) and not _ok(True, {"type": "number"})
    assert _ok(3, {"type": "number"}) and _ok(2.5, {"type": ["number", "null"]}) and _ok(None, {"type": ["number", "null"]})
    assert not _ok(0, {"type": "integer", "minimum": 1}) and not _ok(11, {"type": "integer", "maximum": 10})
    assert not _ok("x", {"enum": ["a", "b"]}) and _ok("a", {"enum": ["a", "b"]})
    assert not _ok("abc", {"type": "string", "pattern": "^[0-9]+$"})
    assert _ok("abc", {"type": "string", "pattern": "(["})  # patrón roto de la tool: no culpa al dueño
    assert not _ok([1, "x"], {"type": "array", "items": {"type": "integer"}})
    assert errores({"a": 1}, {"type": "object", "properties": {"a": {"type": "string"}}}) == [("a", "Debe ser de tipo string.")]
    assert ("b", "Falta este campo.") in errores({}, {"type": "object", "required": ["b"]})
    assert ("z", "Campo desconocido.") in errores({"z": 1}, {"type": "object", "additionalProperties": False})
    assert _ok("cualquier cosa", {"anyOf": [{"type": "integer"}]})  # no se evalúan anyOf/oneOf/$ref


def test_los_campos_nuevos_de_mcptool_nacen_en_falso() -> None:
    async def h(a, c):
        return {}

    tool = MCPTool(name="t", description="d", parameters={}, handler=h)
    assert tool.apta_para_opcion is False and tool.sensible is False


def _decl(**extra) -> dict:
    return {"name": "consultar", "url": "https://api.negocio.com/x", "allowed_domains": ["api.negocio.com"], **extra}


def test_option_safe_solo_con_el_literal_true() -> None:
    assert declarative.build_tool(declarative.validate_declarative_tools([_decl(option_safe=True)])[0]).apta_para_opcion
    for valor in ("true", 1, None, "yes"):
        (d,) = declarative.validate_declarative_tools([_decl(option_safe=valor)])
        assert "option_safe" not in d and declarative.build_tool(d).apta_para_opcion is False


def test_las_definiciones_existentes_no_cambian_de_forma() -> None:
    (d,) = declarative.validate_declarative_tools([_decl()])
    assert "option_safe" not in d and declarative.build_tool(d).apta_para_opcion is False


def test_una_declarativa_que_escribe_es_sensible() -> None:
    get = declarative.build_tool(declarative.validate_declarative_tools([_decl(method="GET")])[0])
    post = declarative.build_tool(declarative.validate_declarative_tools([_decl(method="POST")])[0])
    assert get.sensible is False and post.sensible is True


@pytest.mark.parametrize(
    ("settings", "esperado"),
    [
        (None, False), ({}, False), ({"opciones_directas": None}, False), ({"opciones_directas": {}}, False),
        ({"opciones_directas": True}, False), ({"opciones_directas": {"habilitada": "true"}}, False),
        ({"opciones_directas": {"habilitada": 1}}, False), ({"opciones_directas": {"habilitada": False}}, False),
        ({"agent_os": {"opciones_directas": {"habilitada": True}}}, False),
        ({"opciones_directas": {"habilitada": True}}, True),
    ],
)
def test_la_bandera_esta_apagada_salvo_el_literal_true(settings, esperado: bool) -> None:
    assert bandera.habilitada_desde(settings) is esperado


def test_nadie_fuera_del_modulo_ni_de_la_ruta_master_toca_la_bandera() -> None:
    """`bandera.fijar` solo la llama la ruta de plataforma master-only (`platform.py`). Los otros módulos que
    importan la bandera solo la LEEN: el consumer (`habilitada`, HU-OPC-03), el envío (`habilitada`, HU-OPC-02) y la
    tool `enviar_plantilla_opciones` (`oferta_habilitada`) la API de plantillas (`habilitada`, PR 6) y el catálogo de mensajes (`habilitada`, solo lectura); nunca la
    escriben. Entre los routers, solo `platform.py` (master) importa la bandera: NINGÚN router del dueño."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "core"
    lectores = {
        "core/orchestrator/consumer.py",
        "core/gateway/opciones_envio.py",
        "core/tools/core_skill.py",
        "core/api/routers/plantillas_opciones.py",
        "core/admin/catalogo_mensajes_service.py",  # catálogo de mensajes (solo lectura): omite `opciones` si está apagada
    }
    # La ruta master-only de plataforma es la ÚNICA que llama a `fijar` (y a `leer` para el GET).
    escritor = "core/api/routers/platform.py"
    permitidos = {"core/kernel/opciones/bandera.py", escritor, *lectores}
    importadores = []
    for ruta in raiz.rglob("*.py"):
        rel = ruta.relative_to(raiz.parent).as_posix()
        texto = ruta.read_text(encoding="utf-8")
        if rel not in permitidos and ("opciones.bandera" in texto or "opciones import bandera" in texto):
            importadores.append(rel)
        if rel in lectores:
            assert "bandera.fijar" not in texto, f"{rel} lee la bandera de opciones directas; no la escribe"
    assert importadores == [], f"módulos que importan la bandera de opciones directas: {importadores}"
    import ast

    arbol = ast.parse((raiz.parent / escritor).read_text(encoding="utf-8"))
    rutas = {n.name: n for n in ast.walk(arbol) if isinstance(n, ast.AsyncFunctionDef)}
    for nombre in ("get_opciones_directas", "put_opciones_directas"):
        llamadas = [
            c.func.id if isinstance(c.func, ast.Name) else getattr(c.func, "attr", "")
            for c in sorted(
                (c for st in rutas[nombre].body for c in ast.walk(st) if isinstance(c, ast.Call)), key=lambda c: (c.lineno, c.col_offset)
            )
        ]
        assert llamadas[0] == "_require_master_agent_os", f"{nombre}: el guard de master va primero"
    assert "fijar" in [getattr(c.func, "attr", "") for c in ast.walk(rutas["put_opciones_directas"]) if isinstance(c, ast.Call)]
    assert sum(1 for n in ast.walk(arbol) if isinstance(n, ast.Attribute) and n.attr == "fijar" and getattr(n.value, "id", "") == "bandera") == 1
    # Nadie más escribe la clave con SQL directo.
    for ruta in (*raiz.rglob("*.py"), *(raiz.parent / "packages").rglob("*.py")):
        rel = ruta.relative_to(raiz.parent).as_posix()
        if rel != "core/kernel/opciones/bandera.py" and "{opciones_directas}" in ruta.read_text(encoding="utf-8"):
            raise AssertionError(f"{rel} escribe la bandera de opciones directas con SQL")


def test_ya_confirmada_solo_la_pasa_el_toque_de_una_opcion() -> None:
    """`_execute_tool_safely(..., ya_confirmada=True)` salta el `requiresConfirmation` del binding: solo el toque de
    una opción directa lo usa, porque el resolver ya exigió (y obtuvo) la confirmación del cliente con `optc:`."""
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent / "core"
    quienes = sorted(
        ruta.relative_to(raiz.parent).as_posix()
        for ruta in raiz.rglob("*.py")
        if "ya_confirmada=True" in ruta.read_text(encoding="utf-8")
    )
    assert quienes == ["core/orchestrator/opciones_toque.py"], quienes
