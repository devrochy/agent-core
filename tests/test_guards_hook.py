"""Hook `.claude/hooks/guards.py`: guardas de producción y de migraciones sin RLS.

Se ejecuta como lo hace Claude Code (y el plugin de OpenCode): JSON por stdin,
decisión por stdout. Sin salida = la tool call pasa.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "guards.py"

POLICY = (
    "RLS = \"nullif(current_setting('app.tenant_id', true), '')::uuid\"\n"
    "def _rls(t):\n"
    '    op.execute(f"alter table {t} enable row level security")\n'
    '    op.execute(f"alter table {t} force row level security")\n'
    '    op.execute(f"create policy tenant_isolation on {t} using (tenant_id = {RLS}) '
    'with check (tenant_id = {RLS})")\n'
)
TABLE = 'op.create_table("pack_x_cosas", sa.Column("tenant_id", sa.Uuid(), nullable=False))\n'
DOWNGRADE = (
    'def downgrade():\n    op.execute("alter table pack_x_cosas no force row level security")\n'
)
MIGRATION_OK = POLICY + "def upgrade():\n    " + TABLE + '    _rls("pack_x_cosas")\n' + DOWNGRADE
FORCE_LINE = '    op.execute(f"alter table {t} force row level security")\n'


def decide(tool_name: str, tool_input: dict, cwd: Path | None = None) -> str:
    payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": str(cwd or HOOK.parent)}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    if not proc.stdout.strip():
        return "allow"
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("psql $PROD_DATABASE_URL", "deny"),
        ("git push --force origin rama", "deny"),
        ("docker ps --format '{{.Names}}' | grep ggzjdyc", "allow"),
        ("docker logs ggzjdyc7zsemt6kzafx5rg7m-1 --tail 50", "allow"),
        ('docker exec huvhzaxim2notqkcxqa5930a psql -U postgres -tAc "select 1"', "ask"),
        (
            "curl -s -X POST http://127.0.0.1:8000/api/v1/applications/ggzjdyc7zsemt6kzafx5rg7m/start",
            "ask",
        ),
        (
            "curl -s -X POST http://127.0.0.1:8000/api/v1/applications/tr0vpdei5av6szcgwpe1vwcg/start",
            "allow",
        ),
        ('docker exec 48h2r2laml64eyrggfg6eqld psql -U postgres -tAc "select 1"', "allow"),
        ("psql -h 127.0.0.1 -p 5439 -U postgres agent_core", "ask"),
        ("curl -X POST https://app.solvershub.site/api/v1/admin/tenants -d '{}'", "ask"),
        ("curl -s https://app.solvershub.site/healthz", "allow"),
        ("uv run pytest -q", "allow"),
        # H3: leer `.env*` por Bash queda bloqueado; `.env.example` pasa.
        ("cat .env", "deny"),
        ("grep -i key .env.local", "deny"),
        ("head -5 .env.production", "deny"),
        ("cat .env.example", "allow"),
        ("python -c 'import os; print(os.environ)'", "allow"),
    ],
)
def test_bash(command: str, expected: str) -> None:
    assert decide("Bash", {"command": command}) == expected


@pytest.mark.parametrize(
    ("path", "content", "expected"),
    [
        ("alembic/versions/0043_cosas.py", MIGRATION_OK, "allow"),
        ("packages/pack-x/alembic/versions/0003_cosas.py", MIGRATION_OK, "allow"),
        ("alembic/versions/0043_cosas.py", MIGRATION_OK.replace(FORCE_LINE, ""), "deny"),
        ("alembic/versions/0043_cosas.py", MIGRATION_OK.replace("with check", "--"), "deny"),
        ("alembic/versions/0043_cosas.py", MIGRATION_OK.replace("nullif(", "coalesce("), "deny"),
        (
            "alembic/versions/0043_catalogo.py",
            '"""Tabla de SISTEMA (sin RLS), ADR-0099."""\n' + TABLE,
            "allow",
        ),
        (
            "alembic/versions/0043_nota.py",
            'op.add_column("orders", sa.Column("nota", sa.Text()))\n',
            "allow",
        ),
        ("core/no_es_migracion.py", TABLE, "allow"),
        (
            "supabase/migrations/001_x.sql",
            "create table pack_x_a (id uuid, tenant_id uuid not null);",
            "deny",
        ),
        ("supabase/migrations/001_x.down.sql", "create table pack_x_a (id uuid);", "allow"),
    ],
)
def test_write_migration(path: str, content: str, expected: str) -> None:
    assert decide("Write", {"file_path": f"/repo/{path}", "content": content}) == expected


def test_edit_evaluates_resulting_file(tmp_path: Path) -> None:
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    compliant = versions / "0036_ok.py"
    compliant.write_text(MIGRATION_OK)
    legacy = versions / "0003_legado.py"
    legacy.write_text(MIGRATION_OK.replace(FORCE_LINE, ""))  # ya no cumplía

    # Quitar el FORCE del upgrade se detecta aunque el downgrade diga "no force".
    assert (
        decide("Edit", {"file_path": str(compliant), "old_string": FORCE_LINE, "new_string": ""})
        == "deny"
    )
    # Con ruta relativa, se resuelve contra el cwd del payload.
    rel = {"file_path": "alembic/versions/0036_ok.py", "old_string": FORCE_LINE, "new_string": ""}
    assert decide("Edit", rel, cwd=tmp_path) == "deny"
    # Una edición menor a una migración vieja pasa; agregarle una tabla, no.
    minor = {
        "file_path": str(legacy),
        "old_string": "def downgrade():",
        "new_string": "def downgrade():  # x",
    }
    assert decide("Edit", minor) == "allow"
    table = {
        "file_path": str(legacy),
        "old_string": "def downgrade():",
        "new_string": TABLE + "def downgrade():",
    }
    assert decide("Edit", table) == "deny"
    assert decide("MultiEdit", {"file_path": str(legacy), "edits": [table]}) == "deny"


@pytest.mark.parametrize("raw", ["{no es json", "[]", ""])
def test_fail_open(raw: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=raw, capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip() == ""
