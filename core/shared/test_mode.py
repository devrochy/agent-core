"""Modo prueba del panel (playground).

El "Probar asistente" del panel deja al admin conversar con el asistente de su
tenant SIN pasar por ningún canal: `core/admin/playground.py` crea un `Customer`
y una `Conversation` sintéticos con `channel == TEST_CHANNEL` y llama al MISMO
loop del runtime (`process_conversation`), devolviendo la respuesta al navegador.

Ese canal es una prueba, no actividad del negocio: todo lo que mide cuotas o
lista conversaciones del cliente debe excluirlo (`core/billing/metrics.py`,
`core/orchestrator/escalation.py`, listados del panel).

La prueba del panel (`state["panel"]`) además usa el sandbox de tools de HU-ARCH-14:
no escribe en las tablas de negocio y se purga al cerrar. Los evals/journeys corren
en `channel == "test"` pero sin ese marcador: ejecutan las tools reales porque su base
también es de prueba.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from sqlalchemy import or_, select

TEST_CHANNEL = "test"

# Marca del turno: alguna tool se ejecutó en modo simulado. El loop la consume al cerrar
# el turno y el panel pinta «Acción simulada».
_simulado: ContextVar[bool] = ContextVar("turno_simulado", default=False)

def is_test_channel(channel: str | None) -> bool:
    """True si la conversación es del playground (no es atención real)."""
    return channel == TEST_CHANNEL


def is_panel_test(conversation: Any) -> bool:
    """True solo para la prueba del panel (sandbox de tools).

    El canal `test` también lo usan los evals/journeys, que necesitan ejecutar las tools de
    verdad contra una base de prueba. El panel marca su conversación con `state["panel"]`.
    """
    if conversation is None or not is_test_channel(getattr(conversation, "channel", None)):
        return False
    estado = getattr(conversation, "state", None)
    return bool(isinstance(estado, dict) and estado.get("panel"))


def marcar_simulado() -> None:
    _simulado.set(True)


def consumir_simulado() -> bool:
    """Devuelve si el turno tuvo acciones simuladas y lo reinicia."""
    valor = _simulado.get()
    _simulado.set(False)
    return valor


def reset_simulado() -> None:
    _simulado.set(False)


def conversaciones_de_prueba(tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Subconsulta con los ids de las conversaciones de la prueba del panel (no evals)."""
    from core.db.models import Conversation

    return select(Conversation.id).where(
        Conversation.tenant_id == tenant_id,
        Conversation.channel == TEST_CHANNEL,
        Conversation.state["panel"].astext == "true",
    )


def no_es_prueba(tenant_id: uuid.UUID, columna: Any):  # type: ignore[no-untyped-def]
    """Condición SQLAlchemy: la fila no pertenece a una conversación de prueba.

    Conserva las filas sin conversación (`NULL`): son actividad legítima no atribuida.
    """
    return or_(columna.is_(None), ~columna.in_(conversaciones_de_prueba(tenant_id)))


def sql_no_es_prueba(columna: str, tenant_param: str = "t") -> str:
    """Condición SQL para consultas raw. Espera el parámetro de tenant `:t` por defecto."""
    sub = (
        f"(select id from conversations where tenant_id = :{tenant_param} "
        "and channel = 'test' and state->>'panel' = 'true')"
    )
    return f"({columna} is null or {columna} not in {sub})"
