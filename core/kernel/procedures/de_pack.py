"""Procedimientos que trae un pack en su carpeta `procedures/` (HU-AOS-08, ADR-0113 §1).

Cada pack publica sus procedimientos `procedure/1` como JSON en `packages/pack-<x>/procedures/`,
junto a su `flow.json` y sus `playbooks/`, y los registra al activarse:

    host.register_procedures(de_pack.cargar(PLUGIN_ID))

La carpeta se resuelve igual que la del `flow.json` (`core/agents/flow.py`): el id del pack es el
nombre de la carpeta con guiones → guiones bajos. Orden determinista (por nombre de archivo).

Solo valida la forma del contrato: un JSON roto o inválido se loguea y se salta, y el pack sigue
vivo (mismo criterio que `MCPHost.register_procedures`). El linter contra las acciones del binario
corre en runtime (`registro.py`) y en CI (`tests/test_procedimientos_de_packs.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

from agentcore_contracts import Procedure

from core.kernel.procedures.linter import MAX_PROCEDURE_BYTES, validar
from core.shared.logger import get_logger

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CARPETA = "procedures"


def carpeta_de(pack_id: str) -> Path | None:
    """`packages/pack-<x>/procedures` del pack, o None si el pack no trae procedimientos."""
    for pattern in ("packages/pack-*", "plugins/*"):
        for folder in sorted(REPO_ROOT.glob(pattern)):
            if folder.name.replace("-", "_") == pack_id and (folder / CARPETA).is_dir():
                return folder / CARPETA
    return None


def archivos(pack_id: str) -> list[Path]:
    carpeta = carpeta_de(pack_id)
    return sorted(carpeta.glob("*.json")) if carpeta is not None else []


def cargar(pack_id: str) -> list[Procedure]:
    """Los procedimientos válidos (forma de `procedure/1`) del pack, en orden de archivo."""
    salida: list[Procedure] = []
    for ruta in archivos(pack_id):
        try:
            crudo = ruta.read_bytes()
            if len(crudo) > MAX_PROCEDURE_BYTES:
                raise ValueError("demasiado grande")
            proc, problemas = validar(json.loads(crudo))
        except (OSError, ValueError) as exc:
            log.error("procedimiento_de_pack_ilegible", pack=pack_id, archivo=ruta.name, error=type(exc).__name__)
            continue
        if proc is None:
            log.error(
                "procedimiento_de_pack_invalido", pack=pack_id, archivo=ruta.name,
                codigos=sorted({p.code for p in problemas})[:10],
            )
            continue
        salida.append(proc)
    return salida
