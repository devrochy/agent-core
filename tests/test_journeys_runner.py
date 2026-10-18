"""Piezas puras del runner de journeys (HU-SUB-12): comparaciones, plantillas, reloj y turnos."""

from __future__ import annotations

import importlib.util
import json
import shutil
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
        "sin_herramientas": ["confirmar_pago_pedido"],
        "texto_contiene": ["Basic"],
        "texto_no_contiene": ["30 días"],
        "max_caracteres": 20,
        "escala": False,
    }
    fallas = journeys._revisar_turno(
        "El Basic tiene 30 días de prueba", ["confirmar_pago_pedido"], True, espera
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
                # Los operadores van como objeto (`{"$no_nulo": true}`): un string "$no_nulo" se
                # compara literal y falla con cualquier valor real (corrida de pass^4, 2026-10-18).
                for campo, valor in ((paso.get("verifica") or {}).get("espera") or {}).items():
                    assert not (isinstance(valor, str) and valor.startswith("$")), (
                        f"{ruta.name}: {campo} usa el operador {valor!r} como string"
                    )


def test_el_pago_simulado_tiene_la_firma_de_build_provider() -> None:
    """Si `build_provider` suma un parámetro y el simulado no, la capacidad de pagos falla y
    el journey lo ve como "pago en línea no disponible" (pasó con #324)."""
    import inspect

    from pack_payments.proveedor import build_provider

    real = list(inspect.signature(build_provider).parameters)
    simulado = list(inspect.signature(journeys.PagoSimulado()._proveedor).parameters)
    assert simulado == real


# ---------------------------------------------------------------- set de un negocio (HU-SH-08)
def test_el_set_de_un_negocio_vive_en_su_tenants_root(monkeypatch, tmp_path) -> None:
    from core.config import get_settings

    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        assert journeys.carpeta_del_negocio("solvershub") == tmp_path / "solvershub" / "evals" / "journeys"
        for slug in ("../otro", "Solvers", "a/b", ""):
            with pytest.raises(SystemExit):
                journeys.carpeta_del_negocio(slug)
    finally:
        get_settings.cache_clear()


def test_la_replica_copia_los_artefactos_sin_evals_y_apunta_el_proceso(monkeypatch, tmp_path) -> None:
    from core.config import get_settings
    from core.onboarding import tenant_dir

    origen = tmp_path / "solvershub"
    (origen / "playbooks").mkdir(parents=True)
    (origen / "evals" / "journeys").mkdir(parents=True)
    (origen / "flow.json").write_text("{}", encoding="utf-8")
    (origen / "playbooks" / "ventas.md").write_text("# ventas", encoding="utf-8")
    (origen / "evals" / "journeys" / "negocio.json").write_text("{}", encoding="utf-8")
    (origen / "solvershub-respaldo-1.tgz").write_text("x", encoding="utf-8")
    (tmp_path / "otro-negocio.md").write_text("secreto", encoding="utf-8")
    (origen / "enlace.md").symlink_to(tmp_path / "otro-negocio.md")
    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    try:
        slug, raiz = journeys.preparar_replica(origen)
        assert slug.startswith("journeys-") and raiz != tmp_path
        assert tenant_dir(slug) == raiz / slug
        copia = sorted(p.relative_to(raiz / slug).as_posix() for p in (raiz / slug).rglob("*"))
        assert copia == ["flow.json", "playbooks", "playbooks/ventas.md"]
        assert (origen / "evals" / "journeys" / "negocio.json").exists(), "el negocio real no se toca"
        shutil.rmtree(raiz, ignore_errors=True)
    finally:
        get_settings.cache_clear()


def test_el_vendedor_solo_vale_en_el_proceso_y_para_el_negocio_de_la_corrida(monkeypatch) -> None:
    import core.provisioning.vendedores as vendedores

    monkeypatch.setattr(vendedores, "vendedores_configurados", vendedores.vendedores_configurados)
    negocio = uuid.uuid4()
    journeys._como_vendedor(negocio)
    assert vendedores.vendedores_configurados() == frozenset({negocio})


@pytest.mark.asyncio
async def test_los_correos_de_sistema_no_salen(monkeypatch) -> None:
    import core.gateway.system_email as correo

    monkeypatch.setattr(correo, "send_system_email", correo.send_system_email)
    correos = journeys.CorreosSimulados()
    correos.instalar()
    message_id = await correo.send_system_email("ana+1@example.com", "Tu acceso", "enlace")
    assert message_id and correos.enviados == [{"para": "ana+1@example.com", "asunto": "Tu acceso", "cuerpo": "enlace"}]


def test_documento_y_celular_unicos_por_intento() -> None:
    from core.provisioning.antiabuso import normalizar_documento, normalizar_telefono

    # Las reglas del alta cuentan por documento y teléfono: cada intento (k ≥ 2) es otra persona.
    uno, otro = journeys.datos_unicos(uuid.uuid4()), journeys.datos_unicos(uuid.uuid4())
    assert uno != otro
    assert journeys.datos_unicos(uuid.UUID(int=12345)) == journeys.datos_unicos(uuid.UUID(int=12345))
    for datos in (uno, otro):
        assert normalizar_documento(datos["documento"]) == datos["documento"]
        assert normalizar_telefono(datos["celular"]) == datos["celular"]
        assert datos["celular"].startswith("3") and len(datos["celular"]) == 10
    ctx = _ctx()
    assert ctx.render("cédula {cliente.documento}, celular {cliente.celular}") == (
        f"cédula {journeys.datos_unicos(ctx.customer_id)['documento']}, "
        f"celular {journeys.datos_unicos(ctx.customer_id)['celular']}"
    )


def test_runtime_en_un_set_con_motor_mide_con_y_sin_procedimientos() -> None:
    # pass^k con y sin el motor sobre el mismo set (ADR-0112): legacy es la línea base de un agente.
    con_motor = {"motor_de_procedimientos": True}
    assert journeys.modo_de_runtime(con_motor, None) == (None, False)  # lo de siempre: lo decide el set
    assert journeys.modo_de_runtime(con_motor, "legacy") == (False, False)
    assert journeys.modo_de_runtime(con_motor, "v3") == (True, False)  # el motor no monta releases
    sin_motor: dict[str, object] = {}
    assert journeys.modo_de_runtime(sin_motor, None) == (None, False)
    assert journeys.modo_de_runtime(sin_motor, "legacy") == (False, False)
    assert journeys.modo_de_runtime(sin_motor, "v3") == (False, True)  # release 1 por conexión (HU-AOS-15)


def test_el_set_de_procedimientos_configura_solo_packs_que_activa() -> None:
    negocio, rutas = journeys.rutas_del_set("procedimientos", [""])
    assert negocio["motor_de_procedimientos"] is True
    assert set(negocio["config"]) <= set(negocio["packs"])
    assert "pack_calendar" in negocio["packs"]  # agendar_cita (04-agendar-una-demo)
    assert {r.stem for r in rutas} >= {"04-agendar-una-demo"}
