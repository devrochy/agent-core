"""Modo grabar/reproducir del LLM de las evals (`core/llm/cassette.py`, adenda de ADR-0119).

Sin red ni LLM real: el proveedor de `record` es un guion en memoria y `replay` no tiene
proveedor (si intentara llamarlo, el test fallaría con AttributeError).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.config import get_settings
from core.llm import cassette, resolve_provider
from core.llm.types import LLMMessage, LLMResponse, LLMToolCall, LLMToolSchema, LLMUsageInfo
from core.shared.exceptions import LLMProviderError

EVALS_DIR = Path(__file__).resolve().parent.parent / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

FECHA_1 = "## Fecha y hora actual\nHoy es viernes, 9 de octubre de 2026 — 10:00 (America/Bogota).\nPróximos días: mañana sábado 10 de octubre."
FECHA_2 = "## Fecha y hora actual\nHoy es lunes, 12 de octubre de 2026 — 18:42 (America/Bogota).\nPróximos días: mañana martes 13 de octubre."
TOOLS = [LLMToolSchema(name="confirmar_pedido", description="Confirma un pedido.", parameters={"type": "object"})]


@pytest.fixture(autouse=True)
def _modo_limpio(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    get_settings.cache_clear()
    cassette.activar("off")
    yield
    cassette.activar("off")
    get_settings.cache_clear()


class Guion:
    """Proveedor guionado: devuelve las respuestas en orden y anota los pedidos."""

    provider_name = "guion"
    pricing_per_mtok = (0.14, 0.28)

    def __init__(self, respuestas: list[LLMResponse]) -> None:
        self.respuestas = list(respuestas)
        self.llamadas = 0

    async def generate(self, **_: Any) -> LLMResponse:
        self.llamadas += 1
        return self.respuestas.pop(0)


def _respuesta(texto: str = "Listo", order_id: str | None = None) -> LLMResponse:
    llamadas = [LLMToolCall(id="call_x1", name="confirmar_pedido", arguments={"order_id": order_id})] if order_id else []
    return LLMResponse(
        content=texto,
        stop_reason="tool_use" if llamadas else "end_turn",
        tool_calls=llamadas,
        usage=LLMUsageInfo(input_tokens=120, output_tokens=8),
        model="deepseek-flash",
        latency_ms=900,
    )


def _proveedor(modo: str, inner: Any = None, secretos: tuple[str, ...] = ()) -> cassette.ProveedorGrabado:
    return cassette.ProveedorGrabado(
        modo=modo,
        inner=inner,
        provider_name="deepseek",
        model="deepseek-flash",
        thinking=False,
        pricing_per_mtok=(0.14, 0.28),
        secretos=secretos,
    )


def _mensajes(order_id: str, texto: str = "Confirmo el pedido") -> list[LLMMessage]:
    return [
        LLMMessage(role="user", content="Quiero el plan básico"),
        LLMMessage(role="assistant", content="", tool_calls=[LLMToolCall(id="call_a", name="crear_pedido", arguments={})]),
        LLMMessage(role="tool", tool_call_id="call_a", content=json.dumps({"order_id": order_id, "creado": "2026-10-09T15:00:00+00:00"})),
        LLMMessage(role="user", content=texto),
    ]


async def _generar(proveedor: cassette.ProveedorGrabado, sistema: list[str], mensajes: list[LLMMessage]) -> LLMResponse:
    return await proveedor.generate(system_blocks=sistema, messages=mensajes, tools=TOOLS, max_tokens=500, temperature=0.3)


def _grabar(ruta: Path, respuestas: list[LLMResponse], pedidos: list[tuple[list[str], list[LLMMessage]]], semillas: list[int | None] | None = None) -> Guion:
    cassette.activar("record")
    guion = Guion(respuestas)
    proveedor = _proveedor("record", guion)

    async def correr() -> None:
        for i, (sistema, mensajes) in enumerate(pedidos):
            semilla = (semillas or [None] * len(pedidos))[i]
            if semilla is None:
                await _generar(proveedor, sistema, mensajes)
            else:
                with cassette.intento(semilla):
                    await _generar(proveedor, sistema, mensajes)

    with cassette.escena(ruta):
        asyncio.run(correr())
    cassette.activar("off")
    return guion


def _reproducir(ruta: Path, sistema: list[str], mensajes: list[LLMMessage], veces: int = 1, semilla: int | None = None) -> list[LLMResponse]:
    cassette.activar("replay")
    proveedor = _proveedor("replay")

    async def correr() -> list[LLMResponse]:
        salida = []
        for _ in range(veces):
            if semilla is None:
                salida.append(await _generar(proveedor, sistema, mensajes))
            else:
                with cassette.intento(semilla):
                    salida.append(await _generar(proveedor, sistema, mensajes))
        return salida

    with cassette.escena(ruta, regrabar="python evals/runner.py 06 --grabacion record"):
        return asyncio.run(correr())


def test_grabar_y_reproducir_da_la_misma_respuesta(tmp_path: Path):
    ruta = tmp_path / "golden" / "06.json"
    pedido_id = str(uuid.uuid4())
    sistema = ["## Identidad\nSoy el asistente.", f"## Perfil\nCliente nuevo.\n\n{FECHA_1}"]
    guion = _grabar(ruta, [_respuesta("Confirmo tu pedido", pedido_id)], [(sistema, _mensajes(pedido_id))])
    assert guion.llamadas == 1
    assert ruta.exists()

    [respuesta] = _reproducir(ruta, sistema, _mensajes(pedido_id))
    assert respuesta.content == "Confirmo tu pedido"
    assert respuesta.stop_reason == "tool_use"
    assert respuesta.tool_calls[0].name == "confirmar_pedido"
    assert respuesta.tool_calls[0].arguments == {"order_id": pedido_id}
    assert respuesta.usage.input_tokens == 120
    # El archivo es estable para diffs: sin latencias, con marcadores y el bloque de fecha normalizado.
    texto = ruta.read_text(encoding="utf-8")
    assert "latency" not in texto and pedido_id not in texto
    assert "<uuid:1>" in texto and cassette.FECHA_HORA in texto


def test_replay_sin_grabacion_falla_cerrado(tmp_path: Path):
    with pytest.raises(cassette.SinGrabacion, match="No hay grabación"):
        _reproducir(tmp_path / "no-existe.json", ["## Identidad"], _mensajes(str(uuid.uuid4())))


def test_replay_no_construye_el_proveedor_real():
    cassette.activar("replay")
    proveedor = resolve_provider()  # LLM_API_KEY vacía: no hace falta
    assert isinstance(proveedor, cassette.ProveedorGrabado)
    assert proveedor.inner is None
    assert proveedor.model == get_settings().llm_model


def test_llamada_fuera_de_escena_falla():
    cassette.activar("replay")
    with pytest.raises(cassette.ErrorGrabacion, match="fuera de una escena"):
        asyncio.run(_generar(_proveedor("replay"), ["x"], _mensajes(str(uuid.uuid4()))))


def test_record_sin_api_key_no_sale_a_la_red():
    cassette.activar("record")
    with pytest.raises(cassette.ErrorGrabacion, match="LLM_API_KEY"):
        resolve_provider()


def test_modo_off_devuelve_el_proveedor_de_siempre():
    proveedor = resolve_provider()
    assert not isinstance(proveedor, cassette.ProveedorGrabado)


def test_normalizacion_ignora_fecha_hora_uuid_y_rehidrata(tmp_path: Path):
    ruta = tmp_path / "j.json"
    grabado = str(uuid.uuid4())
    _grabar(ruta, [_respuesta("Pedido listo", grabado)], [(["## Identidad", f"## Perfil\nx\n\n{FECHA_1}"], _mensajes(grabado))])

    nuevo = str(uuid.uuid4())
    a = cassette.normalizar(proveedor="d", modelo="m", thinking=None, system_blocks=[FECHA_1], messages=_mensajes(grabado), tools=None, max_tokens=1, temperature=None)
    b = cassette.normalizar(proveedor="d", modelo="m", thinking=None, system_blocks=[FECHA_2], messages=_mensajes(nuevo), tools=None, max_tokens=1, temperature=None)
    assert a.clave == b.clave

    # Otra corrida: otra fecha, otro pedido. La respuesta trae el id de ESTA corrida.
    [respuesta] = _reproducir(ruta, ["## Identidad", f"## Perfil\nx\n\n{FECHA_2}"], _mensajes(nuevo))
    assert respuesta.tool_calls[0].arguments == {"order_id": nuevo}


def test_el_bloque_de_fecha_no_se_come_las_secciones_siguientes():
    bloque = f"## Perfil\nAna\n\n{FECHA_1}\n\n## Otra sección\nimporta"
    assert cassette._sin_fecha_hora(bloque) == f"## Perfil\nAna\n\n## Fecha y hora actual\n{cassette.FECHA_HORA}\n\n## Otra sección\nimporta"


def test_cambio_de_prompt_da_grabacion_desactualizada_con_el_diff(tmp_path: Path):
    ruta = tmp_path / "06.json"
    pedido_id = str(uuid.uuid4())
    _grabar(ruta, [_respuesta()], [(["## Reglas\nNunca inventes precios.", FECHA_1], _mensajes(pedido_id))])
    with pytest.raises(cassette.GrabacionDesactualizada) as error:
        _reproducir(ruta, ["## Reglas\nNunca inventes precios ni stock.", FECHA_1], _mensajes(pedido_id))
    mensaje = str(error.value)
    assert "Grabación desactualizada" in mensaje
    assert "bloque 1 (## Reglas)" in mensaje
    assert "-Nunca inventes precios." in mensaje and "+Nunca inventes precios ni stock." in mensaje
    assert "--grabacion record" in mensaje and "OK del dueño" in mensaje


def test_cambio_en_un_mensaje_dice_cual(tmp_path: Path):
    ruta = tmp_path / "06.json"
    pedido_id = str(uuid.uuid4())
    _grabar(ruta, [_respuesta()], [(["## Reglas"], _mensajes(pedido_id))])
    with pytest.raises(cassette.GrabacionDesactualizada, match="mensaje 4 \\(user\\)"):
        _reproducir(ruta, ["## Reglas"], _mensajes(pedido_id, texto="Mejor cancelo"))


def test_k_intentos_se_reproducen_en_secuencia(tmp_path: Path):
    ruta = tmp_path / "06.json"
    pedido_id = str(uuid.uuid4())
    pedido = (["## Reglas"], _mensajes(pedido_id))
    _grabar(ruta, [_respuesta("primera"), _respuesta("segunda")], [pedido, pedido])
    dato = json.loads(ruta.read_text(encoding="utf-8"))
    assert len(dato["llamadas"]) == 1 and len(dato["llamadas"][0]["respuestas"]) == 2

    assert [r.content for r in _reproducir(ruta, *pedido, veces=2)] == ["primera", "segunda"]
    with pytest.raises(cassette.GrabacionDesactualizada, match="se pidió 3 veces"):
        _reproducir(ruta, *pedido, veces=3)


def test_la_semilla_del_intento_es_parte_de_la_clave(tmp_path: Path):
    ruta = tmp_path / "10.json"
    pedido_id = str(uuid.uuid4())
    pedido = (["## Cliente simulado"], _mensajes(pedido_id))
    _grabar(ruta, [_respuesta("semilla cero"), _respuesta("semilla uno")], [pedido, pedido], semillas=[0, 1])
    assert len(json.loads(ruta.read_text(encoding="utf-8"))["llamadas"]) == 2
    assert _reproducir(ruta, *pedido, semilla=1)[0].content == "semilla uno"
    assert _reproducir(ruta, *pedido, semilla=0)[0].content == "semilla cero"
    with pytest.raises(cassette.GrabacionDesactualizada, match="semilla 2"):
        _reproducir(ruta, *pedido, semilla=2)


def test_produccion_rechaza_el_modo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    for modo in ("record", "replay"):
        with pytest.raises(cassette.GrabacionProhibida, match="production"):
            cassette.activar(modo)
    assert cassette.modo_activo() == "off"
    cassette.activar("off")  # apagarlo siempre se puede


def test_modo_desconocido():
    with pytest.raises(cassette.ErrorGrabacion, match="desconocido"):
        cassette.activar("grabar")


def test_no_escribe_la_api_key(tmp_path: Path):
    ruta = tmp_path / "06.json"
    clave = "clave-del-proveedor-123456"
    cassette.activar("record")
    proveedor = _proveedor("record", Guion([_respuesta(f"mi clave es {clave}")]), secretos=(clave,))
    with pytest.raises(cassette.ErrorGrabacion, match="API key"), cassette.escena(ruta):
        asyncio.run(_generar(proveedor, ["## Reglas"], _mensajes(str(uuid.uuid4()))))
    assert not ruta.exists()


def test_no_escribe_algo_con_forma_de_api_key(tmp_path: Path):
    ruta = tmp_path / "06.json"
    cassette.activar("record")
    proveedor = _proveedor("record", Guion([_respuesta("sk-abcdefghijklmnopqrstuvwx")]))
    with pytest.raises(cassette.ErrorGrabacion, match="forma de credencial"), cassette.escena(ruta):
        asyncio.run(_generar(proveedor, ["## Reglas"], _mensajes(str(uuid.uuid4()))))
    assert not ruta.exists()


def test_el_error_no_es_de_proveedor():
    """El loop convierte `LLMProviderError` en fallback o escalamiento; la grabación debe cortar."""
    assert not issubclass(cassette.ErrorGrabacion, LLMProviderError)


def test_demo_mode_graba_y_reproduce_por_resolve_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cableado completo por `resolve_provider` con el FakeProvider del demo (sin red)."""
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()
    ruta = tmp_path / "demo.json"
    mensajes = [LLMMessage(role="user", content="hola")]

    cassette.activar("record")
    with cassette.escena(ruta):
        grabada = asyncio.run(resolve_provider().generate(system_blocks=["## X"], messages=mensajes))
    cassette.activar("replay")
    with cassette.escena(ruta):
        repetida = asyncio.run(resolve_provider().generate(system_blocks=["## X"], messages=mensajes))
    assert repetida.content == grabada.content


