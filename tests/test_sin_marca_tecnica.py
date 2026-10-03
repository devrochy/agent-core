"""HU-REP-04: sin marca técnica ("agent-core") en cadenas visibles al cliente.

Cubre dos superficies reales y una guardia de regresión:
1. La descripción del link de pago de plataforma (Wompi).
2. El evento de calendario en modo Google.
3. Un barrido de plantillas/playbooks de los packs: ninguna cadena enviada al
   cliente debe contener "agent-core".
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PATRON = re.compile(r"agent[ _-]?core", re.IGNORECASE)

# Superficies que SÍ ven los clientes finales (no logs, OTel, título FastAPI,
# docstrings del CLI: esos son internos y van en HU-REP-03).
SUPERFICIES_VISIBLES = [
    REPO / "packages/pack-commerce/templates",
    REPO / "packages/pack-commerce/playbooks",
    REPO / "packages/pack-calendar/templates",
    REPO / "packages/pack-calendar/playbooks",
    REPO / "packages/pack-cobranza/templates",
    REPO / "packages/pack-subscriptions/templates",
    REPO / "packages/pack-payments/templates",
]


@pytest.mark.parametrize("base", SUPERFICIES_VISIBLES)
def test_superficies_visibles_sin_marca_tecnica(base: pathlib.Path) -> None:
    if not base.exists():
        pytest.skip(f"{base} no existe")
    for path in base.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix.lower() not in {".html", ".md", ".txt", ".json", ".jinja", ".j2"}:
            continue
        texto = path.read_text(encoding="utf-8", errors="ignore")
        assert not PATRON.search(texto), (
            f"{path.relative_to(REPO)} contiene 'agent-core' en una superficie "
            "visible al cliente (HU-REP-04)"
        )


def test_wompi_no_usa_marca_tecnica() -> None:
    texto = (REPO / "core/billing/providers/wompi.py").read_text(encoding="utf-8")
    # La descripción del link se arma; "agent-core" no debe aparecer como texto literal.
    assert 'f"Plan agent-core' not in texto
    assert "SolversHub" in texto


def test_calendar_no_usa_marca_tecnica() -> None:
    texto = (REPO / "packages/pack-calendar/pack_calendar/skills.py").read_text(encoding="utf-8")
    assert "[agent-core conversación" not in texto
    assert "_business_name" in texto
