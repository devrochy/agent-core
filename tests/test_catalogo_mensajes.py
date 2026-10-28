"""Pista D0 · piezas puras del catálogo de mensajes y la marca de mensaje de los packs (ADR-0178)."""

from __future__ import annotations

import importlib.metadata
import re
from typing import Any

import pytest

from core.admin import catalogo_mensajes_service as svc
from core.plugin_manager import ENTRY_POINT_GROUP

# Campos `str` con `{variable}` en su default que NO son un mensaje al cliente (no llevan la marca a propósito).
NO_SON_MENSAJE: set[tuple[str, str]] = set()


def _schemas() -> dict[str, Any]:
    salida: dict[str, Any] = {}
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        clase = ep.load()
        schema = getattr(clase, "config_schema", None)
        if schema is not None:
            salida[ep.name] = schema
    return salida


def test_todo_texto_de_pack_con_variables_esta_marcado() -> None:
    """Un campo nuevo con `{variables}` en su default y sin marca se perdería del catálogo en silencio: este test
    obliga a marcarlo (`json_schema_extra={"mensaje": "<canal>"}`) o a declararlo en `NO_SON_MENSAJE`."""
    sin_marca: list[str] = []
    for pack, schema in _schemas().items():
        marcados = svc.campos_de_texto(schema)
        for nombre, info in schema.model_fields.items():
            if info.annotation is not str or not isinstance(info.default, str):
                continue
            if re.search(r"\{[a-z][a-z0-9_.]*\}", info.default) and nombre not in marcados and (pack, nombre) not in NO_SON_MENSAJE:
                sin_marca.append(f"{pack}.{nombre}")
    assert sin_marca == [], f"campos con variables sin marca de mensaje: {sin_marca}"


def test_las_marcas_son_canales_conocidos() -> None:
    marcas = {m for schema in _schemas().values() for m in svc.campos_de_texto(schema).values()}
    assert marcas and marcas <= svc.CANALES_DE_MARCA


def test_el_core_no_detecta_campos_por_nombre() -> None:
    from pydantic import BaseModel, Field

    class Config(BaseModel):
        aviso_texto: str = "sin marca, aunque se llame como un texto"
        saludo: str = Field(default="Hola {a.b}", json_schema_extra={"mensaje": "whatsapp", "format": "textarea"})
        numero: int = Field(default=1, json_schema_extra={"mensaje": "whatsapp"})

    assert svc.campos_de_texto(Config) == {"saludo": "whatsapp"}


def test_canales_de_usa_la_marca_o_la_constante_del_tipo() -> None:
    assert svc.canales_de("meta") == ["whatsapp"]
    assert svc.canales_de("texto_pack", "email") == ["email"]
    assert svc.canales_de("texto_pack", "algo_raro") == ["whatsapp"]
    # Salen de la tabla de capacidades por canal (C1): hoy solo WhatsApp, y el playground no cuenta.
    for tipo in ("meta", "evento", "opciones", "texto_pack"):
        assert svc.canales_de(tipo) == ["whatsapp"]


def test_llaves_y_resumen() -> None:
    assert svc._llaves("Hola {cliente.nombre}, {fecha} y {fecha} {{1}} {A}") == ["cliente.nombre", "fecha"]
    assert svc._resumen("  \n ") is None
    largo = svc._resumen("palabra " * 100)
    assert largo is not None and len(largo) <= svc.MAX_RESUMEN and largo.endswith("…")


def test_fuera_del_tenant_scope_no_lee() -> None:
    import asyncio
    import uuid

    with pytest.raises(PermissionError):
        asyncio.run(svc.catalogo_de_mensajes(uuid.uuid4()))
