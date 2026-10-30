"""HU-CRM-18 · `pack_commerce.etapas` es el único lugar que escribe la etapa del lead.

Estático (AST): ninguna asignación a `<algo-de-lead>.status` fuera de `etapas.py`. Crear un lead con
`Lead(status=...)` es alta, no cambio, y no cuenta; `quote.status`/`order.status` son de otras tablas.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACK = Path(__file__).resolve().parents[1] / "packages" / "pack-commerce" / "pack_commerce"
PERMITIDO = "etapas.py"


def _nombre_base(nodo: ast.expr) -> str:
    if isinstance(nodo, ast.Name):
        return nodo.id
    if isinstance(nodo, ast.Attribute):
        return nodo.attr
    return ""


def _escrituras_de_status(arbol: ast.AST) -> list[tuple[int, str]]:
    hallazgos: list[tuple[int, str]] = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Assign):
            objetivos = nodo.targets
        elif isinstance(nodo, (ast.AugAssign, ast.AnnAssign)):
            objetivos = [nodo.target]
        else:
            continue
        for objetivo in objetivos:
            for sub in ast.walk(objetivo):
                if isinstance(sub, ast.Attribute) and sub.attr == "status" and "lead" in _nombre_base(sub.value).lower():
                    hallazgos.append((sub.lineno, ast.unparse(sub)))
    return hallazgos


def test_nadie_escribe_lead_status_fuera_de_etapas() -> None:
    archivos = sorted(PACK.rglob("*.py"))
    assert archivos, PACK
    hallazgos = [
        f"{a.name}:{linea} {codigo}"
        for a in archivos
        if a.name != PERMITIDO
        for linea, codigo in _escrituras_de_status(ast.parse(a.read_text(encoding="utf-8")))
    ]
    assert hallazgos == [], "la etapa del lead se cambia solo con pack_commerce.etapas.cambiar_etapa"


def test_la_vigilancia_detecta_las_escrituras() -> None:
    malo = ast.parse("lead.status = 'x'\nfresh_lead.status += 'y'\nself.lead.status = 'z'\nquote.status = 'q'\n")
    assert [c for _, c in _escrituras_de_status(malo)] == ["lead.status", "fresh_lead.status", "self.lead.status"]


def test_etapas_si_escribe_y_las_cinco_coinciden_con_el_panel() -> None:
    from pack_commerce.etapas import ETAPAS
    from pack_commerce.providers import ESTADOS_LEAD

    assert _escrituras_de_status(ast.parse((PACK / PERMITIDO).read_text(encoding="utf-8")))
    assert ETAPAS == tuple(v for v, _ in ESTADOS_LEAD)
