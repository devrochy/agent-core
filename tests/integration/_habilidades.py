"""Habilidades del negocio en los tests de integración (HU-AOS-91 parte C, ADR-0206).

Una habilidad es del negocio solo si algún agente activo la tiene instalada: el alta ya no elige packs. Los tests que
antes pedían `onboard_tenant(packs=[...])` usan `onboard_tenant` de aquí, que da de alta el negocio sin packs y luego
instala esas habilidades en un especialista de prueba (`habilidades_de_prueba`), como lo hace el dueño al crear su
primer agente. El especialista no tiene versión publicada: no entra en ningún release ni atiende conversaciones.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any

KEY_DE_PRUEBA = "habilidades_de_prueba"


async def dar_habilidades(
    tenant_id: uuid.UUID, packs: Iterable[str], *, key: str = KEY_DE_PRUEBA, chequear: bool = True
) -> uuid.UUID:
    """Activa `packs` en el negocio y los instala en el especialista de prueba (lo crea si no existe). Devuelve su id.
    `chequear=False` instala sin el chequeo previo (cuota y activación): para medir la cuota misma. Es un fixture: no
    exige el `min_plan` de los packs (HU-BILL-19), que se prueba en `test_topes_estructurales_conexiones.py`."""
    from sqlalchemy import select

    from core.agents import versiones
    from core.agents.habilitacion import asegurar_habilidades
    from core.agents.instalaciones_agente import instalar_en_agente
    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    pedidos = sorted(set(packs))
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            agent_id = (
                await session.execute(
                    select(AgentRecord.id).where(
                        AgentRecord.tenant_id == tenant_id, AgentRecord.key == key, AgentRecord.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
        if agent_id is None:
            agent_id = (
                await versiones.crear_agente(tenant_id, key=key, kind="specialist", name="Habilidades de prueba")
            ).id
        if pedidos and chequear:
            await asegurar_habilidades(tenant_id, pedidos, actor=None, exigir_plan=False)
        for pack in pedidos:
            # Activa, también Pagos: es lo que daba la habilitación del negocio de antes.
            await instalar_en_agente(tenant_id, agent_id, pack)
    return agent_id


async def onboard_tenant(*args: Any, packs: list[str] | None = None, **kwargs: Any) -> tuple[uuid.UUID, list[str]]:
    """`core.alta_negocio.onboard_tenant` sin packs, más `dar_habilidades(packs)`."""
    from core.alta_negocio import onboard_tenant as alta

    tenant_id, pasos = await alta(*args, **kwargs)
    if packs:
        await dar_habilidades(tenant_id, packs)
        pasos = [*pasos, *(f"pack {p} instalado en {KEY_DE_PRUEBA}" for p in sorted(set(packs)))]
    return tenant_id, pasos


async def quitar_habilidades(tenant_id: uuid.UUID, packs: Iterable[str], *, key: str = KEY_DE_PRUEBA) -> None:
    """Quita `packs` del especialista de prueba: dejan de ser del negocio si ningún otro agente activo los tiene."""
    from sqlalchemy import select

    from core.agents.instalaciones_agente import InstalacionNoEncontrada, desinstalar_de_agente
    from core.db.models import AgentRecord
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tenant_id):
        async with get_session() as session:
            agent_id = (
                await session.execute(
                    select(AgentRecord.id).where(
                        AgentRecord.tenant_id == tenant_id, AgentRecord.key == key, AgentRecord.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()
        if agent_id is None:
            return
        for pack in sorted(set(packs)):
            try:
                await desinstalar_de_agente(tenant_id, agent_id, pack)
            except InstalacionNoEncontrada:  # ya no estaba: quitar es idempotente
                pass


def pack_de_pago(monkeypatch: Any, pack: str, plan: str) -> None:
    """Hace que `pack` pida `plan` como mínimo (D-79, ADR-0208 §10). Hoy todos los packs son Free (el candado es de
    superpoderes, decisión 2026-11-24): quien prueba el candado o el preset «pendiente por plan» simula un pack de pago."""
    from core.plugin_manager import plugin_manager

    if plugin_manager.get(pack) is None:
        plugin_manager.scan()
    monkeypatch.setattr(type(plugin_manager.get(pack)), "min_plan", plan)
