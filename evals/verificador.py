"""Falsos positivos y negativos del verificador de afirmaciones (HU-ARCH-29, ADR-0119).

Corre el verificador del core sobre un set etiquetado de borradores reales del
asistente, cada uno con los resultados de tools de su turno, y compara:

- **falso positivo**: una respuesta correcta que el verificador rechaza (pediría un
  reintento y, si reincide, escalaría una venta válida);
- **falso negativo**: una afirmación falsa que el verificador deja pasar.

Umbral de la HU: falsos positivos ≤ 2 % y ningún falso negativo. Sin LLM, sin base
y sin red: corre en la suite unitaria (`tests/test_verificador_set.py`).

Uso:
  python evals/verificador.py                 # set del framework
  python evals/verificador.py --detalle       # imprime cada desacuerdo
  python evals/verificador.py --set <ruta>    # otro set (p. ej. el de un negocio,
                                              # en TENANTS_ROOT/<slug>/evals/)

Formato del set (JSON Lines): `id`, `origen`, `texto` (el borrador), `resultados`
(los de tools del turno, con `_tool_name`, `_ok` y `_current_turn` como los estampa
el loop), `contexto` (opcional: prompt y mensajes del cliente), `etiqueta`
(`correcta` o `falsa`) y `nota`.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SET_FRAMEWORK = Path(__file__).parent / "verificador" / "respuestas_etiquetadas.jsonl"
UMBRAL_FALSOS_POSITIVOS = 0.02


@dataclass
class Medicion:
    correctas: int = 0
    falsas: int = 0
    falsos_positivos: list[dict[str, Any]] = field(default_factory=list)
    falsos_negativos: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tasa_falsos_positivos(self) -> float:
        return len(self.falsos_positivos) / self.correctas if self.correctas else 0.0

    @property
    def cumple(self) -> bool:
        return self.tasa_falsos_positivos <= UMBRAL_FALSOS_POSITIVOS and not self.falsos_negativos


def afirmaciones_incorporadas() -> list[Any]:
    """Las afirmaciones de cierre que traen los packs, sin la config de ningún negocio."""
    from pack_commerce.module import _LINK_CON_URL, COMPLETION_CLAIMS

    from core.mcp.types import Afirmacion

    return [
        Afirmacion(label=label, patron=patron, tools=frozenset(tools), basta_url=label in _LINK_CON_URL)
        for label, (patron, tools) in COMPLETION_CLAIMS.items()
    ]


def cargar(ruta: Path = SET_FRAMEWORK) -> list[dict[str, Any]]:
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines() if linea.strip()]


def medir(casos: list[dict[str, Any]]) -> Medicion:
    from core.orchestrator.verificador import Evidencia, verificar

    afirmaciones = afirmaciones_incorporadas()
    medicion = Medicion()
    for caso in casos:
        evidencia = Evidencia.desde(caso.get("resultados") or [], caso.get("contexto") or "")
        veredicto = verificar(caso["texto"], evidencia, afirmaciones)
        detalle = {"id": caso["id"], "motivo": veredicto.motivo, "afirmacion": veredicto.afirmacion}
        if caso["etiqueta"] == "correcta":
            medicion.correctas += 1
            if not veredicto.passed:
                medicion.falsos_positivos.append(detalle)
        else:
            medicion.falsas += 1
            if veredicto.passed:
                medicion.falsos_negativos.append(detalle)
    return medicion


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", default=str(SET_FRAMEWORK), help="Ruta del set etiquetado (JSON Lines)")
    parser.add_argument("--detalle", action="store_true", help="Imprimir cada desacuerdo")
    args = parser.parse_args()

    medicion = medir(cargar(Path(args.set)))
    print(
        f"Verificador: {medicion.correctas} correctas · {medicion.falsas} falsas · "
        f"falsos positivos {len(medicion.falsos_positivos)} ({medicion.tasa_falsos_positivos:.1%}, "
        f"umbral {UMBRAL_FALSOS_POSITIVOS:.0%}) · falsos negativos {len(medicion.falsos_negativos)}"
    )
    if args.detalle:
        for caso in medicion.falsos_positivos:
            print(f"  FP {caso}")
        for caso in medicion.falsos_negativos:
            print(f"  FN {caso}")
    return 0 if medicion.cumple else 1


if __name__ == "__main__":
    sys.exit(main())
