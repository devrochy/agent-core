"""Exporta el catálogo público de planes a un JSON versionado (HU-BILL-28).

Uso:
    python scripts/export_planes.py [salida]

Por defecto escribe `docs/planes.json` (commiteado: la landing lo copia con
`scripts/sync-planes.mjs` para no repetir precios a mano). Solo importa
`core.billing.plans`: no necesita BD, Redis ni los packs.

La salida es determinista (claves ordenadas, `\\n` final): el CI la regenera y
falla si difiere de la commiteada.

No incluye «≈ conversaciones»: es una estimación que deriva la landing desde
`monthly_credits` hasta que HU-BILL-29 la calibre.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SALIDA = ROOT / "docs" / "planes.json"
VERSION = 3



def _plan(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "price_cop": plan.price_cop,
        "monthly_credits": plan.monthly_credits,
        "trial_days": plan.trial_days,
        "currency": plan.currency,
        "limits": dict(plan.limits),
        "features": dict(plan.features),
    }


def build() -> dict[str, Any]:
    from core.billing.plans import public_plans

    return {
        "version": VERSION,
        "fuente": "core/billing/plans.py",
        "planes": [_plan(p) for p in public_plans()],
    }


def render() -> str:
    return json.dumps(build(), indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else SALIDA
    texto = render()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(texto, encoding="utf-8")
    print(f"✓ {out} · {len(json.loads(texto)['planes'])} planes")


if __name__ == "__main__":
    main()
