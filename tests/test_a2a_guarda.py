"""Guarda A2A pura, grafo y revisión de la release (HU-AOS-18, adenda de ADR-0116). Sin base.

Los tests de caos (ciclos, autollamada, ping-pong, reintentos) van aquí contra `evaluar` y
`aplicar`; el ledger real está en `tests/integration/test_a2a_guarda_ledger.py`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.agents.grafo_a2a import EnlaceA2A, advertencias, codigo_de_error, hash_de, normalizar
from core.kernel.a2a import (
    EstadoDelTurno,
    GrafoCongelado,
    Limites,
    Operacion,
    aplicar,
    evaluar,
    payload_hash,
    redactar,
)
from core.releases import manifiesto as man
from core.releases.build import _Instancia, revisar_a2a

A, B, C, D, E, F = (uuid.uuid4() for _ in range(6))  # agentes
IA, IB, IC, ID, IE, IF = (uuid.uuid4() for _ in range(6))  # sus instancias
AGENTE_DE = {IA: A, IB: B, IC: C, ID: D, IE: E, IF: F}


def _enlace(desde: uuid.UUID, hacia: uuid.UUID, **kw: object) -> dict[str, object]:
    return {"from_agent_id": str(desde), "to_agent_id": str(hacia), "modes": ["delegate", "handoff", "query"], **kw}


def _grafo(*enlaces: dict[str, object]) -> GrafoCongelado:
    links = [EnlaceA2A.model_validate(e).canonico() for e in enlaces]
    return GrafoCongelado.desde_manifiesto({"links": links}, AGENTE_DE)


def _estado(**kw: object) -> EstadoDelTurno:
    return EstadoDelTurno(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), **kw)  # type: ignore[arg-type]


def _op(desde: uuid.UUID, hacia: uuid.UUID, modo: str = "delegate", **kw: object) -> Operacion:
    return Operacion(desde, hacia, modo, kw.pop("intencion", "cotizar"), **kw)  # type: ignore[arg-type]


def _pedir(estado: EstadoDelTurno, op: Operacion, grafo: GrafoCongelado, limites: Limites | None = None):  # type: ignore[no-untyped-def]
    decision = evaluar(estado, op, grafo, limites or Limites())
    aplicar(estado, op, decision)
    return decision


# ------------------------------------------------------------------ permisos
def test_sin_enlace_se_niega_con_no_link() -> None:
    d = _pedir(_estado(), _op(IA, IB), _grafo())
    assert not d.permitido and d.razon == "no_link"


def test_el_enlace_limita_modo_e_intencion() -> None:
    grafo = _grafo(_enlace(A, B, modes=["query"], intents=["stock"]))
    assert evaluar(_estado(), _op(IA, IB, "delegate", intencion="stock"), grafo, Limites()).razon == "no_link"
    assert evaluar(_estado(), _op(IA, IB, "query", intencion="precio"), grafo, Limites()).razon == "no_link"
    assert evaluar(_estado(), _op(IA, IB, "query", intencion="stock"), grafo, Limites()).permitido


def test_un_enlace_es_dirigido() -> None:
    grafo = _grafo(_enlace(A, B))
    assert evaluar(_estado(), _op(IA, IB), grafo, Limites()).permitido
    assert evaluar(_estado(), _op(IB, IA), grafo, Limites()).razon == "no_link"


def test_instancia_fuera_de_la_release_no_tiene_enlace() -> None:
    assert evaluar(_estado(), _op(IA, uuid.uuid4()), _grafo(_enlace(A, B)), Limites()).razon == "no_link"


# ------------------------------------------------------------------ caos
def test_ciclo_a_b_a_se_corta_y_cierra_el_turno() -> None:
    estado = _estado()
    grafo = _grafo(_enlace(A, B), _enlace(B, A))
    assert _pedir(estado, _op(IA, IB), grafo).permitido
    ciclo = _pedir(estado, _op(IB, IA), grafo)
    assert ciclo.razon == "cycle" and ciclo.pila == (IA, IB, IA)
    assert estado.cerrado == "loop_blocked"
    # Todo lo que sigue en el turno se niega, aunque tenga permiso.
    assert _pedir(estado, _op(IA, IB, intencion="otra"), grafo).razon == "turn_closed"


def test_autollamada_es_ciclo_aunque_no_haya_enlace() -> None:
    d = _pedir(_estado(), _op(IA, IA), _grafo(_enlace(A, B)))
    assert d.razon == "cycle"


def test_ciclo_largo_a_b_c_a() -> None:
    estado = _estado()
    grafo = _grafo(_enlace(A, B), _enlace(B, C), _enlace(C, A))
    assert _pedir(estado, _op(IA, IB), grafo).permitido
    assert _pedir(estado, _op(IB, IC), grafo).permitido
    assert _pedir(estado, _op(IC, IA), grafo).razon == "cycle"


def test_ping_pong_secuencial_se_frena_por_repeticion_y_por_llamadas() -> None:
    """A consulta a B, B vuelve; A vuelve a consultar lo mismo: `repeat`. Con payloads distintos,
    el turno se topa con `maxCallsPerTurn`, y las denegaciones también cuentan."""
    estado = _estado()
    grafo = _grafo(_enlace(A, B, budget_usd=0.001, budget_tokens=10))
    primera = _pedir(estado, _op(IA, IB, payload={"sku": 1}), grafo)
    assert primera.permitido
    estado.abiertas.pop(primera.call_id)
    estado.pila = [IA]
    assert _pedir(estado, _op(IA, IB, payload={"sku": 1}), grafo).razon == "repeat"
    razones = [_pedir(estado, _op(IA, IB, payload={"sku": n}), grafo).razon for n in range(2, 10)]
    # 2 llamadas ya contadas (la aceptada y el repeat): faltan 4 hasta 6 y luego `calls`.
    assert razones[4:] == ["calls"] * 4
    assert estado.llamadas == 10


def test_reintentar_lo_denegado_se_topa_con_el_limite() -> None:
    estado = _estado()
    grafo = _grafo()
    razones = [_pedir(estado, _op(IA, IB, payload={"n": n}), grafo).razon for n in range(8)]
    assert razones == ["no_link"] * 8  # no_link va antes que calls, pero todas cuentan
    assert estado.llamadas == 8
    grafo_ok = _grafo(_enlace(A, B))
    assert evaluar(estado, _op(IA, IB, payload={"n": 99}), grafo_ok, Limites()).razon == "calls"


def test_profundidad_hasta_el_limite_del_enlace_y_del_kernel() -> None:
    cadena = [IA, IB, IC, ID, IE, IF]
    agentes = [A, B, C, D, E, F]
    enlaces = [_enlace(agentes[i], agentes[i + 1], max_depth=5, budget_usd=0, budget_tokens=0) for i in range(5)]
    grafo = _grafo(*enlaces)
    estado = _estado()
    limites = Limites(max_depth=5)
    for i in range(5):
        d = _pedir(estado, _op(cadena[i], cadena[i + 1]), grafo, limites)
        assert d.permitido and d.depth == i + 1
    # Con el KernelSpec en 3, la cuarta llamada encadenada se niega por profundidad.
    estado = _estado()
    for i in range(3):
        assert _pedir(estado, _op(cadena[i], cadena[i + 1]), grafo).permitido
    d = _pedir(estado, _op(cadena[3], cadena[4]), grafo)
    assert d.razon == "depth" and d.depth == 4


def test_la_profundidad_registrada_nunca_pasa_de_5() -> None:
    """Una sexta llamada encadenada se niega por `depth` y el evento registra 5 (CHECK 1-5)."""
    g, ig = uuid.uuid4(), uuid.uuid4()
    agente_de = {**AGENTE_DE, ig: g}
    agentes = [A, B, C, D, E, F, g]
    links = [
        EnlaceA2A.model_validate(_enlace(agentes[i], agentes[i + 1], max_depth=5, budget_usd=0, budget_tokens=0)).canonico()
        for i in range(6)
    ]
    grafo = GrafoCongelado.desde_manifiesto({"links": links}, agente_de)
    estado = _estado(pila=[IA, IB, IC, ID, IE, IF])
    d = evaluar(estado, _op(IF, ig), grafo, Limites(max_depth=5))
    assert d.razon == "depth" and d.depth == 5


def test_handoff_respeta_el_freno_de_cambios_de_dueno() -> None:
    grafo = _grafo(_enlace(A, B))
    assert evaluar(_estado(cambios_de_dueno=1), _op(IA, IB, "handoff"), grafo, Limites()).permitido
    assert evaluar(_estado(cambios_de_dueno=2), _op(IA, IB, "handoff"), grafo, Limites()).razon == "owner_frozen"
    # Sin conteo (lectura fallida) el handoff se niega; una consulta no mira el dueño.
    assert evaluar(_estado(cambios_de_dueno=None), _op(IA, IB, "handoff"), grafo, Limites()).razon == "owner_frozen"
    assert evaluar(_estado(cambios_de_dueno=None), _op(IA, IB, "query"), grafo, Limites()).permitido


def test_clase_de_datos_que_el_enlace_no_permite() -> None:
    grafo = _grafo(_enlace(A, B))
    assert evaluar(_estado(), _op(IA, IB, data_classes=("pii",)), grafo, Limites()).razon == "data_class"


def test_presupuesto_reserva_el_del_enlace_contra_lo_que_queda_del_turno() -> None:
    """El Gherkin: un enlace de 0,02 USD con un turno de 0,05 deja pasar 2 y niega la tercera."""
    estado = _estado()
    grafo = _grafo(_enlace(A, B, budget_usd=0.02, budget_tokens=100), _enlace(A, C, budget_usd=0.02, budget_tokens=100))
    assert _pedir(estado, _op(IA, IB, payload={"n": 1}), grafo).permitido
    estado.pila = [IA]
    assert _pedir(estado, _op(IA, IC, payload={"n": 2}), grafo).permitido
    estado.pila = [IA]
    d = _pedir(estado, _op(IA, IB, payload={"n": 3}), grafo)
    assert d.razon == "budget" and estado.cerrado == "budget_exhausted"
    assert estado.reservado_usd == Decimal("0.04")


# ------------------------------------------------------------------ payload
def test_payload_hash_es_hmac_de_64_hex_y_estable() -> None:
    h = payload_hash({"b": 1, "a": [1, 2]})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    assert h == payload_hash({"a": [1, 2], "b": 1})
    assert h != payload_hash({"a": [1, 2], "b": 2})


def test_payload_redactado_solo_guarda_la_forma() -> None:
    red = redactar({"telefono": "3001234567", "3001234567": {"x": 1}, "mail@x.co": True, "lista": [{"a": 1}]})
    assert red == {
        "«clave1»": {"x": "number"},
        "«clave2»": "boolean",
        "lista": [{"a": "number"}],
        "telefono": "string",
    }
    profundo = redactar({"a": {"b": {"c": {"d": 1}}}})
    assert profundo == {"a": {"b": {"c": "object"}}}
    muchas = redactar({f"k{i:02d}": i for i in range(30)})
    assert len(muchas) == 21 and muchas["…"] == "+10"


# ------------------------------------------------------------------ grafo
def test_broadcast_y_colaboracion_no_van_en_un_enlace() -> None:
    for modo in ("broadcast", "collaborate"):
        with pytest.raises(ValidationError) as exc:
            EnlaceA2A.model_validate(_enlace(A, B, modes=[modo]))
        assert codigo_de_error(exc.value) == "modo_no_permitido"


def test_un_enlace_hacia_si_mismo_se_rechaza() -> None:
    with pytest.raises(ValidationError) as exc:
        EnlaceA2A.model_validate(_enlace(A, A))
    assert codigo_de_error(exc.value) == "autollamada"


def test_el_hash_del_grafo_no_depende_del_orden() -> None:
    uno = normalizar([_enlace(A, B), _enlace(B, C, modes=["query", "delegate"])])
    dos = normalizar([_enlace(B, C, modes=["delegate", "query"]), _enlace(A, B)])
    assert hash_de(uno) == hash_de(dos)
    assert hash_de(uno) != hash_de(normalizar([_enlace(A, B)]))


def test_advierte_ciclos_de_handoff_sin_bloquear() -> None:
    enlaces = normalizar([_enlace(A, B, modes=["handoff"]), _enlace(B, A, modes=["handoff"]), _enlace(B, C, modes=["query"])])
    assert advertencias(enlaces) == [f"ciclo_handoff:{min(str(A), str(B))}>{max(str(A), str(B))}"]


# ------------------------------------------------------------------ release
def _componente(aid: uuid.UUID, iid: uuid.UUID, key: str, rol: str) -> man.Componente:
    return man.Componente(str(iid), str(aid), key, rol, str(uuid.uuid4()), 1, "0" * 64, "agentspec/3")


def test_revisar_a2a_exige_que_el_enlace_no_pase_al_coordinador() -> None:
    core = _Instancia(_componente(C, IC, "coordinador", "core"), {"a2a": {"maxDepth": 3, "turnBudgetUsd": 0.05}})
    ventas = _Instancia(_componente(A, IA, "ventas", "specialist"), {})
    agenda = _Instancia(_componente(B, IB, "agenda", "specialist"), {})
    instancias = [core, ventas, agenda]
    componentes = [i.componente for i in instancias]
    bien = {"links": [EnlaceA2A.model_validate(_enlace(A, B)).canonico()]}
    assert revisar_a2a(instancias, componentes, bien) == []
    mal = {"links": [EnlaceA2A.model_validate(_enlace(A, B, max_depth=5, budget_usd=0.5)).canonico()]}
    problemas = revisar_a2a(instancias, componentes, mal)
    assert len(problemas) == 2 and problemas[0].startswith("ventas→agenda: max_depth 5 > 3")
    # Un enlace con un agente fuera de la conexión queda inerte: no se revisa.
    inerte = {"links": [EnlaceA2A.model_validate(_enlace(A, D, max_depth=5)).canonico()]}
    assert revisar_a2a(instancias, componentes, inerte) == []
    assert revisar_a2a(instancias, componentes, None) == []


def test_sin_grafo_el_manifiesto_y_su_hash_no_cambian() -> None:
    base = {
        "componentes": [_componente(C, IC, "coordinador", "core")],
        "habilidades": [],
        "plugins_del_negocio": [],
        "overrides": {},
        "flujo_del_negocio": None,
        "app_settings": {"language": "es"},
    }
    sin = man.armar(**base)
    assert "a2a_graph" not in sin
    con = man.armar(**base, a2a_graph={"version_id": str(uuid.uuid4()), "version": 1, "content_hash": "a" * 64, "links": []})
    assert man.hash_de(sin) != man.hash_de(con)
    assert man.resumen(con)["a2a_graph"] == {"version": 1, "content_hash": "a" * 64, "links": 0}
    assert man.resumen(sin)["a2a_graph"] is None
