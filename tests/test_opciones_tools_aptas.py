"""SPEC-40 (PR 10a) · las tools marcadas `apta_para_opcion` son una lista corta y REVISADA por el dueño.

Una tool apta puede ejecutarse sin LLM al toque de un botón: por eso la lista no crece sin que alguien la revise.
Este archivo fija la lista, vigila que no se cuele otra en el código y comprueba que las marcadas se pueden usar
de verdad (JSON Schema legible por el formulario del panel y un uso típico que pasa el lint).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.kernel.opciones import lint as L

RAIZ = Path(__file__).resolve().parent.parent

# Para agregar una tool a esta lista: revisión del dueño en el PR (solo lectura o escritura reversible e idempotente,
# nada financiero, destructivo ni que escriba a terceros) y la fila correspondiente en el runbook
# `docs/runbooks/opciones-directas.md`.
APTAS_REVISADAS = {"list_client_appointments"}
# Solo lee: no hace falta forzar la confirmación del cliente.
SENSIBLES: set[str] = set()


def _tools_del_codigo() -> dict[str, Any]:
    from pack_calendar.skills import CalendarSkill
    from pack_cobranza.skills import CobranzaSkill
    from pack_commerce.skills import CatalogSkill, LeadSkill, OrderSkill
    from pack_knowledge.skills import KnowledgeSkill

    from core.subscriptions.tools import SubscriptionSkill
    from core.tools.core_skill import CoreSkill

    tools: dict[str, Any] = {}
    for clase in (CoreSkill, SubscriptionSkill, CalendarSkill, CobranzaSkill, KnowledgeSkill, LeadSkill, CatalogSkill, OrderSkill):
        skill = clase.__new__(clase)
        try:
            clase.__init__(skill)  # type: ignore[misc]
        except TypeError:
            pass
        for tool in skill.get_tools():
            tools[tool.name] = tool
    return tools


def test_solo_las_tools_revisadas_estan_marcadas_aptas() -> None:
    tools = _tools_del_codigo()
    aptas = {n for n, t in tools.items() if t.apta_para_opcion}
    assert aptas == APTAS_REVISADAS, (
        f"hay tools aptas sin revisar o revisadas sin marcar: {sorted(aptas ^ APTAS_REVISADAS)}"
    )
    assert {n for n, t in tools.items() if t.sensible} == SENSIBLES
    assert not tools["enviar_plantilla_opciones"].apta_para_opcion, "una opción no puede enviar otra plantilla"


def test_ningun_otro_archivo_del_codigo_marca_una_tool_apta() -> None:
    """Un `apta_para_opcion=True` en una tool nueva (o en un pack nuevo) rompe esto antes de llegar a revisión."""
    esperados = {"packages/pack-calendar/pack_calendar/skills.py"}
    encontrados: set[str] = set()
    for carpeta in ("core", "packages"):
        for ruta in (RAIZ / carpeta).rglob("*.py"):
            if re.search(r"apta_para_opcion\s*=\s*True", ruta.read_text(encoding="utf-8")):
                encontrados.add(ruta.relative_to(RAIZ).as_posix())
    # `mcp/declarative.py` la deriva de `option_safe` (el dueño la marca en su propia tool declarativa).
    assert encontrados - {"core/mcp/declarative.py"} == esperados


def test_las_marcadas_no_exigen_argumentos_del_cliente_y_su_esquema_sirve_al_formulario() -> None:
    """Sin `args_contexto` disponibles (la lista de rutas de datos está vacía a propósito) la acción solo puede
    llevar `args_fijos`: ningún argumento obligatorio. Cada propiedad lleva tipo simple y descripción para el panel."""
    tools = _tools_del_codigo()
    for nombre in APTAS_REVISADAS:
        esquema = tools[nombre].parameters
        assert esquema["type"] == "object"
        assert not esquema.get("required"), f"{nombre} tiene argumentos obligatorios: no se puede ofrecer en un botón"
        for propiedad, regla in esquema.get("properties", {}).items():
            assert regla.get("type") in {"string", "integer", "number", "boolean"}, (nombre, propiedad)
            assert str(regla.get("description", "")).strip(), (nombre, propiedad)


def _ficha(tool: Any, *, pack_activo: bool = True, binding: bool | None = True) -> L.FichaTool:
    return L.FichaTool(
        nombre=tool.name,
        parameters=tool.parameters,
        apta_para_opcion=tool.apta_para_opcion,
        sensible=tool.sensible,
        pack_activo=pack_activo,
        binding_habilitado=binding,
    )


def _definicion(tool: str, args_fijos: dict[str, Any], respuesta: str, claves: list[str]) -> dict[str, Any]:
    return {
        "body": "Hola {cliente.nombre}, ¿qué necesitas?",
        "footer": None,
        "variables": ["cliente.nombre"],
        "exclusivo": True,
        "ttl_horas": 24,
        "opciones": [
            {
                "id": "consulta",
                "titulo": "Consultar",
                "accion": {"tool": tool, "args_fijos": args_fijos, "args_contexto": {}, "confirmar": False},
                "respuesta_ok": respuesta,
                "respuesta_error": "No pude consultarlo ahora. Un asesor te ayuda en breve.",
                "result_keys": claves,
            }
        ],
    }


USOS_TIPICOS = {
    "list_client_appointments": ({}, "Tienes {result.proximas} cita(s) próxima(s). Escríbenos si quieres moverlas.", ["proximas"]),
}


def test_un_uso_tipico_de_cada_marcada_pasa_el_lint_y_sin_binding_o_pack_se_rechaza() -> None:
    tools = _tools_del_codigo()
    variables = {"cliente.nombre"}
    for nombre, (args, respuesta, claves) in USOS_TIPICOS.items():
        crudo = _definicion(nombre, args, respuesta, claves)
        bien = L.ContextoLint(herramientas={nombre: _ficha(tools[nombre])}, variables=variables)
        assert [h for h in L.lintar(crudo, bien) if h.severity == "error"] == [], nombre
        sin_binding = L.ContextoLint(herramientas={nombre: _ficha(tools[nombre], binding=None)}, variables=variables)
        assert "tool_sin_binding" in {h.code for h in L.lintar(crudo, sin_binding)}
        sin_pack = L.ContextoLint(herramientas={nombre: _ficha(tools[nombre], pack_activo=False)}, variables=variables)
        assert "pack_inactivo" in {h.code for h in L.lintar(crudo, sin_pack)}
        apagada = L.ContextoLint(herramientas={nombre: _ficha(tools[nombre], binding=False)}, variables=variables)
        assert "tool_binding_deshabilitado" in {h.code for h in L.lintar(crudo, apagada)}
    assert set(USOS_TIPICOS) == APTAS_REVISADAS


def test_los_argumentos_fijos_que_el_dueno_podria_poner_se_validan_contra_el_esquema() -> None:
    tools = _tools_del_codigo()
    variables = {"cliente.nombre"}
    nombre = "list_client_appointments"
    contexto = L.ContextoLint(herramientas={nombre: _ficha(tools[nombre])}, variables=variables)
    _, respuesta, claves = USOS_TIPICOS[nombre]
    for invalido in ({"attendee_email": 5}, {"no_existe": "x"}):
        hallazgos = L.lintar(_definicion(nombre, invalido, respuesta, claves), contexto)
        assert any(h.severity == "error" for h in hallazgos), invalido
