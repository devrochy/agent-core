"""Hook `.claude/hooks/guards.py`: guardas de producción y de migraciones sin RLS.

Se ejecuta como lo hace Claude Code (y el plugin de OpenCode): JSON por stdin,
decisión por stdout. Sin salida = la tool call pasa. El modo `--migraciones` es el
que usa pre-commit sobre los archivos en stage.
"""

from __future__ import annotations

import json
import os
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


def _git(cwd: Path, *args: str) -> None:
    identity = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **identity}
    )


def check_files(cwd: Path, *paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK), "--migraciones", *paths],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def test_files_mode_compares_against_head(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    versions = tmp_path / "alembic" / "versions"
    versions.mkdir(parents=True)
    legacy = versions / "0003_legado.py"
    legacy.write_text(MIGRATION_OK.replace(FORCE_LINE, ""))  # ya no cumplía
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "legado")
    rel = "alembic/versions/0003_legado.py"

    # Una migración vieja sin cambios, o con un retoque menor, pasa.
    assert check_files(tmp_path, rel).returncode == 0
    legacy.write_text(legacy.read_text().replace("def downgrade():", "def downgrade():  # x"))
    assert check_files(tmp_path, rel).returncode == 0
    # Agregarle una tabla, no.
    legacy.write_text(legacy.read_text().replace("def downgrade():", TABLE + "def downgrade():"))
    result = check_files(tmp_path, rel)
    assert result.returncode == 1
    assert "force row level security" in result.stderr

    # Un archivo nuevo tiene que cumplir completo.
    (versions / "0004_ok.py").write_text(MIGRATION_OK)
    assert check_files(tmp_path, "alembic/versions/0004_ok.py").returncode == 0
    (versions / "0005_sin_force.py").write_text(MIGRATION_OK.replace(FORCE_LINE, ""))
    assert check_files(tmp_path, "alembic/versions/0005_sin_force.py").returncode == 1

    # Lo que no es una migración se ignora.
    (tmp_path / "modulo.py").write_text(TABLE)
    assert check_files(tmp_path, "modulo.py").returncode == 0


# ---------------------------------------------------------------- abrir PR

VERIFIER_FAILS = 'import sys\nprint("✗ [ERROR] falta un fragmento")\nsys.exit(1)\n'
VERIFIER_PASSES = (
    "import pathlib, sys\n"
    'pathlib.Path(__file__).with_name("argv.txt").write_text(" ".join(sys.argv[1:]))\n'
)


def pr_repo(root: Path, verifier: str | None, origin: str = "agent-core") -> Path:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for args in (
        ["init", "-q", "-b", "mi-rama"],
        ["remote", "add", "origin", f"https://github.com/devrochy/{origin}.git"],
        ["commit", "-q", "--allow-empty", "-m", "x"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, env=env)
    if verifier is not None:
        path = root / ".claude/skills/abrir-pr/verificar_pr.py"
        path.parent.mkdir(parents=True)
        path.write_text(verifier, encoding="utf-8")
    return root


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('gh pr create --base develop --title "fix(x): algo"', "deny"),
        ('gh pr create --title "fix(x): algo"', "deny"),
        ('git push -u origin mi-rama && gh pr create -B develop -t "fix(x): algo"', "deny"),
        ("gh pr create \\\n  --base develop \\\n  --fill", "deny"),
        ('gh pr create --base develop --body "$(cat <<EOF\ncomillas " sueltas\nEOF\n)"', "deny"),
        ("gh pr create --repo devrochy/agent-core --base develop --head mi-rama", "deny"),
        ("gh pr create --base main --title 'release'", "allow"),
        ("gh pr create --base feat/padre --title 'fix(x): hija'", "allow"),
        ("gh pr create --repo devrochy/solvershub-docs --base develop", "allow"),
        ("gh pr create --base develop --head otra-rama", "allow"),
        ("gh pr list --base develop", "allow"),
        ("echo 'gh pr create --base develop'", "allow"),
    ],
)
def test_pr_create_con_verificador_en_rojo(tmp_path: Path, command: str, expected: str) -> None:
    assert decide("Bash", {"command": command}, pr_repo(tmp_path, VERIFIER_FAILS)) == expected


def test_pr_create_explica_los_errores(tmp_path: Path) -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "gh pr create --base develop --fill"},
        "cwd": str(pr_repo(tmp_path, VERIFIER_FAILS)),
    }
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "falta un fragmento" in reason


def test_pr_create_sigue_el_cd_del_comando(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    repo.mkdir()
    pr_repo(repo, VERIFIER_FAILS)
    command = f"cd {repo} && gh pr create --base develop --fill"
    assert decide("Bash", {"command": command}, tmp_path) == "deny"


def test_pr_create_pasa_el_titulo_al_verificador(tmp_path: Path) -> None:
    pr_repo(tmp_path, VERIFIER_PASSES)
    command = 'gh pr create --base develop --title "fix(x): algo"'
    assert decide("Bash", {"command": command}, tmp_path) == "allow"
    argv = tmp_path / ".claude/skills/abrir-pr/argv.txt"
    assert argv.read_text(encoding="utf-8") == "--titulo fix(x): algo"


@pytest.mark.parametrize(
    ("verifier", "origin"),
    [(None, "agent-core"), (VERIFIER_FAILS, "solvershub-panel")],
)
def test_pr_create_sin_verificador_o_de_otro_repo(
    tmp_path: Path, verifier: str | None, origin: str
) -> None:
    pr_repo(tmp_path, verifier, origin)
    assert decide("Bash", {"command": "gh pr create --base develop"}, tmp_path) == "allow"


def test_pr_create_fuera_de_un_repo(tmp_path: Path) -> None:
    assert decide("Bash", {"command": "gh pr create --base develop"}, tmp_path) == "allow"
