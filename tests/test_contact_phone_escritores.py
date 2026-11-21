"""Quién escribe `Customer.contact_phone` (adenda de ADR-0179 §7, HU-CONN-26).

El enlace perezoso de cobranza entrega los datos de cartera a un `Customer` de WhatsApp o Telegram cuyo
`contact_phone` cruza con el de un deudor. Eso solo es seguro si ese número lo verificó el canal: el `wa_id` de
WhatsApp o el `request_contact` propio de Telegram (ADR-0199 §8), ambos en el consumer. El intake web de commerce
también lo escribe, pero solo en `channel == "web"`, que el enlace no acepta. Un escritor nuevo rompe este test a
propósito: hay que revisar si abre un número sin verificar en un canal que enlaza.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
# Asignación o kwarg, `setattr(x, "contact_phone", …)` y SQL crudo que lo pone en un SET o en las columnas de un INSERT.
_ESCRITURA = re.compile(
    r"contact_phone\s*=(?!=)"
    r"|setattr\([^)]*[\"']contact_phone[\"']"
    r"|\bSET\b[^;\"']*\bcontact_phone\b"
    r"|INSERT\s+INTO\s+customers\s*\([^)]*\bcontact_phone\b"
)

# archivo → cuántas escrituras se revisaron.
ESCRITORES_REVISADOS = {
    "core/orchestrator/consumer.py": 4,  # alta del Customer (wa_id) y teléfono compartido en Telegram
    "core/leads/intake.py": 2,  # intake web, `channel == "web"`
}


def _escrituras() -> Counter[str]:
    encontradas: Counter[str] = Counter()
    for carpeta in ("core", "packages", "plugins", "cli", "scripts", "evals"):
        for ruta in (RAIZ / carpeta).rglob("*.py"):
            if not ruta.is_file() or "tests" in ruta.parts or "alembic" in ruta.parts:
                continue
            n = len(_ESCRITURA.findall(ruta.read_text(encoding="utf-8")))
            if n:
                encontradas[ruta.relative_to(RAIZ).as_posix()] = n
    return encontradas


def test_solo_los_escritores_revisados_escriben_contact_phone() -> None:
    assert dict(_escrituras()) == ESCRITORES_REVISADOS
