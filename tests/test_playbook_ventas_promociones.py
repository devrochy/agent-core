"""El playbook `ventas` fija cuándo se habla de promociones (pedido del dueño, 2026-11-07).

Solo se mencionan si `aplicar_promocion` devolvió `applied: true`; si no hay ninguna vigente, el
agente no saca el tema ni dice que no existen. La regla vive en el playbook (invariante 3), no en
el texto que devuelven las tools.
"""

from __future__ import annotations

from pathlib import Path

VENTAS = Path(__file__).resolve().parents[1] / "packages" / "pack-commerce" / "playbooks" / "ventas.md"


def _regla() -> str:
    texto = VENTAS.read_text(encoding="utf-8")
    inicio = texto.index("- Solo hablas de promociones")
    return " ".join(texto[inicio : texto.index("\n- ", inicio + 1)].split())


def test_solo_habla_de_promociones_si_hay_una_aplicada() -> None:
    regla = _regla()
    assert "`applied: true`" in regla
    assert "NO saques el tema" in regla


def test_nunca_anuncia_que_no_hay_promociones() -> None:
    regla = _regla()
    assert "NUNCA digas que no hay promociones" in regla
    assert "sin enumerar lo que no existe" in regla


def test_las_tools_de_comercio_no_traen_la_frase_de_ausencia() -> None:
    """Una frase de ausencia en el resultado de la tool invita al modelo a repetirla."""
    skills = VENTAS.parents[1] / "pack_commerce" / "skills.py"
    fuente = skills.read_text(encoding="utf-8")
    assert "No hay promociones" not in fuente
    assert "No aplicó ninguna promoción" not in fuente
