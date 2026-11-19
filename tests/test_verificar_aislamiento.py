"""`scripts/verificar_aislamiento.py`: entorno, reutilización de `rls_forced` y salida.

Sin base de datos. Las consultas al catálogo (dueños, BYPASSRLS, RLS fuera de `public`)
corren contra Postgres en `tests/integration/test_scripts_produccion_vps.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "verificar_aislamiento.py"


def _cargar():
    spec = importlib.util.spec_from_file_location("verificar_aislamiento", _RUTA)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["verificar_aislamiento"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


va = _cargar()

URL_PROD = "postgresql+asyncpg://app_runtime:Otra%2FClave@10.0.0.3:5432/agent_core"


# ------------------------------------------------------------------ contraseña


def test_contrasena_de_desarrollo_falla() -> None:
    check = va.revisar_contrasena("postgresql+asyncpg://app_runtime:app_runtime@db:5432/agent_core")
    assert check.ok is False
    assert "ALTER ROLE" in check.detail


def test_contrasena_propia_pasa_y_no_se_imprime() -> None:
    check = va.revisar_contrasena(URL_PROD)
    assert check.ok is True
    assert "Otra" not in check.detail


def test_url_sin_contrasena_queda_como_advertencia() -> None:
    assert va.revisar_contrasena("postgresql://app_runtime@db/agent_core").ok is None


def test_la_url_se_describe_sin_contrasena() -> None:
    assert va.describir_url(URL_PROD) == "agent_core en 10.0.0.3:5432 como app_runtime"


# ------------------------------------------------------------------ ejecutar()


def _instalar_falsos(monkeypatch, *, rls_forced_ok: bool, llamadas: list[str]):
    """Reemplaza la base: `check_rls_forced` (el del checklist) y los checks de catálogo."""
    import core.db.session as session_mod
    import core.gateway.meta_production_readiness as readiness

    async def _rls_forced():
        llamadas.append("rls_forced")
        return va._check("rls_forced", rls_forced_ok, "detalle del checklist")

    @asynccontextmanager
    async def _sesion():
        yield object()

    class _Motor:
        async def dispose(self) -> None:
            llamadas.append("dispose")

    async def _otros(session):
        return va._check("rls_otros_esquemas", True, "ok")

    async def _rol(session, rol_esperado):
        return [va._check("rol_runtime", True, "ok"), va._check("rol_esperado", True, rol_esperado)]

    monkeypatch.setattr(readiness, "check_rls_forced", _rls_forced)
    monkeypatch.setattr(session_mod, "get_session", _sesion)
    monkeypatch.setattr(session_mod, "get_engine", lambda: _Motor())
    monkeypatch.setattr(va, "revisar_rls_otros_esquemas", _otros)
    monkeypatch.setattr(va, "revisar_rol", _rol)


async def test_sin_database_url_sale_con_2_y_no_cae_al_env(tmp_path, monkeypatch, capsys) -> None:
    """Un `.env` en el directorio no aporta la URL: sin la variable no se consulta nada."""
    (tmp_path / ".env").write_text(f"DATABASE_URL={URL_PROD}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    llamadas: list[str] = []
    _instalar_falsos(monkeypatch, rls_forced_ok=True, llamadas=llamadas)

    assert await va.ejecutar() == va.SALIDA_ERROR
    assert llamadas == []
    assert "DATABASE_URL" in capsys.readouterr().err


async def test_reutiliza_rls_forced_y_todo_ok_sale_con_0(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", URL_PROD)
    llamadas: list[str] = []
    _instalar_falsos(monkeypatch, rls_forced_ok=True, llamadas=llamadas)

    assert await va.ejecutar() == va.SALIDA_OK
    salida = capsys.readouterr().out
    assert llamadas == ["rls_forced", "dispose"]
    for clave in (
        "rls_forced",
        "rls_otros_esquemas",
        "rol_runtime",
        "rol_esperado",
        "contrasena_runtime",
    ):
        assert f"[OK] {clave}:" in salida
    assert "Otra" not in salida


async def test_si_rls_forced_falla_sale_con_1(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", URL_PROD)
    _instalar_falsos(monkeypatch, rls_forced_ok=False, llamadas=[])

    assert await va.ejecutar() == va.SALIDA_FALLA
    assert "[X ] rls_forced: detalle del checklist" in capsys.readouterr().out


async def test_contrasena_de_desarrollo_hace_fallar_la_corrida(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://app_runtime:app_runtime@db/agent_core")
    _instalar_falsos(monkeypatch, rls_forced_ok=True, llamadas=[])

    assert await va.ejecutar() == va.SALIDA_FALLA
    assert "[X ] contrasena_runtime" in capsys.readouterr().out


async def test_sin_conexion_sale_con_2(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DATABASE_URL", URL_PROD)
    _instalar_falsos(monkeypatch, rls_forced_ok=True, llamadas=[])
    import core.gateway.meta_production_readiness as readiness

    async def _caida():
        # Un driver que repite la URL (con la clave) en su mensaje.
        raise ConnectionRefusedError(f"no hay servidor en {URL_PROD} (clave Otra/Clave)")

    monkeypatch.setattr(readiness, "check_rls_forced", _caida)

    assert await va.ejecutar() == va.SALIDA_ERROR
    error = capsys.readouterr().err
    assert "ConnectionRefusedError" in error
    assert "Otra" not in error


@pytest.mark.parametrize(
    ("nombres", "esperado"),
    [(["a", "b"], "a, b"), ([str(i) for i in range(12)], "0, 1, 2, 3, 4, 5, 6, 7, 8, 9 y 2 más")],
)
def test_listar_corta_las_listas_largas(nombres, esperado) -> None:
    assert va._listar(nombres) == esperado
