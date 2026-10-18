"""HU-AOS-16 · habilidades por scope y procedimientos por agente: reglas puras (sin base).

- rangos de versión de un `ProcedureRef` y su resolución contra lo que publica una release;
- el lector tolerante DECLARA en el General los procedimientos publicados (`^M.0`);
- `ReleaseCongelada`: procedimientos por agente, `procedimiento_para` por major, el `is_default`
  en `elegir_foco` (HU-AOS-15) y la regla transitoria con un manifiesto `release/1`;
- la oferta fail-closed por agente y por scope (`core/kernel/oferta.py`);
- la ranura del turno (el `foco` de HU-AOS-15) y `registro.disponibles`/`definicion` con una
  release.
Lo que necesita Postgres está en `tests/integration/test_habilidades_por_agente_loop.py`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import types
import uuid
from typing import Any

import pytest

from core.kernel import oferta
from core.kernel.procedures import registro
from core.kernel.release_del_turno import con_release
from core.releases import manifiesto as man
from core.releases import procedimientos as rp
from core.releases.cargar import ErrorDeRelease, ReleaseCongelada
from core.shared import release_vigente
from core.shared.release_vigente import AgenteCongelado

T = uuid.uuid4()
PLAN = "pack_commerce.contratar_plan"
CITA = "pack_calendar.agendar_cita"


def _proc(pid: str, version: str, *, requires: list[str] | None = None, etapas: list[dict[str, Any]] | None = None) -> Any:
    from agentcore_contracts import Procedure

    return Procedure.model_validate(
        {
            "id": pid,
            "version": version,
            "title": pid,
            "requires": requires or [],
            "stages": etapas or [{"id": "unica", "actions": ["informar_plan"]}],
        }
    )


# ---------------------------------------------------------------- rangos y resolución
@pytest.mark.parametrize(
    ("version", "rango", "esperado"),
    [
        ("1.2.0", "^1.0", True),
        ("1.0.0", "^1.0", True),
        ("2.0.0", "^1.0", False),
        ("1.2.0", "^1.3", False),
        ("0.2.5", "^0.2", True),
        ("0.3.0", "^0.2", False),
        ("1.2.9", "~1.2.3", True),
        ("1.3.0", "~1.2.3", False),
        ("1.2.0", "=1.2.0", True),
        ("1.2.1", "=1.2.0", False),
        ("1.2.0", "1.2.0", True),
        ("1.2.0", "1.2", True),
        ("1.2.1", "1.2", False),
    ],
)
def test_rangos_de_version(version: str, rango: str, esperado: bool) -> None:
    assert rp.cumple(version, rango) is esperado


def test_resolver_refs_abiertos_rangos_y_fail_closed() -> None:
    publicados = rp.publicados_por_id(
        [
            _proc(PLAN, "1.1.0"),
            _proc(PLAN, "1.2.0"),
            _proc(CITA, "1.0.0"),
            _proc("pack_cobranza.promesa_de_pago", "2.0.0", requires=["pack_subscriptions"]),
        ]
    )
    refs = [
        {"id": "core.saludo", "version": "=0.0.0"},
        {"id": PLAN, "version": "^1.0"},
        {"id": CITA, "version": "^1"},
        {"id": "pack_cobranza.promesa_de_pago", "version": "^2.0"},
        {"id": PLAN, "version": "^2.0"},
        {"id": "tenant.propia", "version": "0.0.0"},
    ]
    resueltos, problemas = rp.resolver_refs(
        refs, abiertos=["core.saludo"], publicados=publicados, habilidades=["pack_commerce", "pack_cobranza"]
    )
    # La versión más alta que cumple; la agenda no está instalada en el scope; la promesa requiere
    # una habilidad que no está; `^2.0` no lo provee nadie; la sección abierta ya no existe.
    assert [(r.id, r.version) for r in resueltos] == [("core.saludo", "0.0.0"), (PLAN, "1.2.0")]
    assert len(problemas) == 4
    assert any("agendar_cita" in p and "no está instalada" in p for p in problemas)
    assert any("promesa_de_pago" in p and "requiere" in p for p in problemas)
    assert any(f"{PLAN} ^2.0" in p for p in problemas)
    assert any("tenant.propia" in p and "sección" in p for p in problemas)


def test_el_general_declara_los_publicados_con_rango_compatible() -> None:
    from core.agents.tolerante import _declarados

    refs = _declarados([_proc(PLAN, "1.2.0"), _proc(PLAN, "1.1.0"), _proc(CITA, "1.0.0"), _proc("x.y", "2.3.1")], {"x.y"})
    assert refs == [{"id": CITA, "version": "^1.0"}, {"id": PLAN, "version": "^1.0"}]
    assert rp.rango_compatible("2.3.1") == "^2.0"


# ---------------------------------------------------------------- release congelada
def _iid(nombre: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"instancia/{nombre}")


def _agente(nombre: str, key: str, *, role: str = "specialist", default: bool = False, procs: Any = (), tools: list[str] | tuple[str, ...] = (), contract: str = "agentspec/3") -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=_iid(nombre),
        agent_id=uuid.uuid5(uuid.NAMESPACE_URL, f"agente/{nombre}"),
        key=key,
        role=role,
        version_id=uuid.uuid5(uuid.NAMESPACE_URL, f"version/{nombre}"),
        version=1,
        contract=contract,
        content={"tools": [{"tool": t, "enabled": True} for t in tools], "declarativeTools": [{"name": "consultar_envio"}]},
        is_default=default,
        procedimientos=procs,
    )


def _release(schema: str = man.SCHEMA, *, agentes: tuple[AgenteCongelado, ...] = (), skills: list[str] | None = None) -> ReleaseCongelada:
    congelados = {
        (PLAN, "1.2.0"): _proc(PLAN, "1.2.0", etapas=[{"id": "confirmar", "actions": ["confirmar_cotizacion"], "confirmation": "explicit", "exitWhen": {"action": "confirmar_cotizacion"}}]),
        (CITA, "1.0.0"): _proc(CITA, "1.0.0"),
        ("pack_cobranza.promesa_de_pago", "2.0.0"): _proc("pack_cobranza.promesa_de_pago", "2.0.0", requires=["pack_subscriptions"]),
    }
    release = ReleaseCongelada(
        id=uuid.uuid4(),
        tenant_id=T,
        connection_id=uuid.uuid4(),
        number=1,
        manifest_hash="h",
        manifest={"schema": schema, "skills": [{"pack_id": p} for p in (skills or ["pack_commerce", "pack_calendar", "pack_cobranza"])], "tenant_plugins": []},
        idle_minutes=720,
        bytes=10,
        componentes=agentes,
        congelados=congelados,
    )
    # Sin flujo del binario: los abiertos no importan para estas reglas.
    release._procedimientos = {k[0]: v for k, v in congelados.items()}
    release._procedimientos = {pid: p for pid, p in release._procedimientos.items() if rp.ofrecible(p, release.packs)}
    return release


def test_procedimientos_por_agente_y_foco_por_defecto() -> None:
    ventas = _agente("ventas", "ventas", default=True, procs=((PLAN, "1.2.0"),), tools=["informar_plan"])
    general = _agente("general", "general", procs=((PLAN, "1.2.0"), (CITA, "1.0.0"), ("core.saludo", "0.0.0")))
    core = _agente("core", "coordinador", role="core", procs=(), contract="kernelspec/1")
    release = _release(agentes=(core, ventas, general))
    # El `is_default` de la conexión gana sobre el `routing.defaultAgent` (sin KernelSpec: general).
    assert release.foco() == ventas
    assert set(release.procedimientos_de(ventas.instance_id)) == {PLAN}
    assert set(release.procedimientos_de(general.instance_id)) == {PLAN, CITA}  # sin los abiertos
    assert release.procedimientos_de(core.instance_id) == {} and release.procedimientos_de(uuid.uuid4()) == {}
    # La promesa requiere pack_subscriptions, que no está en la release: no es de la release.
    assert "pack_cobranza.promesa_de_pago" not in release.procedimientos()
    artefactos = release.artefactos("stable", foco=release.foco())
    assert artefactos.foco == ventas and artefactos.procedimientos_por_agente is not None
    assert set(artefactos.procedimientos_por_agente[general.instance_id]) == {PLAN, CITA}
    assert artefactos.skills == ("pack_commerce", "pack_calendar", "pack_cobranza")
    # Sin `is_default`: la elección de HU-AOS-15 (dueño → `routing.defaultAgent` → único).
    x = _agente("x", "x")
    kernel = dataclasses.replace(core, content={"routing": {"defaultAgent": "general"}})
    assert _release(agentes=(kernel, general, x)).foco() == general
    assert _release(agentes=(kernel, general, x)).foco(x.instance_id) == x  # el dueño manda
    assert _release(agentes=(core, x)).foco() == x
    with pytest.raises(ErrorDeRelease):
        _release(agentes=(core,)).foco()
    with pytest.raises(ErrorDeRelease):
        _release(agentes=(core, general, x)).foco()  # varios y ninguno por defecto


def test_release_uno_sin_procedimientos_por_agente_rige_la_regla_transitoria() -> None:
    general = _agente("general", "general", procs=None)
    release = _release(man.SCHEMA_SIN_PROCEDIMIENTOS, agentes=(general,))
    assert not release.con_procedimientos_por_agente
    assert release.procedimientos_de(general.instance_id) == {}
    assert release.artefactos("stable").procedimientos_por_agente is None
    # El manifiesto `release/2` sí lleva `is_default` y `procedures` en el hash.
    base = {
        "componentes": [man.Componente("i1", "a1", "general", "specialist", "v1", 1, "h1", "agentspec/3", True, ((PLAN, "1.2.0"),))],
        "habilidades": [man.Habilidad("pack_commerce", "2.3.0", "x")],
        "plugins_del_negocio": [],
        "overrides": {},
        "flujo_del_negocio": None,
        "app_settings": {"assistant_name": "Ana"},
    }
    m = man.armar(**base)
    assert m["schema"] == "release/2"
    assert m["components"][0]["procedures"] == [{"id": PLAN, "version": "1.2.0"}] and m["components"][0]["is_default"]
    otro = dict(base, componentes=[man.Componente("i1", "a1", "general", "specialist", "v1", 1, "h1", "agentspec/3", False, ((PLAN, "1.2.0"),))])
    assert man.hash_de(m) != man.hash_de(man.armar(**otro))


def test_procedimiento_para_sigue_con_el_mismo_major_y_no_con_otro() -> None:
    release = _release()
    assert release.procedimiento_para(PLAN, "1.1.0") is not None  # 1.1.0 → 1.2.0: revalidada
    assert release.procedimiento_para(PLAN, "1.2.0") is not None
    assert release.procedimiento_para(PLAN, "2.0.0") is None  # cambio de major: se cancela/escala
    assert release.procedimiento_para("pack_subscriptions.cambiar_plan", "1.0.0") is None  # desapareció
    assert rp.major("3.1.4") == 3


# ---------------------------------------------------------------- oferta por agente y por scope
def _tool(nombre: str, plugin: str | None) -> Any:
    return types.SimpleNamespace(name=nombre, plugin_id=plugin)


def test_oferta_fail_closed_por_agente_y_por_scope() -> None:
    tools = [
        _tool("informar_plan", "pack_commerce"),
        _tool("cotizar_plan", "pack_commerce"),
        _tool("check_availability", "pack_calendar"),
        _tool("escalate_to_human", "core"),
        _tool("consultar_envio", None),
        _tool("otra_declarativa", "declarative"),
        _tool("iniciar_procedimiento", "core"),
    ]
    agente = _agente("ventas", "ventas", tools=["informar_plan"])
    declaradas = oferta.declaradas_por(agente)
    assert declaradas == {"informar_plan", "consultar_envio"}
    ofrecidas = oferta.ofrecidas_para_el_agente(tools, packs={"pack_commerce"}, del_agente=declaradas, comandos={"iniciar_procedimiento"})
    assert [t.name for t in ofrecidas] == ["informar_plan", "escalate_to_human", "consultar_envio", "iniciar_procedimiento"]
    # Con foco, las acciones de la etapa pasan aunque la versión no las declare.
    con_etapa = oferta.ofrecidas_para_el_agente(tools, packs={"pack_commerce"}, del_agente=declaradas, de_la_etapa={"cotizar_plan"})
    assert "cotizar_plan" in {t.name for t in con_etapa}
    # Fuera del scope de la conexión no pasa ni declarándola.
    assert not oferta.permitida_para_el_agente("check_availability", "pack_calendar", packs={"pack_commerce"}, del_agente={"check_availability"})
    # Sin release (None) todo pasa; sin agente en foco, solo filtra el scope.
    assert len(oferta.ofrecidas_para_el_agente(tools, packs=None, del_agente=None)) == len(tools)
    assert oferta.declaradas_por(None) is None
    assert {t.name for t in oferta.ofrecidas_para_el_agente(tools, packs={"pack_calendar"}, del_agente=None)} == {
        "check_availability", "escalate_to_human", "consultar_envio", "otra_declarativa", "iniciar_procedimiento",
    }


# ---------------------------------------------------------------- ranura y registro
async def _en_scope(fn: Any) -> Any:
    from core.shared.tenant import tenant_scope

    async with tenant_scope(T):
        return await fn()


def test_foco_del_turno_y_registro_con_release(monkeypatch: Any) -> None:
    ventas = _agente("ventas", "ventas", default=True, procs=((PLAN, "1.2.0"),))
    agenda = _agente("agenda", "agenda", procs=((CITA, "1.0.0"),))
    release = _release(agentes=(ventas, agenda))
    monkeypatch.setattr(registro, "_valido", lambda proc: True)
    monkeypatch.setattr(registro, "_todos", lambda: [_proc(PLAN, "9.9.9"), _proc(CITA, "1.0.0")])

    @con_release
    async def turno() -> dict[str, Any]:
        assert release_vigente.procedimientos_del_agente() is None  # sin release fijada
        assert release_vigente.fijar(release.artefactos("stable", foco=release.foco()))
        salida: dict[str, Any] = {}
        salida["defecto"] = sorted(registro.disponibles(["pack_commerce", "pack_calendar"]))
        salida["definicion_release"] = registro.definicion(PLAN, "1.2.0").version
        salida["definicion_binario"] = registro.definicion(PLAN, "9.9.9").version
        salida["definicion_ninguna"] = registro.definicion(PLAN, "1.1.0")
        # Agenda es la dueña de la conversación: `elegir_foco` la mantiene en foco.
        assert release_vigente.fijar(release.artefactos("stable", foco=release.foco(agenda.instance_id)))
        salida["agenda"] = sorted(registro.disponibles(["pack_commerce", "pack_calendar"]))
        salida["agenda_roto"] = sorted(registro.disponibles(["pack_commerce", "pack_calendar"], ["pack_calendar"]))
        agente = release_vigente.agente_del_turno()
        salida["en_foco"] = agente.key if agente is not None else None
        return salida

    r = asyncio.run(_en_scope(turno))
    assert r["defecto"] == [PLAN] and r["agenda"] == [CITA] and r["agenda_roto"] == [] and r["en_foco"] == "agenda"
    assert (r["definicion_release"], r["definicion_binario"], r["definicion_ninguna"]) == ("1.2.0", "9.9.9", None)
    # Fuera del turno la ranura está vacía: el binario, como siempre.
    assert release_vigente.procedimientos_del_agente() is None
    assert release_vigente.agente_del_turno() is None


def test_sin_release_disponibles_sigue_el_binario(monkeypatch: Any) -> None:
    class _Host:
        def get_procedures(self) -> dict[str, list[Any]]:
            return {"pack_commerce": [_proc(PLAN, "1.2.0")], "pack_calendar": [_proc(CITA, "1.0.0")]}

    monkeypatch.setattr(registro, "mcp_host", _Host())
    monkeypatch.setattr(registro, "_valido", lambda proc: True)
    assert sorted(registro.disponibles(["pack_commerce"])) == [PLAN]
    assert sorted(registro.disponibles(["pack_commerce", "pack_calendar"], ["pack_calendar"])) == [PLAN]


def test_release_sin_agente_en_foco_es_fail_closed() -> None:
    """Una `release/2` sin especialista en foco no ofrece procedimientos (no cae al binario).
    En el turno real `elegir_foco` ya falla antes (`ErrorDeRelease`); la ranura es fail-closed
    igual si alguien la fija sin foco."""
    core = _agente("core", "coordinador", role="core", procs=(), contract="kernelspec/1")
    release = _release(agentes=(core,))

    @con_release
    async def turno() -> Any:
        assert release_vigente.fijar(release.artefactos("stable"))
        return release_vigente.agente_del_turno(), release_vigente.procedimientos_del_agente()

    en_foco, procs = asyncio.run(_en_scope(turno))
    assert en_foco is None and procs == {}


def test_rangos_mal_formados_no_cumplen() -> None:
    assert rp.cumple("1.x.0", "^1.0") is False
    assert rp.cumple("1.0.0", "^uno") is False


def test_veredicto_del_linter_por_contenido_no_por_id_del_objeto(monkeypatch: Any) -> None:
    """Dos procedimientos abiertos con el mismo id y versión pero distinto contenido (flujos de
    dos negocios) no comparten el veredicto aunque CPython reutilice la dirección del objeto."""
    llamadas: list[str] = []

    def _lint(proc: Any, _catalogo: Any) -> list[Any]:
        llamadas.append(proc.title)
        return [types.SimpleNamespace(code="x")] if proc.title == "malo" else []

    monkeypatch.setattr(registro, "lint", _lint)
    monkeypatch.setattr(registro, "catalogo_de_acciones", lambda _m: None)
    monkeypatch.setattr(registro, "_revisados", registro.OrderedDict())
    bueno = _proc("tenant.ventas", "0.0.0").model_copy(update={"title": "bueno"})
    assert registro._valido(bueno)
    direccion = id(bueno)
    del bueno
    malo = _proc("tenant.ventas", "0.0.0").model_copy(update={"title": "malo"})
    # Aunque reciba la misma dirección, el contenido decide: se vuelve a revisar y falla.
    assert not registro._valido(malo)
    assert llamadas == ["bueno", "malo"]
    assert registro._valido(_proc("tenant.ventas", "0.0.0").model_copy(update={"title": "bueno"}))
    assert llamadas == ["bueno", "malo"]  # mismo contenido: veredicto en caché
    del direccion


def test_revisados_acotado(monkeypatch: Any) -> None:
    monkeypatch.setattr(registro, "lint", lambda _p, _c: [])
    monkeypatch.setattr(registro, "catalogo_de_acciones", lambda _m: None)
    monkeypatch.setattr(registro, "_revisados", registro.OrderedDict())
    monkeypatch.setattr(registro, "_MAX_REVISADOS", 3)
    for i in range(5):
        registro._valido(_proc(f"tenant.p{i}", "0.0.0"))
    assert len(registro._revisados) == 3
