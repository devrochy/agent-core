"""agent-core init: wizard de creación de un nuevo asistente.

Flujo:
1. Preguntas interactivas (nombre, negocio, LLM, canal, infra).
2. Genera .env (con claves de cifrado/sesión auto-generadas).
3. Genera el SDD vivo (sdd/*.md desde plantillas).
4. Corre migraciones y crea el admin por defecto (si la BD está disponible).
"""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.panel import Panel

from cli.sdd_generator import append_changelog, render_all, save_state

console = Console()

ENV_TEMPLATE = """# Generado por `agent-core init` — {assistant_name}
APP_NAME="{assistant_name}"
APP_ENV=development
APP_SECRET_KEY={app_secret}
PUBLIC_BASE_URL=http://localhost:8000

DATABASE_URL={database_url}
REDIS_URL={redis_url}

MASTER_ENCRYPTION_KEY={fernet_key}

LLM_PROVIDER={llm_provider}
LLM_API_KEY={llm_api_key}
LLM_MODEL={llm_model}
LLM_MODEL_REASONING=deepseek-reasoner
LLM_BASE_URL=https://api.deepseek.com
LLM_MAX_TOKENS=4096
DIFFICULTY_ROUTING=true

EMBEDDING_PROVIDER={embedding_provider}
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIMENSIONS=384
OPENAI_API_KEY=

WHATSAPP_VERIFY_TOKEN={verify_token}
WHATSAPP_APP_SECRET=
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=

DEBOUNCE_MODE={debounce_mode}
MAX_AGENT_ITERATIONS=6
MESSAGE_SPLIT_STYLE=pocos_cortos

LOG_LEVEL=INFO
"""


