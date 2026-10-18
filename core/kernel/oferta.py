"""Predicados de oferta de tools según el motor de procedimientos (HU-AOS-08, ADR-0112).

Se componen con el `connection_predicate` de una tool (lo evalúa el loop por turno, antes de armar
las candidatas del motor):

- `sin_motor_activo`: la tool de siempre que el procedimiento reemplaza (`sub_cambiar_plan`,
  `cob_registrar_promesa_pago`) no se ofrece con `tenants.aos_procedures` encendida: el modelo no
  puede saltarse el botón con ella. Si la bandera no se puede leer, se ofrece como siempre (con la
  bandera apagada nada cambia; adenda HU-AOS-08 de ADR-0113).
- `solo_con_motor_activo`: una acción que solo existe para un procedimiento no se ofrece sin la
  bandera, aunque un flujo del negocio la nombre. Si la bandera no se puede leer, no se ofrece.

Leen la bandera del negocio del contexto (`get_current_tenant_id()`) en cada evaluación.

HU-AOS-16 (fail-closed por agente y por scope, ADR-0118 §2 y §3): con una release en el turno y
el motor activo, `ofrecidas_para_el_agente` deja pasar solo las acciones de las habilidades de
la release y, de ellas, las del core, los comandos del hilo, las de la etapa en foco y las que
la versión del agente en foco declara (`tools` habilitadas y sus `declarativeTools`). Es una
sola regla para cualquier agente: el General del lector tolerante declara todas las que hoy
ofrece el loop, así nada cambia hasta que el dueño cree especialistas.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from core.shared.logger import get_logger

log = get_logger(__name__)

Predicado = Callable[[set[str]], Awaitable[bool]]


async def _siempre(_tipos: set[str]) -> bool:
    return True


async def motor_activo() -> bool | None:
    """¿El negocio del contexto tiene `aos_procedures`? None si no se pudo leer o no hay negocio."""
    from core.shared.tenant import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    try:
        from core.db.session import get_session
        from core.kernel.banderas import banderas_del_negocio

        async with get_session() as session:
            return (await banderas_del_negocio(session, tenant_id)).procedimientos
    except Exception:
        log.warning("oferta_bandera_ilegible", exc_info=True)
        return None


CORE_OWNER = "core"


def permitida_para_el_agente(
    nombre: str,
    plugin_id: str | None,
    *,
    packs: Iterable[str] | None,
    del_agente: Iterable[str] | None,
    de_la_etapa: Iterable[str] = (),
    comandos: Iterable[str] = (),
) -> bool:
    """¿La acción `nombre` (de `plugin_id`; None = declarativa del negocio) se ofrece al agente?
    `packs`: habilidades de la release (None = sin release: todo pasa). `del_agente`: lo que su
    versión declara (None = sin agente en foco: todo pasa)."""
    if plugin_id == CORE_OWNER or nombre in set(comandos):
        return True
    if packs is not None and plugin_id is not None and plugin_id not in set(packs):
        return False
    if del_agente is None:
        return True
    return nombre in set(del_agente) or nombre in set(de_la_etapa)


def ofrecidas_para_el_agente(
    tools: Iterable[Any],
    *,
    packs: Iterable[str] | None,
    del_agente: Iterable[str] | None,
    de_la_etapa: Iterable[str] = (),
    comandos: Iterable[str] = (),
) -> list[Any]:
    """Las `tools` (objetos con `name` y `plugin_id`) que pasan `permitida_para_el_agente`."""
    return [
        t
        for t in tools
        if permitida_para_el_agente(
            t.name,
            getattr(t, "plugin_id", None),
            packs=packs,
            del_agente=del_agente,
            de_la_etapa=de_la_etapa,
            comandos=comandos,
        )
    ]


def declaradas_por(agente: Any) -> frozenset[str] | None:
    """Lo que la versión de un `AgenteDeRelease` declara: `tools` habilitadas y declarativas.
    None sin agente (sin release en el turno)."""
    if agente is None:
        return None
    return frozenset(agente.tools) | frozenset(agente.declarativas)


def sin_motor_activo(predicado: Predicado | None = None) -> Predicado:
    base = predicado or _siempre

    async def _predicado(tipos: set[str]) -> bool:
        if not await base(tipos):
            return False
        return await motor_activo() is not True

    return _predicado


def solo_con_motor_activo(predicado: Predicado | None = None) -> Predicado:
    base = predicado or _siempre

    async def _predicado(tipos: set[str]) -> bool:
        if not await base(tipos):
            return False
        return await motor_activo() is True

    return _predicado
