"""SPEC-48, HU-COM-23 · superpoder «Pago en línea» para pedidos existentes.

- El procedimiento valida con el linter y vive en el dominio «Formas de pago».
- No declara `requires: pack_payments`: esa habilidad no trae acciones y el linter lo rechaza. La puerta
  es la forma de pago (sin proveedor queda «Falta configurar» y no se ofrece) y `confirmar_pago_pedido`.
- Con `tomar_pedido` en foco, «ya pagué» no abre otro procedimiento: la tarea en foco manda.
- Las afirmaciones de link y de pago confirmado exigen el dato del servidor.
"""

from __future__ import annotations

import uuid
from typing import Any

from core.kernel.disparadores import Indice, exponentes_de
from core.kernel.procedures import de_pack
from core.kernel.router import Entrada, TareaEnFoco, decidir
from core.shared.release_vigente import AgenteCongelado

PAGO = "pack_commerce.pago_en_linea"
PEDIDO = "pack_commerce.tomar_pedido"


def _proc(pid: str) -> Any:
    return next(p for p in de_pack.cargar("pack_commerce") if p.id == pid)


def test_valida_con_el_linter_y_es_del_dominio_formas_de_pago() -> None:
    from pack_commerce.module import CRMModule

    from core.kernel.procedures import catalogo_de_acciones, lint
    from core.plugin_manager import plugin_manager

    plugin_manager.scan()
    proc = _proc(PAGO)
    assert lint(proc, catalogo_de_acciones(plugin_manager.list_modules())) == []
    assert proc.requires == []
    assert [s.id for s in proc.stages] == ["elegir_pedido", "enviar_link", "esperar_pago", "cerrar"]
    assert CRMModule.grupos_de_procedimientos[PAGO] == "formas_de_pago"


def test_sin_proveedor_la_forma_queda_falta_configurar() -> None:
    from pack_commerce.formas_de_pago import estados

    pago = {e.method: e for e in estados(None, [], False)}["pago_en_linea"]
    assert pago.superpoder == PAGO and pago.estado == "apagada"
    guardadas = [{"method": "efectivo_contraentrega", "enabled": True}, {"method": "pago_en_linea", "enabled": True}]
    pago = {e.method: e for e in estados(guardadas, [], False)}["pago_en_linea"]
    assert pago.estado == "falta_configurar"


def test_afirmaciones_respaldadas_por_el_servidor() -> None:
    claims = {c.kind: c for c in _proc(PAGO).claims}
    assert claims["link"].mustMatch == ["slot:link"]
    assert claims["status"].requires == "slot:pago_confirmado"


def _agente(procs: list[str]) -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key="ventas", role="specialist", version_id=uuid.uuid4(),
        version=1, contract="agentspec/3", content={}, is_default=True, procedimientos=tuple((p, "1.0.0") for p in procs),
    )


class _Release:
    def __init__(self, agente: AgenteCongelado) -> None:
        self.componentes = (agente,)
        self.con_procedimientos_por_agente = True
        procs = {p.id: p for p in de_pack.cargar("pack_commerce")}
        self._indice = Indice(exponentes_de(procs))

    def indice_de_disparadores(self) -> tuple[Indice, dict[str, str]]:
        return self._indice, {PAGO: "ventas", PEDIDO: "ventas"}


def test_la_compra_en_curso_no_se_la_roba_el_pago() -> None:
    from agentcore_contracts.kernelspec import Routing

    ventas = _agente([PEDIDO, PAGO])
    release: Any = _Release(ventas)
    routing = Routing.model_validate({"defaultAgent": "ventas"})
    tarea = TareaEnFoco(uuid.uuid4(), PEDIDO, "entrega", ventas.instance_id)
    ruta = decidir(release, routing, Entrada(texto="ya pagué, me reenvías el link de pago", tarea=tarea), ventas)
    assert (ruta.source, ruta.focus_task_id, ruta.start) == ("task", tarea.task_id, None)
    # Sin tarea en foco, el mismo texto sí elige el superpoder de pago.
    sin_foco = decidir(release, routing, Entrada(texto="no me llegó el link de pago"), ventas)
    assert sin_foco.procedure_id == PAGO