def init_command(
    skip_db: bool = typer.Option(False, "--skip-db", help="No tocar la BD (solo .env + sdd/)"),
) -> None:
    """Wizard interactivo: crea un nuevo asistente desde el core."""
    console.print(Panel.fit("🤖 [bold]agent-core init[/bold] — nuevo asistente", border_style="cyan"))

    if Path(".env").exists():
        overwrite = questionary.confirm("Ya existe un .env. ¿Sobrescribir?", default=False).ask()
        if not overwrite:
            console.print("[yellow]Cancelado.[/yellow]")
            raise typer.Exit(1)

    # ---------------- preguntas
    assistant_name = questionary.text("Nombre del asistente:", default="Mi Asistente").ask()
    business = questionary.text("Describe el negocio (1-2 frases):").ask() or ""
    language = questionary.select("Idioma principal:", choices=["es", "en", "pt"], default="es").ask()
    tone = questionary.select("Tono del asistente:", choices=["calido", "formal", "divertido"]).ask()

    llm_provider = questionary.select(
        "Proveedor LLM:", choices=["deepseek", "openai", "anthropic"], default="deepseek"
    ).ask()
    default_models = {"deepseek": "deepseek-chat", "openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5"}
    llm_model = questionary.text("Modelo:", default=default_models[llm_provider]).ask()
    llm_api_key = questionary.password("API Key del LLM (Enter para dejar vacío):").ask() or ""

    embedding_provider = questionary.select(
        "Memoria vectorial (embeddings):",
        choices=[
            questionary.Choice("none — desactivada (puedes activarla luego)", value="none"),
            questionary.Choice("local — sentence-transformers, gratis (requiere extra pip)", value="local"),
            questionary.Choice("openai — text-embedding-3-small (requiere OPENAI_API_KEY)", value="openai"),
        ],
    ).ask()

    debounce_mode = questionary.select(
        "Velocidad de respuesta (debounce):",
        choices=["inmediato", "rapido", "natural", "pausado"],
        default="natural",
    ).ask()

    database_url = questionary.text(
        "URL de PostgreSQL:",
        default="postgresql+asyncpg://agent_core:agent_core@localhost:5432/agent_core",
    ).ask()
    redis_url = questionary.text("URL de Redis:", default="redis://localhost:6379/0").ask()

    admin_email = questionary.text("Email del admin:", default="admin@local").ask()
    admin_password = questionary.password("Contraseña del admin (Enter = generar):").ask() or ""
    if not admin_password:
        admin_password = secrets.token_urlsafe(10)
        generated_password = True
    else:
        generated_password = False

    # ---------------- .env
    from cryptography.fernet import Fernet

    env_content = ENV_TEMPLATE.format(
        assistant_name=assistant_name,
        app_secret=secrets.token_hex(32),
        database_url=database_url,
        redis_url=redis_url,
        fernet_key=Fernet.generate_key().decode(),
        llm_provider=llm_provider,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        embedding_provider=embedding_provider,
        verify_token=secrets.token_urlsafe(24),
        debounce_mode=debounce_mode,
    )
    Path(".env").write_text(env_content)
    console.print("✓ [green].env generado[/green]")

    # ---------------- SDD vivo
    state = {
        "assistant_name": assistant_name,
        "business_description": business,
        "industry": "",
        "language": language,
        "tone": tone,
        "channel": "whatsapp",
        "channel_provider": "Meta Cloud API",
        "llm_provider": llm_provider,
        "llm_model": llm_model,
        "embedding_provider": embedding_provider,
        "debounce_mode": debounce_mode,
        "installed_plugins": [],
        "entities": [],
        "changelog": [],
    }
    append_changelog(state, f"Proyecto inicializado: {assistant_name} ({llm_provider}/{llm_model})")
    save_state(state)
    for file in render_all(state):
        console.print(f"✓ [green]{file}[/green]")

    # ---------------- BD + admin
    if not skip_db:
        console.print("\nInicializando base de datos...")
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                check=True, capture_output=True, text=True,
            )
            console.print("✓ [green]Migraciones aplicadas[/green]")
            _create_admin_and_settings(admin_email, admin_password, assistant_name, business,
                                       language, tone, debounce_mode)
            console.print("✓ [green]Usuario admin y configuración creados[/green]")
        except subprocess.CalledProcessError as exc:
            console.print(f"[yellow]⚠ No se pudo migrar la BD: {exc.stderr[:200]}[/yellow]")
            console.print("[yellow]  Levanta la BD (docker-compose up -d postgres redis) y corre:")
            console.print("  agent-core db migrate && agent-core db seed[/yellow]")

    # ---------------- resumen
    lines = [
        f"[bold]{assistant_name}[/bold] listo.",
        "",
        "Siguientes pasos:",
        "  1. docker-compose up -d",
        "  2. Abre http://localhost:8000/admin",
        f"  3. Ingresa con: [bold]{admin_email}[/bold]",
    ]
    if generated_password:
        lines.append(f"     Contraseña generada: [bold red]{admin_password}[/bold red]  (guárdala)")
    lines += [
        "",
        "Para agregar capacidades:",
        "  agent-core plugin install mod-crm-engine",
    ]
    console.print(Panel("\n".join(lines), border_style="green", title="✓ init completado"))


def _create_admin_and_settings(
    email: str, password: str, assistant_name: str, business: str,
    language: str, tone: str, debounce_mode: str,
) -> None:
    import asyncio

    async def _seed() -> None:
        from sqlalchemy import select

        from core.admin.auth import hash_password
        from core.db.models import Admin, AppSettings
        from core.db.session import get_session

        async with get_session() as session:
            existing = (await session.execute(select(Admin).where(Admin.email == email))).scalar_one_or_none()
            if existing is None:
                session.add(Admin(email=email, password_hash=hash_password(password), role="master"))
            settings_row = (await session.execute(select(AppSettings).limit(1))).scalar_one_or_none()
            if settings_row is None:
                session.add(
                    AppSettings(
                        assistant_name=assistant_name,
                        business_description=business,
                        language=language,
                        behavior_config={"tono": tone, "debounce_mode": debounce_mode},
                    )
                )
            else:
                settings_row.assistant_name = assistant_name
                settings_row.business_description = business

    asyncio.run(_seed())
