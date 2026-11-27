"""Cliente simulado de las pruebas del agente y de los evals de consistencia (HU-ARCH-28, ADR-0119; ADR-0212 §3).

Un LLM hace de cliente con un objetivo, los datos que conoce, un estilo y una semilla. Vive en `core/` (y no en
`evals/`) porque `pyproject.toml` empaqueta solo `core*`: el corredor de las pruebas del dueño corre en el worker.
`evals/simulador.py` lo reexporta para el gate.

- Las instrucciones viven en el playbook del core `cliente_simulado` (invariante 3). La semilla elige una variante de
  comportamiento de forma determinista, para que los k intentos de un escenario no sean todos iguales.
- El objetivo y los datos son del dueño (o de la IA que propuso el escenario): entran como **dato** en un bloque JSON
  aparte (`DATOS_DEL_CLIENTE`), nunca mezclados con las instrucciones (invariante 7).
- El simulador no tiene tope de turnos: lo aplica quien lo corre (`core.pruebas.corredor`, 12).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

PLAYBOOK = "cliente_simulado"
CUMPLIDO = "[OBJETIVO_CUMPLIDO]"
RINDE = "[ME_RINDO]"
BLOQUE_DATOS = "DATOS_DEL_CLIENTE"


def _cargar_plantilla() -> tuple[str, list[str], str]:
    """(sistema, variantes, primer turno) desde el playbook `cliente_simulado`."""
    from core.agents.playbooks import load_core_playbook

    sistema, _, resto = load_core_playbook(PLAYBOOK).partition("---VARIANTES---")
    variantes, _, inicio = resto.partition("---INICIO---")
    return sistema.strip(), [v.strip() for v in variantes.splitlines() if v.strip()], inicio.strip()


@dataclass
class ClienteSimulado:
    objetivo: str
    datos: dict[str, Any]
    estilo: str = "cordial y breve"
    semilla: int = 0
    turnos: list[dict[str, str]] = field(default_factory=list)
    # Lo medido de la última llamada al proveedor, para quien cobra (`core.pruebas.corredor`). El simulador no escribe
    # en el ledger: llama al proveedor directo.
    ultima_respuesta: Any = field(default=None, repr=False)

    @property
    def variante(self) -> str:
        _, variantes, _ = _cargar_plantilla()
        return random.Random(self.semilla).choice(variantes) if variantes else ""

    def _sistema(self) -> list[str]:
        """Instrucciones (el playbook con estilo y variante, que son de la plataforma) y, aparte, el dato."""
        sistema, _, _ = _cargar_plantilla()
        instrucciones = sistema.replace("{estilo}", self.estilo).replace("{variante}", self.variante)
        datos = json.dumps({"objetivo": self.objetivo, "datos": self.datos}, ensure_ascii=False, default=str)
        return [instrucciones, f"{BLOQUE_DATOS}:\n{datos}"]

    async def siguiente(self, provider: Any) -> str:
        """Próximo mensaje del cliente, o `CUMPLIDO` / `RINDE` si termina."""
        from core.llm.types import LLMMessage

        _, _, inicio = _cargar_plantilla()
        # Para el simulador, el asistente del negocio es el "user" y él es el "assistant".
        mensajes = [LLMMessage(role="user", content=inicio)]
        for turno in self.turnos:
            rol = "assistant" if turno["quien"] == "cliente" else "user"
            mensajes.append(LLMMessage(role=rol, content=turno["texto"]))
        respuesta = await provider.generate(
            system_blocks=self._sistema(), messages=mensajes, max_tokens=300, temperature=0.7
        )
        self.ultima_respuesta = respuesta
        texto = (respuesta.content or "").strip()
        if CUMPLIDO in texto:
            return CUMPLIDO
        if RINDE in texto:
            return RINDE
        return texto

    def anotar(self, quien: str, texto: str) -> None:
        self.turnos.append({"quien": quien, "texto": texto})

    def traza(self, fin: str) -> dict[str, Any]:
        return {
            "semilla": self.semilla,
            "variante": self.variante,
            "fin": fin,
            "turnos": len([t for t in self.turnos if t["quien"] == "cliente"]),
            "conversacion": self.turnos,
        }


__all__ = ["BLOQUE_DATOS", "CUMPLIDO", "PLAYBOOK", "RINDE", "ClienteSimulado"]