def test_journeys_no_traga_el_error_de_grabacion(monkeypatch: pytest.MonkeyPatch):
    """Un journey que pide algo no grabado corta la corrida, no cuenta como journey fallido."""
    import journeys

    async def paso_que_pide_al_llm(*_: Any) -> None:
        raise cassette.GrabacionDesactualizada("cambió el prompt")

    monkeypatch.setattr(journeys, "paso_evento", paso_que_pide_al_llm)
    ctx = journeys.Contexto(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), journeys.PagoSimulado())
    with pytest.raises(cassette.GrabacionDesactualizada):
        asyncio.run(journeys._correr_pasos({"pasos": [{"evento": "x"}]}, "j", ctx, 0.0, []))


def test_grabaciones_de_un_negocio_no_van_en_el_framework(tmp_path: Path):
    import grabaciones

    with pytest.raises(SystemExit, match="framework"):
        grabaciones.raiz(str(EVALS_DIR / "cassettes"), tmp_path / "tenants" / "x" / "evals")
    assert grabaciones.raiz("", tmp_path / "x" / "evals") == (tmp_path / "x" / "evals" / "cassettes").resolve()
    assert grabaciones.raiz("", None) == EVALS_DIR / "cassettes"
    # Raíz explícita compartida: cada negocio en su carpeta.
    assert grabaciones.raiz(str(tmp_path / "comun"), tmp_path / "t" / "ana" / "evals") == (tmp_path / "comun" / "ana").resolve()


