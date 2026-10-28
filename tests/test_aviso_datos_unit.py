"""Aviso de tratamiento de datos: reglas puras (HU-CRM-16, ADR-0179)."""

from __future__ import annotations

import ast
import pathlib
from types import SimpleNamespace

import pytest

from core import consentimiento as c
from core.datos_cliente import aviso as aviso_mod
from core.datos_cliente.ajustes import aviso_de, con_aviso
from core.datos_cliente.aviso_chat import interpretar

RAIZ = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "texto",
    ["Acepto", "acepto.", "  SÍ ACEPTO!! ", "Sí, acepto", "estoy de acuerdo", "Autorizo", "de acuerdo"],
)
def test_respuestas_inequivocas_de_aceptacion(texto: str) -> None:
    assert interpretar(texto) == "acepto"


@pytest.mark.parametrize("texto", ["No", "no acepto", "Rechazo", "no gracias", "NO autorizo", "no estoy de acuerdo"])
def test_respuestas_inequivocas_de_rechazo(texto: str) -> None:
    assert interpretar(texto) == "rechazo"


@pytest.mark.parametrize(
    "texto", ["ok", "dale", "listo", "claro", "sí", "no sé", "no acepto que me llamen pero sí los datos", "acepto pero", "", "cuánto cuesta"]
)
def test_lo_ambiguo_no_registra_nada(texto: str) -> None:
    assert interpretar(texto) is None


def _fila(estado: str, version: str | None = "v1") -> SimpleNamespace:
    return SimpleNamespace(estado=estado, version_aviso=version)


def test_estados_frente_a_la_version_vigente() -> None:
    assert c.estado_de(None, "v1") == c.SIN_AVISO
    assert c.estado_de(_fila("informado"), "v1") == c.INFORMADO
    assert c.estado_de(_fila("otorgado"), "v1") == c.OTORGADO
    assert c.estado_de(_fila("revocado"), "v1") == c.REVOCADO


def test_una_version_nueva_del_texto_se_vuelve_a_mostrar_salvo_que_haya_dicho_que_no() -> None:
    assert c.estado_de(_fila("otorgado", "v1"), "v2") == c.SIN_AVISO
    assert c.estado_de(_fila("informado", "v1"), "v2") == c.SIN_AVISO
    assert c.estado_de(_fila("revocado", "v1"), "v2") == c.REVOCADO  # no se le insiste


def test_la_version_sale_del_texto_y_el_borrador_esta_marcado() -> None:
    a = aviso_mod.desde(None)
    assert not a.activo and a.es_borrador and a.texto and len(a.version) == 12
    b = aviso_mod.desde(con_aviso(None, True, "Texto revisado por el abogado."))
    assert b.activo and not b.es_borrador and b.version != a.version
    assert aviso_mod.version_de(" x ") == aviso_mod.version_de("x")


def test_el_aviso_guardado_se_lee_saneado_y_vacio_quita_el_bloque() -> None:
    assert aviso_de({"datos_cliente": {"aviso": "no es dict"}}) == {"activo": False, "texto": ""}
    behavior = con_aviso({"otra": 1}, True, "  hola  ")
    assert behavior["otra"] == 1 and aviso_de(behavior) == {"activo": True, "texto": "hola"}
    assert "datos_cliente" not in con_aviso(behavior, False, "")


def test_registrar_rechaza_estados_que_no_existen() -> None:
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(c.registrar(None, tenant_id=None, customer_id=None, estado="quizas", version="v", origen="x"))  # type: ignore[arg-type]


# ------------------------------------------------------------------ el perfil solo se escribe por guardar()
# Quién puede llamar a `upsert_attributes` / `escribir_hechos`, con el motivo.
ESCRITORES_PERMITIDOS = {
    "core/memory/entity.py": "define upsert_attributes",
    "core/memory/facts.py": "define escribir_hechos",
    "core/datos_cliente/guardar.py": "la puerta de los agentes (HU-CRM-16)",
    "core/qa/escenarios.py": "siembra de datos de prueba, no es un agente",
    "core/api/routers/leads.py": "ediciones del operador desde el panel: nunca se bloquean",
    "core/orchestrator/consumer.py": "datos que el canal entrega (nombre del perfil), no los dicta el agente",
    "packages/pack-commerce/pack_commerce/api.py": "formulario público de la landing: el titular lo envía",
    "packages/pack-commerce/pack_commerce/skills.py": (
        "crear_pedido guarda el perfil solo con `guardar_datos` explícito del cliente (D17)"
    ),
}


def test_las_tools_del_agente_escriben_el_perfil_solo_por_guardar() -> None:
    ofensores = []
    for base in ("core", "packages"):
        for ruta in (RAIZ / base).rglob("*.py"):
            rel = ruta.relative_to(RAIZ).as_posix()
            if rel in ESCRITORES_PERMITIDOS or "/tests/" in rel:
                continue
            for nodo in ast.walk(ast.parse(ruta.read_text())):
                if isinstance(nodo, ast.Call):
                    nombre = nodo.func.attr if isinstance(nodo.func, ast.Attribute) else getattr(nodo.func, "id", "")
                    if nombre in {"upsert_attributes", "upsert_attributes_en_sesion", "escribir_hechos"}:
                        ofensores.append(f"{rel}:{nodo.lineno}")
    assert not ofensores, f"escriben el perfil sin pasar por datos_cliente.guardar(): {ofensores}"


def test_el_unico_upsert_que_queda_en_commerce_es_el_del_consentimiento_explicito() -> None:
    fuente = (RAIZ / "packages/pack-commerce/pack_commerce/skills.py").read_text()
    assert fuente.count("entity_memory.upsert_attributes(") == 1
    cuerpo = fuente.split("entity_memory.upsert_attributes(")[0].rsplit("if guardar_datos:", 1)[-1]
    assert len(cuerpo) < 200, "el único upsert directo debe estar dentro de `if guardar_datos:`"
