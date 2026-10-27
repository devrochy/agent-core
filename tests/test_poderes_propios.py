"""HU-AOS-51 / D-49 (SPEC-39): cada agente tiene solo los poderes de lo suyo (sin base).

Parte de la prueba de fuga: con el catálogo REAL de acciones del binario, un agente derivado de una
plantilla no hereda tools de otro enfoque. Las esenciales del core (escalar, perfil, memoria) siguen."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.presets import load_plantilla
from core.presets.agentes import derivar_contenido, herramienta_propia, herramientas_de_plantilla

TODOS_LOS_PACKS = {"pack_commerce", "pack_payments", "pack_calendar", "pack_cobranza", "pack_subscriptions"}
ESENCIALES_DEL_CORE = {"escalate_to_human", "get_customer_profile", "update_customer_profile", "remember_fact"}


@pytest.fixture(scope="module")
def catalogo() -> Any:
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager

    return catalogo_de_acciones(plugin_manager.scan())


def _general_con_todo(catalogo: Any) -> dict[str, Any]:
    """Un General como el de hoy: un binding por cada acción del binario."""
    return {
        "model": {"primary": "modelo-x", "temperature": 0.2},
        "persona": {"inheritConnectionVoice": True, "maxLength": 400, "forbiddenPhrases": []},
        "memory": {"shortTermTurns": 20, "writeNamespaces": ["perfil"]},
        "policies": {"maxToolCallsPerTurn": 7},
        "playbooks": [],
        "tools": [{"tool": nombre} for nombre in sorted(catalogo)],
        "guardrails": [],
        "declarativeTools": [],
    }


def _publicados() -> list[Any]:
    from core.kernel.procedures.de_pack import cargar

    return [p for pack in sorted(TODOS_LOS_PACKS) for p in cargar(pack)]


def _tools_de(key: str, catalogo: Any, *, proveedor: str | None = None) -> set[str]:
    contenido, _ = derivar_contenido(
        load_plantilla(key),
        _general_con_todo(catalogo),
        catalogo=catalogo,
        habilitados=TODOS_LOS_PACKS,
        publicados=_publicados(),
        playbooks_de_packs=set(),
        proveedor_suscripciones=proveedor,
    )
    return {b["tool"] for b in contenido["tools"]}


def _de_pack(catalogo: Any, pack: str) -> set[str]:
    return {n for n, i in catalogo.items() if i.owner == pack}


def test_las_sub_son_las_unicas_del_core_con_enfoque(catalogo: Any) -> None:
    """Guarda contra deriva: toda tool `sub_*` del binario es del core y viene de SubscriptionSkill, y
    ninguna del core con otro nombre pertenece a Suscripciones."""
    from core.subscriptions.tools import SubscriptionSkill

    de_skill = {t.name for t in SubscriptionSkill().get_tools()}
    assert de_skill and all(n.startswith("sub_") for n in de_skill)
    assert {n for n, i in catalogo.items() if i.owner == "core" and n.startswith("sub_")} == de_skill


@pytest.mark.parametrize("key", ["ventas", "agenda", "cobranza"])
def test_solo_suscripciones_recibe_las_sub(catalogo: Any, key: str) -> None:
    """La fuga que el informe marcó «a verificar»: sin la regla, un Ventas heredaba `sub_*` del General."""
    sub = {n for n in catalogo if n.startswith("sub_")}
    assert sub
    assert _tools_de(key, catalogo) & sub == set(), key
    assert _tools_de("suscripciones", catalogo) >= sub


def test_cobranza_no_hereda_la_familia_de_suscripcion_salvo_que_sea_proveedor(catalogo: Any) -> None:
    familia = {n for n in _de_pack(catalogo, "pack_cobranza") if "_suscripcion" in n}
    assert familia
    sin = _tools_de("cobranza", catalogo)
    assert sin & familia == set()
    assert "cob_consultar_saldo" in sin and "cob_registrar_promesa_saldo" in sin
    assert _tools_de("cobranza", catalogo, proveedor="pack_cobranza") >= familia
    assert _tools_de("cobranza", catalogo, proveedor="pack_subscriptions") & familia == set()


@pytest.mark.parametrize("key", ["ventas", "agenda", "cobranza", "suscripciones"])
def test_conserva_las_esenciales_del_core_y_nada_de_otro_pack(catalogo: Any, key: str) -> None:
    plantilla = load_plantilla(key)
    tools = _tools_de(key, catalogo, proveedor="pack_subscriptions")
    assert ESENCIALES_DEL_CORE & set(catalogo) <= tools
    packs_de_tools = {catalogo[t].owner for t in tools} - {"core"}
    permitidos = set(plantilla.habilidades) | set(plantilla.opcionales)
    assert packs_de_tools <= permitidos, (key, packs_de_tools - permitidos)


def test_agenda_solo_toma_crear_lead_de_commerce(catalogo: Any) -> None:
    tools = _tools_de("agenda", catalogo)
    de_commerce = {t for t in tools if catalogo[t].owner == "pack_commerce"}
    assert de_commerce == {"crear_lead"}


def test_herramientas_de_plantilla_lista_las_de_pack_sin_las_esenciales(catalogo: Any) -> None:
    ventas = herramientas_de_plantilla(load_plantilla("ventas"), catalogo, habilitados=TODOS_LOS_PACKS)
    assert "crear_pedido" in ventas and "generar_cotizacion" in ventas
    assert not (set(ventas) & ESENCIALES_DEL_CORE) and not {n for n in ventas if n.startswith("sub_")}
    suscripciones = herramientas_de_plantilla(load_plantilla("suscripciones"), catalogo, habilitados=TODOS_LOS_PACKS)
    assert "sub_consultar" in suscripciones and "crear_pedido" not in suscripciones


def test_herramienta_desconocida_no_es_de_nadie() -> None:
    plantilla = load_plantilla("ventas")
    assert not herramienta_propia(plantilla, "no_existe", {}, habilitados=set())
    catalogo = {"sub_consultar": SimpleNamespace(owner="core")}
    assert not herramienta_propia(plantilla, "sub_consultar", catalogo, habilitados={"pack_commerce"})
