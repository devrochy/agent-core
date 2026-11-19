"""Cliente simulado para los evals de consistencia (HU-ARCH-28, ADR-0119).

Un LLM hace de cliente con un objetivo, los datos que conoce, un estilo y una semilla.
El prompt vive en `evals/simulador/cliente.md` (no en el código, invariante 3). La semilla
elige una variante de comportamiento de forma determinista, para que los k intentos de un
escenario no sean todos iguales y la inestabilidad aparezca.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLANTILLA = Path(__file__).resolve().parent / "simulador" / "cliente.md"
CUMPLIDO = "[OBJETIVO_CUMPLIDO]"
RINDE = "[ME_RINDO]"

def _cargar_plantilla() -> tuple[str, list[str], str]:
    """(sistema, variantes, primer turno) desde `evals/simulador/cliente.md`."""
    texto = PLANTILLA.read_text(encoding="utf-8")
    if "-->" in texto:  # quita el comentario de cabecera
        texto = texto.split("-->", 1)[1]
    sistema, _, resto = texto.partition("---VARIANTES---")
    variantes, _, inicio = resto.partition("---INICIO---")
    return sistema.strip(), [v.strip() for v in variantes.splitlines() if v.strip()], inicio.strip()


@dataclass
class ClienteSimulado:
    objetivo: str
    datos: dict[str, Any]
    estilo: str = "cordial y breve"
    semilla: int = 0
    turnos: list[dict[str, str]] = field(default_factory=list)

    @property
    def variante(self) -> str:
        _, variantes, _ = _cargar_plantilla()
        return random.Random(self.semilla).choice(variantes) if variantes else ""

    def _sistema(self) -> str:
        sistema, _, _ = _cargar_plantilla()
        datos = "\n".join(f"- {k}: {v}" for k, v in self.datos.items()) or "- (nada)"
        return sistema.format(objetivo=self.objetivo, datos=datos, estilo=self.estilo, variante=self.variante)

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
            system_blocks=[self._sistema()], messages=mensajes, max_tokens=300, temperature=0.7
        )
        texto = (respuesta.content or "").strip()
        if CUMPLIDO in texto:
            return CUMPLIDO
        if RINDE in texto:
            return RINDE
        return texto

    def anotar(self, quien: str, texto: str) -> None:
        self.turnos.append({"quien": quien, "texto": texto})

    def traza(self, fin: str) -> dict[str, Any]:
        return {"semilla": self.semilla, "variante": self.variante, "fin": fin, "turnos": len([t for t in self.turnos if t["quien"] == "cliente"]), "conversacion": self.turnos}
