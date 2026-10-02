#!/usr/bin/env python3
"""Guardia de comandos peligrosos y de migraciones sin aislamiento por tenant.

Hook PreToolUse de Claude Code y ÚNICA implementación de las guardas: el plugin
de OpenCode (`.opencode/plugins/guards.ts`) ejecuta este mismo script y aplica
su decisión. Recibe el input del hook como JSON por stdin y responde por stdout:

- deny: operaciones de producción prohibidas desde una sesión, y migraciones
  (Alembic `.py` del core y de los packs, o `.sql`) que crean tablas sin
  `tenant_id` + RLS habilitada y forzada + policy de aislamiento.
- ask: comandos que operan sobre recursos de producción de Coolify
  (contenedores, despliegues, la BD); se los pide confirmar al humano.

Solo deniega o pregunta por la tool call puntual, nunca corta el turno. Ante un
input que no puede interpretar, deja pasar (fail-open): la guardia no debe
romper la sesión.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROD_PATTERNS = ("PROD_DATABASE_URL", "prod.supabase.co", "--force", "drop database", "truncate ")

# Recursos de PRODUCCIÓN en Coolify (fuente: docs/despliegue-coolify.md). En
# Coolify la app de producción todavía se llama "agent-core-test", así que se
# identifican por UUID, nunca por nombre.
PROD_RESOURCES = {
    "ggzjdyc7zsemt6kzafx5rg7m": "app de producción",
    "huvhzaxim2notqkcxqa5930a": "Postgres de producción",
    "gk9bagcojtdrtofcx8tdxkiq": "Redis de producción",
    "hwh7s5gscnn1k3yqf88xceja": "beat de producción",
    "wv6kycjeae0lmfj11dprkasq": "worker de producción",
}
PROD_DB_PORTS = ("5439", "6381")  # Postgres y Redis de producción en el loopback
PROD_HOST = "app.solvershub.site"

# Verbos que operan sobre un recurso. Los de solo lectura (docker ps, logs,
# inspect) pasan sin preguntar.
OPERATE_RE = re.compile(
    r"docker\s+(exec|rm|stop|restart|kill|cp|start|update)\b"
    r"|/(start|restart|stop|deploy)\b"
    r"|\b(psql|pg_dump|pg_restore|redis-cli)\b",
    re.IGNORECASE,
)
DB_CLIENT_RE = re.compile(r"\b(psql|pg_dump|pg_restore|redis-cli)\b", re.IGNORECASE)
MUTATING_HTTP_RE = re.compile(
    r"(-X\s*|--request\s+)(POST|PUT|PATCH|DELETE)\b"
    r"|\s(-d|--data(-raw|-binary|-urlencode)?|-F|--form)\s",
    re.IGNORECASE,
)

MIGRATION_PY_RE = re.compile(r"(^|/)alembic/versions/[^/]+\.py$")
CREATES_TABLE_RE = re.compile(r"op\.create_table\s*\(|\bcreate\s+table\b", re.IGNORECASE)
SYSTEM_TABLE_MARK = "tabla de sistema"
# Se buscan sobre el texto en minúsculas y con los espacios colapsados.
REQUIRED = (
    (re.compile(r"tenant_id"), "la columna `tenant_id`"),
    (re.compile(r"enable row level security"), "`enable row level security`"),
    # El `no force` del downgrade no cuenta como FORCE.
    (re.compile(r"(?<!no )force row level security"), "`force row level security`"),
    (re.compile(r"create policy"), "la policy `create policy tenant_isolation …`"),
    (re.compile(r"with check"), "`with check` en la policy"),
    (
        re.compile(r"nullif\(current_setting\('app\.tenant_id'"),
        "`nullif(current_setting('app.tenant_id', true), '')::uuid` (falla cerrado sin tenant)",
    ),
)


def _decide(decision: str, message: str) -> None:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": message,
        }
    }
    if decision == "deny":
        output["systemMessage"] = message
    print(json.dumps(output, ensure_ascii=False))
    sys.exit(0)


def deny(message: str) -> None:
    _decide("deny", message)


def ask(message: str) -> None:
    _decide("ask", message)


# ---------------------------------------------------------------- Bash


def check_bash(tool_input: dict) -> None:
    command = str(tool_input.get("command", ""))
    command_lower = command.lower()
    for pattern in PROD_PATTERNS:
        if pattern.lower() in command_lower:
            deny(
                f"BLOQUEADO: el comando contiene '{pattern}'.\n"
                "Las operaciones sobre producción se ejecutan a mano, nunca desde una sesión."
            )

    targets = [name for uuid, name in PROD_RESOURCES.items() if uuid in command_lower]
    operates = bool(OPERATE_RE.search(command)) and bool(targets)
    if DB_CLIENT_RE.search(command) and any(port in command for port in PROD_DB_PORTS):
        operates, targets = True, [*targets, "puerto local de la BD/Redis de producción"]
    if PROD_HOST in command_lower and MUTATING_HTTP_RE.search(command):
        operates, targets = True, [*targets, f"API de {PROD_HOST}"]
    if operates:
        ask(
            "PRODUCCIÓN: el comando opera sobre " + ", ".join(sorted(set(targets))) + ".\n"
            "Confírmalo solo si es parte de una release o lo pediste explícitamente "
            "(skill `release`). Para test, el UUID de la app es tr0vpdei5av6szcgwpe1vwcg."
        )


# ---------------------------------------------------------------- migraciones


def _is_migration(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    if MIGRATION_PY_RE.search(normalized):
        return True
    return (
        normalized.endswith(".sql")
        and "migrations" in normalized
        and not normalized.endswith(".down.sql")
    )


def _normalize(content: str) -> str:
    return re.sub(r"\s+", " ", content.lower())


def missing_isolation(content: str) -> list[str]:
    """Lo que le falta a una migración que crea tablas ([] si cumple o no crea)."""
    if not CREATES_TABLE_RE.search(content):
        return []
    normalized = _normalize(content)
    if SYSTEM_TABLE_MARK in normalized:
        return []
    return [label for pattern, label in REQUIRED if not pattern.search(normalized)]


def _read(path: str, cwd: str | None) -> str:
    target = Path(path)
    if not target.is_absolute() and cwd:
        target = Path(cwd) / target
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return ""


def _apply_edit(current: str, old: str, new: str, replace_all: bool) -> str:
    if old and old in current:
        return current.replace(old, new) if replace_all else current.replace(old, new, 1)
    return current + "\n" + new


def check_migration_write(tool_name: str, tool_input: dict, cwd: str | None) -> None:
    file_path = str(tool_input.get("file_path", ""))
    if not _is_migration(file_path):
        return

    if tool_name == "Write":
        before = ""
        after = str(tool_input.get("content", ""))
        added = after
    else:
        before = _read(file_path, cwd)
        edits = tool_input.get("edits") or [tool_input]  # MultiEdit o Edit
        after, added = before, ""
        for edit in edits:
            new = str(edit.get("new_string", ""))
            after = _apply_edit(
                after, str(edit.get("old_string", "")), new, bool(edit.get("replace_all"))
            )
            added += "\n" + new

    missing = missing_isolation(after)
    if not missing:
        return
    # Una edición menor a una migración vieja que ya no cumplía pasa; se bloquea
    # si agrega una tabla o si quita algo que el archivo sí tenía.
    if tool_name != "Write" and not CREATES_TABLE_RE.search(added):
        if not set(missing) - set(missing_isolation(before)):
            return

    deny(
        f"BLOQUEADO: la migración {file_path} crea tablas y le falta: "
        + "; ".join(missing)
        + ".\nToda tabla de negocio lleva tenant_id + RLS habilitada y FORZADA + policy "
        "tenant_isolation con nullif en USING y WITH CHECK (skill db-migration).\n"
        "Si es una tabla de sistema sin RLS, decláralo en la docstring "
        '("Tabla de SISTEMA (sin RLS): <motivo>, ADR-00NN") y otorga grants mínimos a app_runtime.'
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)
    if not isinstance(payload, dict):
        sys.exit(0)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    try:
        if tool_name == "Bash":
            check_bash(tool_input)
        elif tool_name in ("Write", "Edit", "MultiEdit"):
            check_migration_write(tool_name, tool_input, payload.get("cwd"))
    except Exception:  # fail-open: la guardia nunca rompe la sesión
        sys.exit(0)


if __name__ == "__main__":
    main()
