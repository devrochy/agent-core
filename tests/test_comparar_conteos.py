"""`scripts/comparar_conteos.py`: lógica de comparación, salida y códigos de salida.

Sin base de datos: `tomar_foto` se reemplaza por fotos armadas a mano. La consulta real a
Postgres (RLS forzada, roles sin bypass, extensiones) la cubre
`tests/integration/test_scripts_produccion_vps.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_RUTA = Path(__file__).resolve().parents[1] / "scripts" / "comparar_conteos.py"


def _cargar():
    spec = importlib.util.spec_from_file_location("comparar_conteos", _RUTA)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["comparar_conteos"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


cc = _cargar()

URL_ORIGEN = "postgresql+asyncpg://postgres:clave-origen@127.0.0.1:5439/agent_core"
URL_DESTINO = "postgresql://postgres:clave-destino@127.0.0.1:55499/agent_core"
ENTORNO = {cc.ENV_ORIGEN: URL_ORIGEN, cc.ENV_DESTINO: URL_DESTINO}


def _foto(conteos, versiones=None, omitidas=(), identidad=("1", "arranque", "agent_core")):
    return cc.Foto(
        descripcion="agent_core en host:5432 como postgres",
        conteos=conteos,
        versiones=versiones or {},
        omitidas=tuple(omitidas),
        identidad=identidad,
    )


def _con_fotos(monkeypatch, por_url):
    async def _falsa(url):
        resultado = por_url[url]
        if isinstance(resultado, Exception):
            raise resultado
        return resultado

    monkeypatch.setattr(cc, "tomar_foto", _falsa)


# ------------------------------------------------------------------ URLs y entorno


def test_normaliza_la_url_de_la_app_a_libpq() -> None:
    assert cc.normalizar_url("postgresql+asyncpg://u:p@h:5432/db") == "postgresql://u:p@h:5432/db"
    assert cc.normalizar_url(" postgres://u@h/db ") == "postgresql://u@h/db"


@pytest.mark.parametrize("url", ["mysql://u:p@h/db", "u:p@h/db"])
def test_rechaza_urls_que_no_son_de_postgres(url) -> None:
    with pytest.raises(cc.ErrorDeUso):
        cc.normalizar_url(url)


def test_el_servidor_se_describe_sin_contrasena() -> None:
    assert cc.servidor_de(cc.normalizar_url(URL_ORIGEN)) == "127.0.0.1:5439"
    assert "clave" not in cc.servidor_de(URL_DESTINO)


async def test_un_error_del_driver_no_muestra_la_contrasena(monkeypatch) -> None:
    import asyncpg

    url = "postgresql://postgres:Cl%40ve-secreta@127.0.0.1:5439/agent_core"

    async def _falla(*args, **kwargs):
        raise OSError(f"no se pudo usar {url} (clave Cl@ve-secreta)")

    monkeypatch.setattr(asyncpg, "connect", _falla)

    with pytest.raises(cc.ErrorDeUso) as error:
        await cc.tomar_foto(url)
    assert "Cl%40ve-secreta" not in str(error.value)
    assert "Cl@ve-secreta" not in str(error.value)
    assert "127.0.0.1:5439" in str(error.value)


async def test_sin_variables_sale_con_2_y_no_lee_un_env(tmp_path, monkeypatch, capsys) -> None:
    """Las URL vienen solo del entorno: un `.env` en el directorio no las aporta."""
    (tmp_path / ".env").write_text(
        f"{cc.ENV_ORIGEN}={URL_ORIGEN}\n{cc.ENV_DESTINO}={URL_DESTINO}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    async def _no_debe_conectar(url):
        raise AssertionError("no debía intentar conectar")

    monkeypatch.setattr(cc, "tomar_foto", _no_debe_conectar)

    assert await cc.ejecutar({}) == cc.SALIDA_ERROR
    error = capsys.readouterr().err
    assert cc.ENV_ORIGEN in error and cc.ENV_DESTINO in error


async def test_la_misma_url_en_origen_y_destino_es_error(capsys) -> None:
    entorno = {cc.ENV_ORIGEN: URL_ORIGEN, cc.ENV_DESTINO: URL_ORIGEN.replace("+asyncpg", "")}
    assert await cc.ejecutar(entorno) == cc.SALIDA_ERROR
    assert "misma URL" in capsys.readouterr().err


# ------------------------------------------------------------------ comparación


def test_comparar_clasifica_cada_tabla() -> None:
    origen = _foto(
        {"public.a": 3, "public.b": 5, "public.solo_origen": 1, "public.alembic_version": 1},
        versiones={"public.alembic_version": ("0058",)},
    )
    destino = _foto(
        {"public.a": 3, "public.b": 4, "public.solo_destino": 0, "public.alembic_version": 1},
        versiones={"public.alembic_version": ("0057",)},
    )

    filas = {f.tabla: f for f in cc.comparar(origen, destino)}

    assert filas["public.a"].estado == cc.IGUAL
    assert (filas["public.b"].estado, filas["public.b"].origen, filas["public.b"].destino) == (
        cc.DIFIERE,
        5,
        4,
    )
    assert filas["public.solo_origen"].estado == cc.FALTA_EN_DESTINO
    assert filas["public.solo_destino"].estado == cc.FALTA_EN_ORIGEN
    assert filas["public.alembic_version"].estado == cc.VERSION_DISTINTA
    assert "0058" in filas["public.alembic_version"].detalle
    assert list(filas) == sorted(filas)


def test_informe_resume_y_lista_las_diferencias() -> None:
    origen = _foto({"public.a": 3, "public.b": 5}, omitidas=["public.spatial_ref_sys"])
    destino = _foto({"public.a": 3, "public.b": 4}, omitidas=["public.spatial_ref_sys"])

    lineas = cc.informe(origen, destino, cc.comparar(origen, destino))
    texto = "\n".join(lineas)

    assert "Omitidas por ser de una extensión: public.spatial_ref_sys" in texto
    assert "Resumen: 2 tablas comparadas, 1 iguales, 1 con diferencias:" in texto
    assert "  - public.b: difiere (origen 5, destino 4)" in texto


# ------------------------------------------------------------------ códigos de salida


async def test_todo_igual_sale_con_0(monkeypatch, capsys) -> None:
    conteos = {"public.a": 3, "public.alembic_version": 1}
    versiones = {"public.alembic_version": ("0058",)}
    _con_fotos(
        monkeypatch,
        {
            cc.normalizar_url(URL_ORIGEN): _foto(conteos, versiones),
            cc.normalizar_url(URL_DESTINO): _foto(
                conteos, versiones, identidad=("2", "otro arranque", "agent_core")
            ),
        },
    )

    assert await cc.ejecutar(ENTORNO) == cc.SALIDA_IGUALES
    salida = capsys.readouterr().out
    assert "Resumen: 2 tablas comparadas, 2 iguales, 0 con diferencias." in salida
    assert "clave-origen" not in salida and "clave-destino" not in salida


async def test_una_diferencia_sale_con_1(monkeypatch, capsys) -> None:
    _con_fotos(
        monkeypatch,
        {
            cc.normalizar_url(URL_ORIGEN): _foto({"public.a": 3}),
            cc.normalizar_url(URL_DESTINO): _foto(
                {"public.a": 2}, identidad=("2", "x", "agent_core")
            ),
        },
    )

    assert await cc.ejecutar(ENTORNO) == cc.SALIDA_DIFERENCIAS
    assert "public.a: difiere (origen 3, destino 2)" in capsys.readouterr().out


async def test_error_en_las_dos_bases_reporta_ambas_y_sale_con_2(monkeypatch, capsys) -> None:
    _con_fotos(
        monkeypatch,
        {
            cc.normalizar_url(URL_ORIGEN): cc.ErrorDeUso("sin BYPASSRLS"),
            cc.normalizar_url(URL_DESTINO): cc.ErrorDeUso("no conecta"),
        },
    )

    assert await cc.ejecutar(ENTORNO) == cc.SALIDA_ERROR
    error = capsys.readouterr().err
    assert "ERROR (origen): sin BYPASSRLS" in error
    assert "ERROR (destino): no conecta" in error


async def test_dos_urls_que_llegan_a_la_misma_base_son_error(monkeypatch, capsys) -> None:
    """`localhost` y `127.0.0.1` son URL distintas pero el mismo servidor y la misma base."""
    misma = _foto({"public.a": 3}, identidad=("7", "arranque", "agent_core"))
    _con_fotos(
        monkeypatch,
        {cc.normalizar_url(URL_ORIGEN): misma, cc.normalizar_url(URL_DESTINO): misma},
    )

    assert await cc.ejecutar(ENTORNO) == cc.SALIDA_ERROR
    assert "misma base del mismo servidor" in capsys.readouterr().err
