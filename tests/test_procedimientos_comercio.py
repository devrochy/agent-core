"""HU-AOS-07 · procedimientos de comercio: archivos del pack, linter, ligadura de argumentos,
invalidación al cambiar un dato y lo que NO cambia con las banderas apagadas (unitarios).

Lo que corre contra el loop real y la base (Gherkin) está en
`tests/integration/test_procedimientos_de_comercio.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from agentcore_contracts import Procedure

from core.kernel.procedures import de_pack, motor, textos
from core.kernel.state import Evento, Proyeccion

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def _procs() -> dict[str, Procedure]:
    return {p.id: p for p in de_pack.cargar("pack_commerce")}


def _plan() -> Procedure:
    return _procs()["pack_commerce.contratar_plan"]


def _pedido() -> Procedure:
    return _procs()["pack_commerce.tomar_pedido"]


def _dato(valor: Any, seq: int, *, fuente: str = "llm", confirmado: bool = False) -> dict[str, Any]:
    return {"value": valor, "source": fuente, "seq": seq, "confirmed": confirmado, "data_class": "public"}


def _tarea(stage: str, slots: dict[str, Any], proc_id: str = "pack_commerce.contratar_plan") -> Proyeccion:
    return Proyeccion(
        procedure_id=proc_id, procedure_version="1.0.0", status="active", stage=stage, stack_position=0,
        last_seq=max([1, *(int(m["seq"]) for m in slots.values())]), created_at=T0, updated_at=T0, slots=slots,
    )


# ---------------------------------------------------------------- archivos, linter y manifiesto


def test_el_pack_publica_dos_procedimientos_validos_contra_el_binario() -> None:
    from core.kernel.procedures import catalogo_de_acciones, revisar
    from core.plugin_manager import plugin_manager

    catalogo = catalogo_de_acciones(plugin_manager.scan())
    crudos = de_pack.crudos("pack_commerce")
    assert [c["id"] for c in crudos] == ["pack_commerce.contratar_plan", "pack_commerce.tomar_pedido"]
    for crudo in crudos:
        assert revisar(crudo, catalogo) == [], crudo["id"]
    assert set(_procs()) == {c["id"] for c in crudos}


def test_el_manifiesto_congela_los_procedimientos_que_se_registran() -> None:
    from core.plugin_manager import plugin_manager
    from core.releases.packs import manifiesto_de_pack

    modulos = {m.id: m for m in plugin_manager.scan()}
    comercio = manifiesto_de_pack(modulos["pack_commerce"])
    assert comercio.version == "2.1.0"
    assert comercio.manifest["provides"]["procedures"] == de_pack.crudos("pack_commerce")
    assert manifiesto_de_pack(modulos["pack_payments"]).manifest["provides"]["procedures"] == []


def test_en_confirmar_ninguna_accion_cambia_el_total_sin_cambiar_el_id() -> None:
    """La confirmación se ata a los args (`quote_id`), no al total: en `confirmar` no puede haber
    una acción que mute la cotización vigente (`aplicar_promocion`)."""
    for proc in _procs().values():
        confirmar = motor.etapa(proc, "confirmar")
        assert confirmar is not None and confirmar.confirmation == "interactive_button"
        assert "aplicar_promocion" not in confirmar.actions


def test_los_textos_nuevos_del_motor_estan_en_su_playbook() -> None:
    textos.mensajes.cache_clear()
    assert "{dato}" not in textos.mensaje("argumento_ligado", dato="quote_id")
    assert "quote_id" in textos.mensaje("argumento_ligado", dato="quote_id")
    assert "email" in textos.mensaje("dato_bloqueado", dato="email")


# ---------------------------------------------------------------- banderas apagadas


def test_cotizar_plan_no_esta_en_el_flujo_ni_en_las_acciones_del_general() -> None:
    """Con el pack habilitado hay flujo: el loop y el golden solo ofrecen lo que nombra el flujo
    más las `always_available`. `cotizar_plan` no entra (ni en el General del lector tolerante)."""
    from core.agents.flow import all_pack_ids, compose_flow, load_core_flow, load_pack_flows
    from core.agents.tolerante import acciones_ofrecidas
    from core.mcp.host import mcp_host

    secciones = compose_flow(
        load_core_flow(), load_pack_flows(["pack_commerce"]), [], all_defaults=load_pack_flows(all_pack_ids())
    )
    assert secciones
    assert "cotizar_plan" not in {t for s in secciones for t in s.tools}
    from pack_commerce.skills import OrderSkill

    candidatas = [t.name for t in OrderSkill().get_tools()]
    assert "cotizar_plan" in candidatas
    siempre = {t.name for t in mcp_host.get_tools() if t.always_available}
    assert "cotizar_plan" not in acciones_ofrecidas(secciones, candidatas, siempre)


# ---------------------------------------------------------------- ligadura de argumentos

_ESQUEMA_PEDIDO = {
    "type": "object",
    "properties": {
        "quote_id": {"type": "string"},
        "payment_method": {"type": "string"},
        "customer_data": {
            "type": "object",
            "properties": {"email": {"type": "string"}, "phone": {"type": "string"}, "full_name": {"type": "string"}},
        },
    },
    "required": ["quote_id", "payment_method", "customer_data"],
}


def test_completa_lo_obligatorio_y_canoniza_lo_igual() -> None:
    tarea = _tarea("pagar", {
        "quote_id": _dato("q-1", 5, fuente="action:cotizar_plan"),
        "email": _dato("ana@example.com", 7),
        "payment_method": _dato("transferencia", 8),
    })
    lig = motor.ligar_argumentos(_plan(), tarea, _ESQUEMA_PEDIDO, {
        "payment_method": "transferencia", "customer_data": {"email": "ANA@Example.com ", "full_name": "Ana"},
    })
    assert lig.distintos == ()
    assert lig.args == {
        "quote_id": "q-1", "payment_method": "transferencia",
        "customer_data": {"email": "ana@example.com", "full_name": "Ana"},
    }


def test_un_valor_distinto_queda_para_el_resolver_o_se_niega() -> None:
    tarea = _tarea("confirmar", {"quote_id": _dato("q-nueva", 9, fuente="action:cotizar_plan")})
    lig = motor.ligar_argumentos(_plan(), tarea, {"properties": {"quote_id": {}}, "required": ["quote_id"]},
                                 {"quote_id": "q-vieja"})
    assert lig.distintos == (motor.Distinto("quote_id", None, "q-vieja"),)
    anidado = motor.ligar_argumentos(_plan(), _tarea("pagar", {"email": _dato("a@b.co", 3)}), _ESQUEMA_PEDIDO,
                                     {"customer_data": {"email": "otro@b.co"}})
    assert anidado.distintos == (motor.Distinto("email", "customer_data", "otro@b.co"),)


def test_no_completa_argumentos_opcionales_ni_toca_lo_que_no_es_dato() -> None:
    """`informar_plan(sku | variant_id)` son alternativas: completar `variant_id` cambiaría la consulta."""
    tarea = _tarea("cotizar", {"variant_id": _dato("v-premium", 3)})
    esquema = {"properties": {"sku": {"type": "string"}, "variant_id": {"type": "string"}}}
    lig = motor.ligar_argumentos(_plan(), tarea, esquema, {"sku": "PLAN-BASIC"})
    assert lig.args == {"sku": "PLAN-BASIC"} and lig.distintos == ()
    # Sin valor en la tarea no hay ligadura.
    vacia = motor.ligar_argumentos(_plan(), _tarea("cotizar", {}), _ESQUEMA_PEDIDO, {"quote_id": "q"})
    assert vacia.args == {"quote_id": "q"}


def test_crea_el_objeto_obligatorio_si_hay_datos_que_ponerle() -> None:
    tarea = _tarea("pagar", {"email": _dato("a@b.co", 3)})
    lig = motor.ligar_argumentos(_plan(), tarea, _ESQUEMA_PEDIDO, {"quote_id": "q"})
    assert lig.args["customer_data"] == {"email": "a@b.co"}


def test_ligadura_de_la_direccion_confirmada_del_pedido() -> None:
    tarea = _tarea("pagar", {"address": _dato("Calle 1 # 2-3", 4, confirmado=True)},
                   "pack_commerce.tomar_pedido")
    esquema = {"properties": {"customer_data": {"type": "object", "properties": {"address": {}}}},
               "required": ["customer_data"]}
    assert motor.ligar_argumentos(_pedido(), tarea, esquema, {"customer_data": {}}).args == {
        "customer_data": {"address": "Calle 1 # 2-3"}
    }
    assert motor.ligar_argumentos(_pedido(), tarea, esquema, {"customer_data": {"address": "Otra 9"}}).distintos


# ---------------------------------------------------------------- cambio de opinión


def _en_confirmar() -> Proyeccion:
    return _tarea("confirmar", {
        "variant_id": _dato("v-premium-m", 3),
        "quote_id": _dato("q-1", 5, fuente="action:cotizar_plan"),
        "requiere_pago": _dato(True, 6, fuente="action:cotizar_plan"),
        "pide_email": _dato(True, 7, fuente="action:cotizar_plan"),
        "email": _dato("a@b.co", 9, confirmado=True),
        "payment_method": _dato("transferencia", 10),
    })


def test_cambiar_el_plan_invalida_la_cotizacion_y_vuelve_a_cotizar() -> None:
    tarea = _en_confirmar()
    viejos = motor.dependientes(tarea, 3, frozenset({"variant_id"}))
    assert sorted(viejos) == ["pide_email", "quote_id", "requiere_pago"]
    sin = {k: v for k, v in tarea.slots.items() if k not in viejos}
    destino = motor.etapa_de_retroceso(_plan(), _tarea("confirmar", sin))
    assert destino is not None and destino.id == "cotizar"


def test_un_dato_que_no_rompe_etapas_anteriores_no_retrocede() -> None:
    assert motor.etapa_de_retroceso(_plan(), _en_confirmar()) is None


def test_la_direccion_sin_confirmar_devuelve_a_la_entrega() -> None:
    tarea = _tarea("confirmar", {
        "quote_id": _dato("q", 2, fuente="action:generar_cotizacion"),
        "delivery_method": _dato("domicilio", 3), "payment_method": _dato("transferencia", 4),
        "address": _dato("Otra 9", 8),
    }, "pack_commerce.tomar_pedido")
    destino = motor.etapa_de_retroceso(_pedido(), tarea)
    assert destino is not None and destino.id == "entrega"


def test_tras_el_efecto_de_riesgo_alto_los_datos_se_congelan() -> None:
    pagar = motor.etapa(_plan(), "pagar")
    confirmar = motor.etapa(_plan(), "confirmar")
    assert pagar is not None and confirmar is not None
    hecho = [Evento(seq=20, type="action_succeeded", data={"action": "crear_pedido"}, actor="tool", created_at=T0)]
    fallo = [Evento(seq=20, type="action_failed", data={"action": "crear_pedido"}, actor="tool", created_at=T0)]
    assert motor.datos_congelados(pagar, hecho)
    assert not motor.datos_congelados(pagar, fallo)  # antes del efecto se puede cambiar el método de pago
    assert not motor.datos_congelados(confirmar, hecho)


# ---------------------------------------------------------------- punto de extensión del resumen


def test_register_resumen_solo_para_una_tool_del_mismo_plugin() -> None:
    from core.mcp.host import MCPHost
    from core.mcp.types import BaseSkill, MCPTool
    from core.shared.exceptions import PluginError

    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return {}

    async def resumen(args: dict[str, Any], ctx: Any) -> None:
        return None

    class _Skill(BaseSkill):
        name = "s_resumen"

        def get_tools(self) -> list[MCPTool]:
            return [MCPTool(name="accion_r", description="x", parameters={}, handler=handler)]

    host = MCPHost()
    host.begin_activation("pack_a")
    host.register_skill(_Skill())
    host.register_resumen("accion_r", resumen)
    with pytest.raises(PluginError):
        host.register_resumen("accion_r", resumen)  # una por acción
    host.end_activation()
    host.begin_activation("pack_b")
    with pytest.raises(PluginError):
        host.register_resumen("accion_r", resumen)  # tool de otro plugin
    host.end_activation()
    assert host.get_resumen("accion_r") == (resumen, "pack_a")
    host.remove_plugin("pack_a")
    assert host.get_resumen("accion_r") is None


# ---------------------------------------------------------------- revisión de seguridad


def test_un_id_del_servidor_que_la_tarea_aun_no_tiene_no_lo_pone_el_modelo() -> None:
    """En `pagar`, antes de `crear_pedido`, `confirmar_pago_pedido(order_id=<pedido anterior>)`
    emitiría el link de otra compra: el valor del modelo se niega."""
    tarea = _tarea("pagar", {"quote_id": _dato("q-1", 5, fuente="action:cotizar_plan")})
    esquema = {"properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}
    lig = motor.ligar_argumentos(_plan(), tarea, esquema, {"order_id": "pedido-viejo"})
    assert lig.distintos == (motor.Distinto("order_id", None, "pedido-viejo"),)
    # Un dato que escribe el cliente (sin `setBy`) y aún no tiene valor no se ata.
    libre = motor.ligar_argumentos(_plan(), _tarea("datos", {}), {"properties": {"email": {}}}, {"email": "a@b.co"})
    assert libre.distintos == () and libre.args == {"email": "a@b.co"}


def test_un_cambio_de_etapa_vence_la_confirmacion_dada() -> None:
    def e(seq: int, tipo: str, **data: Any) -> Evento:
        return Evento(seq=seq, type=tipo, data=data, actor="kernel", created_at=T0)

    confirmado = e(10, "confirmed", target={"kind": "action", "name": "confirmar_cotizacion"}, args_hash="h")
    assert motor.accion_confirmada([confirmado], "confirmar_cotizacion", "h")
    ida_y_vuelta = [
        confirmado,
        e(11, "stage_entered", from_stage="confirmar", to_stage="entrega"),
        e(12, "stage_entered", from_stage="entrega", to_stage="confirmar"),
    ]
    assert not motor.accion_confirmada(ida_y_vuelta, "confirmar_cotizacion", "h")


def test_la_huella_del_resumen_cambia_con_el_total() -> None:
    from core.mcp.types import DatoDeResumen

    antes = [DatoDeResumen("total", 600000, es_monto=True), DatoDeResumen("plan", "Plan Premium")]
    despues = [DatoDeResumen("total", 540000, es_monto=True), DatoDeResumen("plan", "Plan Premium")]
    assert motor.huella_de_resumen(antes) == motor.huella_de_resumen(list(antes))
    assert motor.huella_de_resumen(antes) != motor.huella_de_resumen(despues)
    pedida = Evento(seq=3, type="confirmation_requested", actor="kernel", created_at=T0,
                    data={"target": {"kind": "action", "name": "confirmar_cotizacion"}, "args_hash": "h",
                          "resumen": motor.huella_de_resumen(antes)})
    assert motor.huella_pedida([pedida], "confirmar_cotizacion", "h") == motor.huella_de_resumen(antes)
    assert motor.huella_pedida([pedida], "confirmar_cotizacion", "otro") is None
