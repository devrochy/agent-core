"""Afirmaciones de un procedimiento compiladas a `Afirmacion` (ADR-0113 §1, ADR-0119).

Hay un solo verificador (`core/orchestrator/verificador/`) y una sola API
(`MCPHost.register_afirmaciones`). Un procedimiento no trae un verificador propio: sus
`claims` se compilan a la misma `Afirmacion` que hoy registran los packs.

- `status` (y `link` con frases): frases literales, siempre escapadas (`re.escape`,
  ADR-0025; nunca regex crudo), respaldadas por las acciones de su `requires` y su
  `mustMatch`. Un `link` con frases también lo respalda cualquier URL de una acción del
  turno (`basta_url`), igual que el `link_de_pago` de pack-commerce.
- `amount` y `link` sin frases no generan `Afirmacion`: ya los cubren las comprobaciones
  genéricas del verificador (montos contra los montos del servidor del turno, links contra
  las URLs de las acciones). El linter solo exige que declaren su fuente. Hoy el
  verificador compara contra cualquier monto que venga del servidor, no contra esa fuente
  en particular.

No van por `register_afirmaciones` (que no sabe de la bandera): con el motor activo, el loop suma
las de los procedimientos disponibles del turno (`ContextoMotor.afirmaciones`, HU-AOS-07). Con la
bandera apagada no se verifica nada nuevo. Las de los packs (`COMPLETION_CLAIMS` de commerce)
siguen como siempre.
"""

from __future__ import annotations

import re

from agentcore_contracts import Claim, Procedure

from core.mcp.types import Afirmacion


def accion_de(fuente: str) -> str | None:
    """`action:crear_pedido.total` → `crear_pedido`; otra fuente → None."""
    if not fuente.startswith("action:"):
        return None
    return fuente.removeprefix("action:").split(".", 1)[0]


def acciones_que_respaldan(proc: Procedure, fuente: str) -> set[str]:
    """Acciones del servidor detrás de una fuente: la propia (`action:`) o la que llena
    el dato (`slot:` con `setBy: action:`). Un dato que llena el cliente no respalda nada."""
    accion = accion_de(fuente)
    if accion is not None:
        return {accion}
    if fuente.startswith("slot:"):
        slot = proc.slots.get(fuente.removeprefix("slot:"))
        if slot is not None and slot.setBy is not None:
            return {a} if (a := accion_de(slot.setBy)) else set()
    return set()


def respaldo_de(proc: Procedure, claim: Claim) -> set[str]:
    """Acciones que respaldan una afirmación (`requires` y `mustMatch`)."""
    fuentes = [*claim.mustMatch, *([claim.requires] if claim.requires else [])]
    acciones: set[str] = set()
    for fuente in fuentes:
        acciones |= acciones_que_respaldan(proc, fuente)
    return acciones


def compilar_afirmaciones(proc: Procedure) -> list[Afirmacion]:
    """`Afirmacion` de cada `claim` con frases. La etiqueta es `<id>:<índice>`."""
    afirmaciones: list[Afirmacion] = []
    for indice, claim in enumerate(proc.claims):
        if claim.kind == "amount" or not claim.phrases:
            continue
        patron = re.compile("|".join(re.escape(frase) for frase in claim.phrases), re.IGNORECASE)
        afirmaciones.append(
            Afirmacion(
                label=f"{proc.id}:{indice}",
                patron=patron,
                tools=frozenset(respaldo_de(proc, claim)),
                basta_url=claim.kind == "link",
            )
        )
    return afirmaciones
