"""Wizard de importación desde una BD PostgreSQL existente.

Se conecta a la BD del usuario (URL), introspecciona el schema con SQLAlchemy
y propone mapear tablas existentes a las entidades del plugin. El resultado es
un script SQL de importación (INSERT INTO ... SELECT) que el usuario REVISA
antes de ejecutar — nunca se migran datos sin revisión.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import questionary
from rich.console import Console
from sqlalchemy import create_engine, inspect

console = Console()


def run_import_wizard(plugin_id: str, entity_targets: dict[str, list[str]]) -> Path | None:
    """entity_targets: {"leads": ["name", "phone", "email", ...], "deals": [...]}

    Devuelve la ruta del script SQL generado, o None si se canceló.
    """
    source_url = questionary.text(
        "URL de la BD PostgreSQL existente (postgresql://user:pass@host:5432/db):"
    ).ask()
    if not source_url:
        return None

    try:
        engine = create_engine(source_url.replace("+asyncpg", ""))
        inspector = inspect(engine)
        tables = inspector.get_table_names()
    except Exception as exc:
        console.print(f"[red]No se pudo conectar: {exc}[/red]")
        return None

    if not tables:
        console.print("[yellow]La BD no tiene tablas.[/yellow]")
        return None

    console.print(f"\nEncontré [bold]{len(tables)}[/bold] tablas: {', '.join(tables[:15])}\n")

    statements: list[str] = [
        f"-- Importación generada por agent-core plugin install {plugin_id}",
        f"-- Fecha: {datetime.now(timezone.utc).isoformat()}",
        f"-- Origen: {source_url.split('@')[-1]}",
        "-- REVISA este script antes de ejecutarlo. Ajusta los mapeos de columnas.",
        "",
    ]

    for entity, target_columns in entity_targets.items():
        source_table = questionary.select(
            f"¿Qué tabla de tu BD corresponde a la entidad '{entity}'?",
            choices=[*tables, "(ninguna — omitir)"],
        ).ask()
        if source_table == "(ninguna — omitir)":
            continue

        source_columns = [c["name"] for c in inspector.get_columns(source_table)]
        mapping: dict[str, str] = {}
        for target_col in target_columns:
            choices = ["(omitir)"] + source_columns
            default = target_col if target_col in source_columns else "(omitir)"
            selected = questionary.select(
                f"  {entity}.{target_col} <- columna de {source_table}:",
                choices=choices,
                default=default,
            ).ask()
            if selected != "(omitir)":
                mapping[target_col] = selected

        if mapping:
            target_cols = ", ".join(mapping.keys())
            source_cols = ", ".join(mapping.values())
            statements.append(
                f"INSERT INTO {entity} ({target_cols})\nSELECT {source_cols} FROM {source_table};\n"
            )

    output = Path("plugins") / plugin_id / f"import_{datetime.now(timezone.utc):%Y%m%d_%H%M}.sql"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(statements))
    console.print(f"\n✓ Script de importación generado: [bold]{output}[/bold]")
    console.print("  Revísalo y ejecútalo con: psql $DATABASE_URL -f " + str(output))
    return output
