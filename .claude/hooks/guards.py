#!/usr/bin/env python3
"""Guardia de comandos peligrosos y de migraciones sin RLS/tenant_id.

Hook PreToolUse de Claude Code — port de `.opencode/plugins/guards.ts`
(el plugin de OpenCode, ya no leído por esta herramienta). Recibe el input
del hook como JSON por stdin y devuelve una decisión de permiso por stdout.

No bloquea el turno entero: solo deniega la tool call puntual (mismo efecto
que el `block()` que lanzaba una excepción en el plugin original).
"""

from __future__ import annotations

import json
import sys

PROD_PATTERNS = ("PROD_DATABASE_URL", "--force", "drop database", "truncate ")


def deny(message: str) -> None:
    print(
        json.dumps(
            {
                "systemMessage": message,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": message,
                },
            }
        )
    )
    sys.exit(0)


def check_bash(tool_input: dict) -> None:
    command = str(tool_input.get("command", ""))
    command_lower = command.lower()
    for pattern in PROD_PATTERNS:
        if pattern.lower() in command_lower:
            deny(
                f"BLOQUEADO: el comando contiene '{pattern}'.\n"
                "Las operaciones sobre producción se ejecutan a mano, nunca desde una sesión."
            )


def check_migration_write(tool_input: dict) -> None:
    file_path = str(tool_input.get("file_path", ""))
    path_lower = file_path.lower()
    if not (path_lower.endswith(".sql") and "migrations" in path_lower):
        return
    if path_lower.endswith(".down.sql"):
        return

    # El contenido nuevo puede venir en distintas claves según la tool
    # (Write: content; Edit: new_string). Se concatena todo el input para
    # no depender de cuál se use — mismo criterio laxo que el JSON.stringify
    # del plugin original.
    content = json.dumps(tool_input).lower()

    if "enable row level security" not in content:
        deny(
            f"BLOQUEADO: la migración {file_path} no habilita RLS.\n"
            "Toda tabla de negocio requiere: alter table <t> enable row level security;\n"
            "más una policy de aislamiento por tenant_id. Ver .claude/skills/db-migration."
        )
    if "tenant_id" not in content:
        deny(
            f"BLOQUEADO: la migración {file_path} no menciona tenant_id.\n"
            "Si es intencional (tabla de sistema), documéntalo en un ADR y usa .down.sql."
        )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool_name == "Bash":
        check_bash(tool_input)
    elif tool_name in ("Write", "Edit"):
        check_migration_write(tool_input)


if __name__ == "__main__":
    main()
