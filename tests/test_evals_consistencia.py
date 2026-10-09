"""pass@1, pass^k, inestabilidad y cliente simulado de los evals (HU-ARCH-28, ADR-0119)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _cargar(nombre: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(nombre, RAIZ / "evals" / f"{nombre}.py")
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nombre] = modulo  # los dataclasses resuelven tipos por su módulo
    spec.loader.exec_module(modulo)
    return modulo


consistencia = _cargar("consistencia")
simulador = _cargar("simulador")
journeys = _cargar("journeys")
Intento = consistencia.Intento
ResultadoEscenario = consistencia.ResultadoEscenario


def _escenario(nombre: str, *oks: bool) -> ResultadoEscenario:
    return ResultadoEscenario(nombre, [Intento(ok=ok) for ok in oks])


def test_pass_1_y_pass_k_por_escenario() -> None:
    estable = _escenario("a", True, True, True, True)
    inestable = _escenario("b", True, False, True, True)
    roto = _escenario("c", False, False, False, False)
    assert (estable.pass_1, estable.pass_k, estable.inestable) == (1.0, 1.0, False)
    assert (inestable.pass_1, inestable.pass_k, inestable.inestable) == (0.75, 0.0, True)
    assert (roto.pass_1, roto.pass_k, roto.inestable) == (0.0, 0.0, False)


def test_resumen_promedia_pass_1_y_cuenta_pass_k() -> None:
    resumen = consistencia.resumir(
        [_escenario("a", True, True, True, True), _escenario("b", True, False, True, True)], 4
    )
    assert resumen.pass_1 == 0.875
    assert resumen.pass_k == 0.5
    assert resumen.inestables == ["b"]
    texto = consistencia.texto_resumen("Golden set", resumen, 0.95)
    assert "pass@1 88%" in texto and "pass^4 50%" in texto and "inestables: b" in texto


def test_con_k_1_la_linea_es_la_de_siempre() -> None:
    assert consistencia.linea(_escenario("06-x", True)) == "  ✓ PASS  06-x"
    roto = ResultadoEscenario("07-y", [Intento(ok=False, detalle="no llamó la tool 'z'")])
    assert consistencia.linea(roto) == "  ✗ FAIL  07-y  — no llamó la tool 'z'"
    resumen = consistencia.resumir([roto], 1)
    assert "pass^" not in consistencia.texto_resumen("Golden set", resumen)


def test_linea_con_k_marca_inestables() -> None:
    assert consistencia.linea(_escenario("a", True, True)).startswith("  ✓ 2/2")
    assert consistencia.linea(_escenario("b", True, False)).startswith("  ≈ 1/2")
    assert consistencia.linea(_escenario("c", False, False)).startswith("  ✗ 0/2")


def test_atribucion_dice_que_cambio_entre_intentos() -> None:
    bueno = Intento(ok=True, herramientas=["informar_plan", "crear_pedido"])
    malo = Intento(ok=False, detalle="pedido sin crear", herramientas=["informar_plan", "escalar"])
    escenario = ResultadoEscenario("comprar", [bueno, bueno, malo, bueno])
    atribucion = escenario.atribucion()
    assert "falló 1 de 4" in atribucion
    assert "faltó crear_pedido" in atribucion
    assert "apareció escalar" in atribucion
    assert "pedido sin crear (1 veces)" in atribucion
    assert escenario.detalle().startswith("INESTABLE: ")


def test_reporte_incluye_intentos_y_aserciones(tmp_path: Path) -> None:
    intento = Intento(
        ok=False,
        aserciones=[{"tabla": "t", "campo": "estado", "esperado": "activa", "real": "pausada", "ok": False}],
        simulador={"semilla": 1, "fin": "tope"},
    )
    datos = consistencia.reporte("suscripciones", [ResultadoEscenario("x", [intento])], 1, {"k": 1})
    assert datos["pass_1"] == 0.0 and datos["pass_k"] == 0.0
    assert datos["escenarios"][0]["intentos"][0]["aserciones"][0]["real"] == "pausada"
    ruta = tmp_path / "r" / "reporte.json"
    consistencia.escribir_reporte(str(ruta), datos)
    assert json.loads(ruta.read_text(encoding="utf-8"))["set"] == "suscripciones"


def test_plantilla_del_simulador_se_parsea() -> None:
    sistema, variantes, inicio = simulador._cargar_plantilla()
    for marcador in ("{objetivo}", "{datos}", "{estilo}", "{variante}"):
        assert marcador in sistema
    assert simulador.CUMPLIDO in sistema and simulador.RINDE in sistema
    assert len(variantes) >= 2
    assert inicio


def test_la_semilla_elige_la_variante_de_forma_determinista() -> None:
    a = simulador.ClienteSimulado("x", {}, semilla=3)
    b = simulador.ClienteSimulado("x", {}, semilla=3)
    assert a.variante == b.variante
    _, variantes, _ = simulador._cargar_plantilla()
    elegidas = {simulador.ClienteSimulado("x", {}, semilla=s).variante for s in range(20)}
    assert len(elegidas) > 1 and elegidas <= set(variantes)


@dataclass
class _Respuesta:
    content: str


class _Proveedor:
    def __init__(self, textos: list[str]) -> None:
        self.textos = textos
        self.llamadas: list[dict] = []

    async def generate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.llamadas.append(kwargs)
        return _Respuesta(self.textos.pop(0))


def test_el_cliente_simulado_invierte_roles_y_detecta_el_fin() -> None:
    cliente = simulador.ClienteSimulado("Comprar el Basic", {"nombre": "Ana"}, semilla=1)
    proveedor = _Proveedor(["Hola, quiero el Basic", "Listo, gracias [OBJETIVO_CUMPLIDO]"])
    primero = asyncio.run(cliente.siguiente(proveedor))
    assert primero == "Hola, quiero el Basic"
    cliente.anotar("cliente", primero)
    cliente.anotar("asistente", "Te paso el link")
    assert asyncio.run(cliente.siguiente(proveedor)) == simulador.CUMPLIDO
    llamada = proveedor.llamadas[-1]
    roles = [m.role for m in llamada["messages"]]
    assert roles == ["user", "assistant", "user"]  # el negocio es el "user" del simulador
    assert "Comprar el Basic" in llamada["system_blocks"][0] and "Ana" in llamada["system_blocks"][0]
    traza = cliente.traza("objetivo")
    assert traza["turnos"] == 1 and traza["fin"] == "objetivo" and traza["semilla"] == 1


def test_el_intento_de_un_journey_lleva_la_evidencia() -> None:
    ctx = journeys.Contexto(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), journeys.PagoSimulado(), semilla=2)
    ctx.herramientas.append("crear_pedido")
    ctx.aserciones.append({"campo": "estado", "esperado": "activa", "real": "activa", "ok": True})
    intento = journeys.Resultado("x", True, "", 1.234, [], ctx).intento()
    assert intento.ok and intento.herramientas == ["crear_pedido"] and intento.segundos == 1.2
    assert intento.aserciones[0]["real"] == "activa"


def test_los_journeys_simulados_declaran_objetivo_y_aserciones_de_estado() -> None:
    carpeta = RAIZ / "evals" / "journeys" / "suscripciones"
    simulados = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(carpeta.glob("*-simulado-*.json"))]
    assert len(simulados) >= 2
    for journey in simulados:
        pasos = journey["pasos"]
        assert any("simulado" in p and p["simulado"].get("objetivo") for p in pasos)
        assert any("verifica" in p and p["verifica"].get("espera") for p in pasos)
