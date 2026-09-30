"""Modo prueba del panel (playground).

El "Probar asistente" del panel deja al admin conversar con el asistente de su
tenant SIN pasar por ningún canal: `core/admin/playground.py` crea un `Customer`
y una `Conversation` sintéticos con `channel == TEST_CHANNEL` y llama al MISMO
loop del runtime (`process_conversation`), devolviendo la respuesta al navegador.

Ese canal es una prueba, no actividad del negocio: todo lo que mide cuotas o
lista conversaciones del cliente debe excluirlo (`core/billing/metrics.py`,
`core/orchestrator/escalation.py`, listados del panel).
"""

from __future__ import annotations

TEST_CHANNEL = "test"


def is_test_channel(channel: str | None) -> bool:
    """True si la conversación es del playground (no es atención real)."""
    return channel == TEST_CHANNEL
