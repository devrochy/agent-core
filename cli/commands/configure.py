"""agent-core configure: reconfiguración puntual del asistente."""

from __future__ import annotations

import asyncio

import questionary
import typer
from rich.console import Console
from sqlalchemy import select

from cli.sdd_generator import append_changelog, load_state, render_all, save_state

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def tone() -> None:
    """Cambia el tono del asistente (calido | formal | divertido)."""
    selected = questionary.select("Tono:", choices=["calido", "formal", "divertido"]).ask()

    async def _update() -> None:
        from core.db.models import AppSettings
        from core.db.session import get_session

        async with get_session() as session:
            settings_row = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()
            if settings_row is None:
                console.print("[red]No hay configuración. Corre: agent-core init[/red]")
                return
            behavior = dict(settings_row.behavior_config)
            behavior["tono"] = selected
            settings_row.behavior_config = behavior

    asyncio.run(_update())
    state = load_state()
    state["tone"] = selected
    append_changelog(state, f"Tono cambiado a: {selected}")
    save_state(state)
    render_all(state)
    console.print(f"✓ Tono actualizado: [bold]{selected}[/bold]")


@app.command()
def llm() -> None:
    """Cambia el proveedor/modelo LLM (BYOK, se guarda cifrado en BD)."""
    provider = questionary.select("Proveedor:", choices=["deepseek", "openai", "anthropic"]).ask()
    defaults = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}
    model = questionary.text("Modelo:", default=defaults[provider]).ask()
    api_key = questionary.password("API Key (Enter = conservar la actual):").ask() or ""

    async def _update() -> None:
        from core.db.models import AppSettings
        from core.db.session import get_session
        from core.shared.crypto import get_secret_box

        async with get_session() as session:
            settings_row = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()
            if settings_row is None:
                console.print("[red]No hay configuración. Corre: agent-core init[/red]")
                return
            settings_row.llm_provider = provider
            settings_row.llm_model = model
            if api_key:
                settings_row.llm_api_key_encrypted = get_secret_box().encrypt(api_key)

    asyncio.run(_update())
    state = load_state()
    state.update(llm_provider=provider, llm_model=model)
    append_changelog(state, f"LLM cambiado a: {provider}/{model}")
    save_state(state)
    render_all(state)
    console.print(f"✓ LLM actualizado: [bold]{provider}/{model}[/bold]")
