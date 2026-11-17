"""HU-CRM-18 · `core.leads.etapas` es el único lugar que escribe la etapa del lead (ADR-0193: el lead es del core).

Estático (AST): ninguna asignación a `<algo-de-lead>.status` fuera de `core/leads/etapas.py`, ni en el core ni en los
packs. Crear un lead con `Lead(status=...)` o con el INSERT de `servicio.asegurar` es alta, no cambio, y no cuenta;
`quote.status`/`order.status` son de otras tablas. La única excepción es la siembra de QA
(`core/leads/servicio.py::sembrar_para_qa`): fija el estado inicial del escenario, sin historia.
"""

from __future__ import annotations

import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PERMITIDO = "core/leads/etapas.py"
# (archivo, función) donde una escritura directa está permitida y anotada en el código.
EXCEPCIONES = {("core/leads/servicio.py", "sembrar_para_qa")}


def _nombre_base(nodo: ast.expr) -> str:
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return nodo.attr
    return ""


def _escrituras_de_status(arbol: ast.AST) -> list[tuple[int, str, str]]:
    """(línea, código, función que la contiene) de cada asignación a `<lead>.status`."""
    hallazgos: list[tuple[int, str, str]] = []

    def visitar(nodo: ast.AST, funcion: str) -> None:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcion = nodo.name
        objetivos: list[ast.expr] = []
        if isinstance(nodo, ast.Assign):
            objetivos = nodo.targets
        elif isinstance(nodo, (ast.AugAssign, ast.AnnAssign)):
            objetivos = [nodo.target]
        for objetivo in objetivos:
            for sub in ast.walk(objetivo):
                if isinstance(sub, ast.Attribute) and sub.attr == "status" and "lead" in _nombre_base(sub.value).lower():
                    hallazgos.append((sub.lineno, ast.unparse(sub), funcion))
        for hijo in ast.iter_child_nodes(nodo):
            visitar(hijo, funcion)

    visitar(arbol, "")
    return hallazgos


def _archivos() -> list[Path]:
    return sorted([*(RAIZ / "core").rglob("*.py"), *(RAIZ / "packages").rglob("*.py")])


def test_nadie_escribe_lead_status_fuera_de_etapas() -> None:
    archivos = _archivos()
    assert any(a.relative_to(RAIZ).as_posix() == PERMITIDO for a in archivos)
    hallazgos = []
    excepciones_vistas = set()
    for archivo in archivos:
        rel = archivo.relative_to(RAIZ).as_posix()
        if rel == PERMITIDO:
            continue
        for linea, codigo, funcion in _escrituras_de_status(ast.parse(archivo.read_text(encoding="utf-8"))):
            if (rel, funcion) in EXCEPCIONES:
                excepciones_vistas.add((rel, funcion))
                continue
            hallazgos.append(f"{rel}:{linea} {codigo} (en {funcion or 'módulo'})")
    assert hallazgos == [], "la etapa del lead se cambia solo con core.leads.etapas.cambiar_etapa"
    # La excepción sigue existiendo: si la siembra deja de escribir directo, se quita de aquí.
    assert excepciones_vistas == EXCEPCIONES


def test_la_vigilancia_detecta_las_escrituras() -> None:
    malo = ast.parse(
        "lead.status = 'x'\nfresh_lead.status += 'y'\nself.lead.status = 'z'\nquote.status = 'q'\n"
        "def f():\n    lead.status = 'w'\n"
    )
    assert [(c, f) for _, c, f in _escrituras_de_status(malo)] == [
        ("lead.status", ""), ("fresh_lead.status", ""), ("self.lead.status", ""), ("lead.status", "f"),
    ]


def test_etapas_si_escribe_y_las_cinco_coinciden_con_el_panel() -> None:
    from core.leads.catalogo import ETAPAS
    from core.leads.providers import ESTADOS_LEAD

    assert _escrituras_de_status(ast.parse((RAIZ / PERMITIDO).read_text(encoding="utf-8")))
    assert ETAPAS == tuple(v for v, _ in ESTADOS_LEAD)
