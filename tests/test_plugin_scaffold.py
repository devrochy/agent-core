"""Scaffold del plugin del tenant (HU-FWK-13, ADR-0089).

Ejecuta `agent-core plugin scaffold --tenant` en un directorio temporal y
verifica que el plugin nace con datos, tareas, migración y tests, y que su
migración cumple el invariante nº1 (tenant_id + RLS forzada + policy).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cli.main import app


def _scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, *extra: str) -> Path:
    """Corre el scaffold dentro de `tmp_path` y devuelve la carpeta del pack."""
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(app, ["plugin", "scaffold", name, *extra])
    assert result.exit_code == 0, result.output
    return tmp_path / "packages" / f"pack-{name}"


def test_scaffold_tenant_genera_datos_tareas_migracion_y_tests(tmp_path: Path, monkeypatch):
    base = _scaffold(tmp_path, monkeypatch, "fidelidad", "--tenant")
    package = base / "pack_fidelidad"

    assert (package / "models.py").exists()
    assert (package / "jobs.py").exists()
    assert (base / "alembic.ini").exists()
    assert (base / "alembic" / "env.py").exists()
    assert list((base / "alembic" / "versions").glob("*.py"))
    assert list((base / "tests").glob("test_*_migracion_rls.py"))

    models = (package / "models.py").read_text(encoding="utf-8")
    assert "tenant_id" in models
    assert "deleted_at" in models
    # La versión_table propia evita choques con core y otros packs.
    assert "alembic_version_pack_fidelidad" in (base / "alembic.ini").read_text(encoding="utf-8")


def test_migracion_del_scaffold_cumple_rls(tmp_path: Path, monkeypatch):
    base = _scaffold(tmp_path, monkeypatch, "fidelidad", "--tenant")
    texto = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (base / "alembic" / "versions").glob("*.py")
    ).lower()

    assert "create_table" in texto
    assert "tenant_id" in texto
    assert "enable row level security" in texto
    assert re.search(r"(?<!no )force row level security", texto)
    assert "create policy" in texto
    assert "with check" in texto
    assert "app_runtime" in texto  # grant al rol de runtime


def test_scaffold_sin_tenant_no_genera_modelos(tmp_path: Path, monkeypatch):
    base = _scaffold(tmp_path, monkeypatch, "comunidad")
    package = base / "pack_comunidad"

    assert (package / "module.py").exists()
    assert (package / "skills.py").exists()
    assert not (package / "models.py").exists()
    assert not (base / "alembic.ini").exists()
