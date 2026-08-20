"""agent-core init: wizard de creación de un nuevo asistente.

Flujo:
1. Preguntas interactivas (nombre, negocio, LLM, canal, infra).
2. Detecta automáticamente puertos disponibles (evita colisiones con otros proyectos).
3. Genera .env con credenciales extraídas + variables de docker-compose.
4. Genera el SDD vivo (sdd/*.md desde plantillas).
5. Levanta la BD con docker-compose y corre migraciones.
"""

from __future__ import annotations

import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

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
PUBLIC_BASE_URL=http://localhost:{app_host_port}

# Base de datos (para migraciones desde el host local)
DATABASE_URL={database_url}
REDIS_URL={redis_url}

# Credenciales postgres (usadas por docker-compose)
POSTGRES_USER={pg_user}
POSTGRES_PASSWORD={pg_password}
POSTGRES_DB={pg_db}
POSTGRES_HOST_PORT={pg_host_port}
REDIS_HOST_PORT={redis_host_port}
APP_HOST_PORT={app_host_port}

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


# ------------------------------------------------------------------ puerto
def _port_available(port: int) -> bool:
    """Verifica si un puerto TCP está libre en localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _find_free_port(start: int, label: str) -> int:
    """Busca el primer puerto libre >= start."""
    for port in range(start, start + 50):
        if _port_available(port):
            return port
    raise RuntimeError(f"No hay puerto disponible para {label} desde {start}")


def _parse_db_url(url: str) -> dict[str, str]:
    """Extrae user, password, host, port, dbname del DATABASE_URL."""
    parsed = urlparse(url)
    return {
        "user": parsed.username or "agent_core",
        "password": parsed.password or "agent_core",
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 5432),
        "db": parsed.path.lstrip("/") or "agent_core",
    }


def _parse_redis_url(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    return {
        "host": parsed.hostname or "localhost",
        "port": str(parsed.port or 6379),
    }


def _update_url_port(url: str, new_port: int) -> str:
    """Reemplaza el puerto en una URL manteniendo el resto intacto."""
    return re.sub(r"(://[^/]+):(\d+)", lambda m: f"{m.group(1)}:{new_port}", url)


# ------------------------------------------------------------------ comando
def init_command(
    skip_db: bool = typer.Option(False, "--skip-db", help="No tocar la BD (solo .env + sdd/)"),
    skip_docker: bool = typer.Option(False, "--skip-docker", help="No levantar docker-compose"),
) -> None:
    """Wizard interactivo: crea un nuevo asistente desde el core."""
    console.print(Panel.fit("🤖 [bold]agent-core init[/bold] — nuevo asistente", border_style="cyan"))

    if Path(".env").exists():
        overwrite = questionary.confirm("Ya existe un .env. ¿Sobrescribir?", default=False).ask()
        if not overwrite:
            console.print("[yellow]Cancelado.[/yellow]")
            raise typer.Exit(1)

    # ---- preguntas del negocio
    assistant_name = questionary.text("Nombre del asistente:", default="Mi Asistente").ask()
    business = questionary.text("Describe el negocio (1-2 frases):").ask() or ""
    language = questionary.select("Idioma principal:", choices=["es", "en", "pt"], default="es").ask()
    tone = questionary.select("Tono del asistente:", choices=["calido", "formal", "divertido"]).ask()

    # ---- LLM
    llm_provider = questionary.select(
        "Proveedor LLM:", choices=["deepseek", "openai", "anthropic"], default="deepseek"
    ).ask()
    default_models = {
        "deepseek": "deepseek-chat",
        "openai": "gpt-4o-mini",
        "anthropic": "claude-haiku-4-5",
    }
    llm_model = questionary.text("Modelo:", default=default_models[llm_provider]).ask()
    llm_api_key = questionary.password("API Key del LLM (Enter para dejar vacío):").ask() or ""

    # ---- embeddings
    embedding_provider = questionary.select(
        "Memoria vectorial (embeddings):",
        choices=[
            questionary.Choice("none — desactivada (puedes activarla luego)", value="none"),
            questionary.Choice("local — sentence-transformers, gratis (requiere extra pip)", value="local"),
            questionary.Choice("openai — text-embedding-3-small (requiere OPENAI_API_KEY)", value="openai"),
        ],
    ).ask()

    # ---- comportamiento
    debounce_mode = questionary.select(
        "Velocidad de respuesta (debounce):",
        choices=["inmediato", "rapido", "natural", "pausado"],
        default="natural",
    ).ask()

    # ---- conexiones (el usuario escribe su URL; nosotros detectamos el puerto)
    database_url = questionary.text(
        "URL de PostgreSQL:",
        default="postgresql+asyncpg://agent_core:agent_core@localhost:5432/agent_core",
    ).ask()
    redis_url = questionary.text(
        "URL de Redis:", default="redis://localhost:6379/0"
    ).ask()

    # ---- admin
    admin_email = questionary.text("Email del admin:", default="admin@local").ask()
    admin_password = questionary.password("Contraseña del admin (Enter = generar):").ask() or ""
    generated_password = not admin_password
    if not admin_password:
        admin_password = secrets.token_urlsafe(10)

    # ---- detección automática de puertos disponibles
    console.print("\n[dim]Detectando puertos disponibles...[/dim]")
    db_info = _parse_db_url(database_url)
    redis_info = _parse_redis_url(redis_url)

    pg_host_port = _find_free_port(int(db_info["port"]), "PostgreSQL")
    redis_host_port = _find_free_port(int(redis_info["port"]), "Redis")
    app_host_port = _find_free_port(8000, "API")

    # Ajustar DATABASE_URL si el puerto libre es distinto al pedido
    if str(pg_host_port) != db_info["port"]:
        new_db_url = _update_url_port(database_url, pg_host_port)
        console.print(
            f"[yellow]  Puerto {db_info['port']} ocupado → PostgreSQL usará [bold]{pg_host_port}[/bold][/yellow]"
        )
        database_url = new_db_url

    if str(redis_host_port) != redis_info["port"]:
        new_redis_url = _update_url_port(redis_url, redis_host_port)
        console.print(
            f"[yellow]  Puerto {redis_info['port']} ocupado → Redis usará [bold]{redis_host_port}[/bold][/yellow]"
        )
        redis_url = new_redis_url

    if app_host_port != 8000:
        console.print(
            f"[yellow]  Puerto 8000 ocupado → API usará [bold]{app_host_port}[/bold][/yellow]"
        )

    console.print(
        f"✓ Puertos: Postgres=[bold]{pg_host_port}[/bold] "
        f"Redis=[bold]{redis_host_port}[/bold] "
        f"API=[bold]{app_host_port}[/bold]"
    )

    # ---- .env
    from cryptography.fernet import Fernet

    # Re-parsear con el URL ya ajustado para obtener las credenciales finales
    db_info = _parse_db_url(database_url)

    env_content = ENV_TEMPLATE.format(
        assistant_name=assistant_name,
        app_secret=secrets.token_hex(32),
        database_url=database_url,
        redis_url=redis_url,
        pg_user=db_info["user"],
        pg_password=db_info["password"],
        pg_db=db_info["db"],
        pg_host_port=pg_host_port,
        redis_host_port=redis_host_port,
        app_host_port=app_host_port,
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

    # ---- SDD vivo
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

    # ---- Levantar BD con docker-compose
    if not skip_db and not skip_docker:
        console.print("\n[dim]Levantando PostgreSQL y Redis con docker-compose...[/dim]")
        try:
            # Levantar solo postgres y redis
            subprocess.run(
                ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"],
                check=True, capture_output=True, text=True,
            )
            console.print("✓ [green]PostgreSQL y Redis levantados[/green]")

            # Migraciones del core
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                check=True, capture_output=True, text=True,
            )
            console.print("✓ [green]Migraciones aplicadas[/green]")

            # Admin + configuración base
            _create_admin_and_settings(
                admin_email, admin_password, assistant_name, business, language, tone, debounce_mode
            )
            console.print("✓ [green]Usuario admin y configuración creados[/green]")

        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or exc.stdout or "")[:300]
            console.print("[yellow]⚠ Error al levantar la BD:[/yellow]")
            console.print(f"[yellow]{err}[/yellow]")
            console.print("[yellow]Corre manualmente:[/yellow]")
            console.print("[yellow]  docker compose up -d postgres redis[/yellow]")
            console.print("[yellow]  agent-core db migrate[/yellow]")
            console.print("[yellow]  agent-core db seed[/yellow]")

    elif skip_docker and not skip_db:
        # BD ya corriendo externamente, solo migrar
        console.print("\n[dim]Aplicando migraciones sobre BD existente...[/dim]")
        try:
            subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                check=True, capture_output=True, text=True,
            )
            _create_admin_and_settings(
                admin_email, admin_password, assistant_name, business, language, tone, debounce_mode
            )
            console.print("✓ [green]BD lista[/green]")
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or "")[:300]
            console.print(f"[yellow]⚠ No se pudo migrar: {err}[/yellow]")

    # ---- resumen final
    lines = [
        f"[bold]{assistant_name}[/bold] listo.",
        "",
        "Siguientes pasos:",
        "  1. docker compose up -d  (o ya está corriendo)",
        f"  2. Abre http://localhost:{app_host_port}/admin",
        f"  3. Ingresa con: [bold]{admin_email}[/bold]",
    ]
    if generated_password:
        lines.append(
            f"     Contraseña generada: [bold red]{admin_password}[/bold red]  (guárdala)"
        )
    lines += [
        "",
        "Para agregar capacidades:",
        "  agent-core plugin install mod-crm-engine",
    ]
    console.print(Panel("\n".join(lines), border_style="green", title="✓ init completado"))


def _create_admin_and_settings(
    email: str,
    password: str,
    assistant_name: str,
    business: str,
    language: str,
    tone: str,
    debounce_mode: str,
) -> None:
    import asyncio

    async def _seed() -> None:
        from sqlalchemy import select

        from core.admin.auth import hash_password
        from core.db.models import Admin, AppSettings
        from core.db.session import get_session

        async with get_session() as session:
            existing = (
                await session.execute(select(Admin).where(Admin.email == email))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    Admin(email=email, password_hash=hash_password(password), role="master")
                )
            settings_row = (
                await session.execute(select(AppSettings).limit(1))
            ).scalar_one_or_none()
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
