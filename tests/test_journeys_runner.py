"""Piezas puras del runner de journeys (HU-SUB-12): comparaciones, plantillas, reloj y turnos."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
assert _SPEC and _SPEC.loader
journeys = importlib.util.module_from_spec(_SPEC)
sys.modules["journeys"] = journeys  # los dataclasses resuelven tipos por su módulo
_SPEC.loader.exec_module(journeys)


def _ctx():
    return journeys.Contexto(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), journeys.PagoSimulado())


def test_coincide_numeros_textos_y_operadores() -> None:
    assert journeys._coincide("200000.00", 200000)
    assert journeys._coincide("activa", "activa")
    assert not journeys._coincide("pausada", "activa")
    assert journeys._coincide(None, None)
    assert journeys._coincide("x", {"$no_nulo": True})
    assert journeys._coincide(None, {"$no_nulo": False})
    assert journeys._coincide("vencida", {"$en": ["activa", "vencida"]})
    assert journeys._coincide("Plan Premium", {"$contiene": "premium"})


def test_render_reemplaza_variables_y_conserva_tipos() -> None:
    ctx = _ctx()
    ctx.variables["cargo"] = {"id": "c-1", "monto": "200000.00"}
    assert ctx.render("sub-{cargo.id}") == "sub-c-1"
    assert ctx.render({"amount": "{cargo.monto}", "lista": ["{cliente.id}"]}) == {
        "amount": "200000.00",
        "lista": [str(ctx.customer_id)],
    }
    with pytest.raises(journeys.FallaJourney, match="sin valor"):
        ctx.render("{no.existe}")


def test_reloj_adelanta_el_tiempo_del_journey() -> None:
    ctx = _ctx()
    registro: list[str] = []
    journeys.paso_reloj(ctx, {"reloj": "+30d"}, registro)
    journeys.paso_reloj(ctx, {"reloj": "+6h"}, registro)
    assert ctx.desfase == timedelta(days=30, hours=6)
    with pytest.raises(journeys.FallaJourney):
        journeys.paso_reloj(ctx, {"reloj": "mañana"}, registro)


def test_revisar_turno_reporta_cada_falla() -> None:
    espera = {
        "herramientas": ["crear_pedido"],
        "alguna_herramienta": ["informar_plan", "consultar_inventario"],
        "sin_herramientas": ["generar_link_pago"],
        "texto_contiene": ["Basic"],
        "texto_no_contiene": ["30 días"],
        "max_caracteres": 20,
        "escala": False,
    }
    fallas = journeys._revisar_turno(
        "El Basic tiene 30 días de prueba", ["generar_link_pago"], True, espera
    )
    assert len(fallas) == 6  # no llamó crear_pedido ni las de catálogo, llamó la prohibida, dijo "30 días", largo, escaló
    assert journeys._revisar_turno("El Basic cuesta $200.000", ["informar_plan", "crear_pedido"], False, espera) == [
        "respuesta de 24 caracteres (máx 20)"
    ]


def test_los_journeys_del_repo_son_validos() -> None:
    """Cada journey tiene pasos de un tipo conocido y el set tiene su negocio."""
    tipos = {"usuario", "evento", "reloj", "job", "verifica", "accion", "simulado"}
    for carpeta in (RAIZ / "evals" / "journeys").iterdir():
        if not carpeta.is_dir():
            continue
        negocio, rutas = journeys.rutas_del_set(carpeta.name, [""])
        assert negocio
        assert rutas, carpeta.name
        for ruta in rutas:
            journey = json.loads(ruta.read_text(encoding="utf-8"))
            assert journey["pasos"], ruta.name
            for paso in journey["pasos"]:
                assert tipos & set(paso), f"{ruta.name}: paso sin tipo {paso}"


def test_el_pago_simulado_tiene_la_firma_de_build_provider() -> None:
    """Si `build_provider` suma un parámetro y el simulado no, la tool de pagos falla y el
    journey lo ve como "pago en línea no disponible" (pasó con #324)."""
    import inspect

    from pack_payments.skills import build_provider

    real = list(inspect.signature(build_provider).parameters)
    simulado = list(inspect.signature(journeys.PagoSimulado()._proveedor).parameters)
    assert simulado == real  # mismos nombres: la tool llama con `http=` por nombre
