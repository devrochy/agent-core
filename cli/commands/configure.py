"""agent-core configure: reconfiguración puntual del asistente."""

from __future__ import annotations

import asyncio

import questionary
import typer
from rich.console import Console
from sqlalchemy import select

from cli.sdd_generator import append_changelog, load_state, render_all, save_state
from cli.tenant_ref import resolve_tenant

app = typer.Typer(no_args_is_help=True)
console = Console()


TONOS_PERSONA = ["formal", "neutral", "cercano", "informal"]


async def cambiar_tono_persona(tid, tono: str) -> bool:
    """Escribe `persona.tone` en los valores por defecto de los agentes del negocio (`app_settings.agent_defaults`,
    ADR-0206 §6); el resto de la persona no cambia.

    Devuelve False si el negocio no tiene `AppSettings` (no pasó por `agent-core init`)."""
    from core.agents import defaults_negocio
    from core.db.models import AppSettings
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid), get_session() as session:
        settings_row = (
            await session.execute(
                select(AppSettings)
                .where(AppSettings.tenant_id == tid, AppSettings.deleted_at.is_(None))
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if settings_row is None:
            return False
        defaults_negocio.escribir(settings_row, {"persona": {"tone": tono}})
    return True


@app.command()
def tone(tenant: str = typer.Argument(..., help="Tenant (slug o UUID)")) -> None:
    """Cambia el tono de la persona del asistente (formal | neutral | cercano | informal).

    Escribe `persona.tone` de los valores por defecto de los agentes (`agent_defaults`, ADR-0206 §6): siembra la voz
    del Coordinador y de los agentes nuevos. La clave vieja `behavior_config["tono"]` ya no se escribe (HU-CFG-01).
    """
    selected = questionary.select("Tono:", choices=TONOS_PERSONA).ask()

    async def _run() -> bool:
        return await cambiar_tono_persona(await resolve_tenant(tenant), selected)

    if not asyncio.run(_run()):
        console.print("[red]No hay configuración. Corre: agent-core init[/red]")
        raise typer.Exit(1)
    state = load_state()
    state["tone"] = selected
    append_changelog(state, f"Tono cambiado a: {selected}")
    save_state(state)
    render_all(state)
    console.print(f"✓ Tono actualizado: [bold]{selected}[/bold]")


@app.command()
def llm(tenant: str = typer.Argument(..., help="Tenant (slug o UUID)")) -> None:
    """Cambia el proveedor/modelo LLM de un tenant (BYOK, se guarda cifrado en BD)."""
    provider = questionary.select("Proveedor:", choices=["deepseek", "openai", "anthropic"]).ask()
    defaults = {"deepseek": "deepseek-flash", "openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}
    model = questionary.text("Modelo:", default=defaults[provider]).ask()
    api_key = questionary.password("API Key (Enter = conservar la actual):").ask() or ""

    async def _update() -> None:
        from core.db.models import AppSettings
        from core.db.session import get_session
        from core.shared.crypto import get_secret_box
        from core.shared.tenant import tenant_scope

        tid = await resolve_tenant(tenant)
        async with tenant_scope(tid), get_session() as session:
            settings_row = (
                await session.execute(
                    select(AppSettings).where(AppSettings.tenant_id == tid, AppSettings.deleted_at.is_(None)).limit(1)
                )
            ).scalar_one_or_none()
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
