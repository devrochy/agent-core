"""Event bus: un handler que falla no rompe el emit ni el log (regresión #PR-webhook)."""

from __future__ import annotations

from core.shared.events import EventBus


def test_emit_sigue_a_pese_a_handler_que_falla():
    """Un handler con excepción se reporta pero NO aborta el emit a los demás."""
    bus = EventBus()
    llamados: list[str] = []

    async def ok(**_: object) -> None:
        llamados.append("ok")

    async def explode(**_: object) -> None:
        raise RuntimeError("boom")

    bus.subscribe("x", explode)
    bus.subscribe("x", ok)

    import asyncio

    asyncio.run(bus.emit("x"))  # no debe lanzar
    assert llamados == ["ok"]


def test_emit_con_handler_sincrono_fallido_no_revienta():
    bus = EventBus()

    def boom(**_: object) -> None:
        raise RuntimeError("boom")

    bus.subscribe("y", boom)
    import asyncio

    asyncio.run(bus.emit("y"))  # no debe lanzar (el log ya no usa `event=` reservado)