def test_no_escribe_secretos_de_la_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cualquier credencial de la config (aquí la de Meta, que podría llegar en un tool result)."""
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secreto-de-meta-de-prueba-0001")
    get_settings.cache_clear()
    ruta = tmp_path / "06.json"
    cassette.activar("record")
    mensajes = [LLMMessage(role="tool", tool_call_id="c1", content='{"x": "secreto-de-meta-de-prueba-0001"}')]
    with pytest.raises(cassette.ErrorGrabacion, match="whatsapp_app_secret"), cassette.escena(ruta):
        asyncio.run(_generar(_proveedor("record", Guion([_respuesta()])), ["## Reglas"], mensajes))
    assert not ruta.exists()


@pytest.mark.parametrize(
    "texto",
    [
        "EAAGm0PX4ZCpsBAKZB2ZAZBn8ZCZAexampletoken",
        "prv_test_abcdefghijklmnop",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "postgresql://usuario:clave-real@db.example.com/x",
        "https://api.example.com/v1?access_token=abcdefghijkl",
    ],
)
def test_formas_de_credencial(texto: str, tmp_path: Path):
    ruta = tmp_path / "06.json"
    cassette.activar("record")
    with pytest.raises(cassette.ErrorGrabacion, match="forma de credencial"), cassette.escena(ruta):
        asyncio.run(_generar(_proveedor("record", Guion([_respuesta(texto)])), ["## Reglas"], _mensajes(str(uuid.uuid4()))))


def test_replay_rechaza_tools_que_no_se_ofrecieron(tmp_path: Path):
    """La grabación es un dato no confiable: no puede hacer ejecutar una tool que no se ofreció."""
    ruta = tmp_path / "06.json"
    pedido_id = str(uuid.uuid4())
    _grabar(ruta, [_respuesta("ok", pedido_id)], [(["## Reglas"], _mensajes(pedido_id))])
    dato = json.loads(ruta.read_text(encoding="utf-8"))
    dato["llamadas"][0]["respuestas"][0]["tool_calls"][0]["name"] = "borrar_todo"
    ruta.write_text(json.dumps(dato), encoding="utf-8")
    with pytest.raises(cassette.GrabacionDesactualizada, match="no ofrecidas"):
        _reproducir(ruta, ["## Reglas"], _mensajes(pedido_id))


@pytest.mark.parametrize("contenido", ["{no es json", '{"formato": 99}', '{"formato": 1, "llamadas": [{"clave": "x"}]}'])
def test_grabacion_ilegible_es_error_de_grabacion(contenido: str, tmp_path: Path):
    ruta = tmp_path / "06.json"
    ruta.write_text(contenido, encoding="utf-8")
    with pytest.raises(cassette.GrabacionDesactualizada):
        _reproducir(ruta, ["## Reglas"], _mensajes(str(uuid.uuid4())))


def test_token_hex_de_16_se_normaliza_pero_no_los_numeros():
    def clave(texto: str) -> str:
        return cassette.normalizar(
            proveedor="d", modelo="m", thinking=None, system_blocks=[], tools=None, max_tokens=1, temperature=None,
            messages=[LLMMessage(role="tool", tool_call_id="c", content=texto)],
        ).clave

    assert clave('{"token": "3fa9c0d2e81b4a77"}') == clave('{"token": "b0c1d2e3f4a5b6c7"}')
    assert clave("tarjeta 4242424242424242") != clave("tarjeta 4000000000000002")


def test_sin_embeddings_en_record_o_replay():
    from core.memory import vector

    cassette.activar("replay")
    assert vector.get_embedding_provider() is None
