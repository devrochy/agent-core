"""HU-AOS-61 · habilidades instaladas y configuradas por agente (ADR-0190 §2-§3, migración 0125).

Sin LLM ni red. Criterios cubiertos:
- **Instalar en un agente:** la fila `scope='agent'` lleva su `agent_id` y el otro agente no la tiene.
- **Reinstalar:** quitar (borrado lógico) y volver a instalar no choca con el índice único.
- **Configuración del agente:** queda cifrada en la instalación y `config_del_agente` la devuelve; no es texto plano.
- **Esquema:** el CHECK rechaza `scope='agent'` sin agente y la FK compuesta rechaza el agente de otro negocio.
- **Agente archivado:** no se le instalan habilidades.
- **Aislamiento:** desde otro negocio no se ven las instalaciones por agente.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from _api_helpers import uniq
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


@pytest.fixture(scope="module", autouse=True)
async def _packs_registrados() -> None:
    from core.plugin_manager import plugin_manager

    await plugin_manager.sync_registry()


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("hpa")
    tid, _ = await onboard_tenant(name=f"HPA {slug}", slug=slug, packs=[], sembrar_ejemplos=False, plan="premium")
    return tid


async def _agente(tid: uuid.UUID, *, estado: str = "active") -> uuid.UUID:
    aid = uuid.uuid4()
    await _sql(
        tid,
        "insert into agents (id, tenant_id, key, kind, name, status) values (:a, :t, :k, 'specialist', 'Ventas', :s)",
        a=aid, t=tid, k=f"ventas_{uuid.uuid4().hex[:8]}", s=estado,
    )
    return aid


async def test_instalar_solo_en_ese_agente_y_reinstalar() -> None:
    from core.agents.instalaciones_agente import (
        desinstalar_de_agente,
        instaladas_en_agente,
        instalar_en_agente,
    )
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    uno, otro = await _agente(tid), await _agente(tid)
    async with tenant_scope(tid):
        vista, creada = await instalar_en_agente(tid, uno, "pack_cobranza")
        assert creada and vista.scope == "agent"
        assert await instalar_en_agente(tid, uno, "pack_cobranza") == (vista, False)
        assert await instaladas_en_agente(tid, uno) == ["pack_cobranza"]
        assert await instaladas_en_agente(tid, otro) == []

        await desinstalar_de_agente(tid, uno, "pack_cobranza")
        assert await instaladas_en_agente(tid, uno) == []
        nueva, creada = await instalar_en_agente(tid, uno, "pack_cobranza")
        assert creada and nueva.id != vista.id
    filas = await _sql(
        tid, "select count(*) from pack_installations where agent_id = :a and scope = 'agent'", a=uno
    )
    assert filas[0][0] == 2  # la quitada sigue (borrado lógico)


async def test_config_del_agente_cifrada() -> None:
    from core.agents.instalaciones_agente import (
        config_del_agente,
        guardar_config_del_agente,
        instaladas_en_agente,
        instalar_en_agente,
    )
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    aid = await _agente(tid)
    async with tenant_scope(tid):
        vista, _ = await instalar_en_agente(tid, aid, "pack_payments", requiere_config=True)
        assert vista.status == "pending_config"
        assert await instaladas_en_agente(tid, aid) == []
        await guardar_config_del_agente(tid, aid, "pack_payments", {"link_expira_horas": 12, "cuenta_id": "x"})
        assert await config_del_agente(tid, aid, "pack_payments") == {"cuenta_id": "x", "link_expira_horas": 12}
        assert await instaladas_en_agente(tid, aid) == ["pack_payments"]
    ((cifrado,),) = await _sql(tid, "select config_encrypted from pack_installations where id = :i", i=vista.id)
    assert cifrado.startswith("gcm:v1:") and "link_expira_horas" not in cifrado


async def test_agente_archivado_no_instala() -> None:
    from core.agents.instalaciones import ConflictoInstalacion
    from core.agents.instalaciones_agente import instalar_en_agente
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    aid = await _agente(tid, estado="archived")
    async with tenant_scope(tid):
        with pytest.raises(ConflictoInstalacion) as exc:
            await instalar_en_agente(tid, aid, "pack_cobranza")
    assert exc.value.code == "agent_archived"


async def test_esquema_rechaza_scope_sin_agente_y_agente_ajeno() -> None:
    tid, ajeno = await _negocio(), await _negocio()
    agente_ajeno = await _agente(ajeno)
    with pytest.raises((IntegrityError, DBAPIError)):
        await _sql(
            tid,
            "insert into pack_installations (tenant_id, pack_id, scope) values (:t, 'pack_cobranza', 'agent')",
            t=tid,
        )
    with pytest.raises((IntegrityError, DBAPIError)):
        await _sql(
            tid,
            "insert into pack_installations (tenant_id, pack_id, scope, agent_id) "
            "values (:t, 'pack_cobranza', 'agent', :a)",
            t=tid, a=agente_ajeno,
        )


async def test_otro_negocio_no_ve_las_instalaciones() -> None:
    from core.agents.instalaciones_agente import instalar_en_agente
    from core.shared.tenant import tenant_scope

    tid, otro = await _negocio(), await _negocio()
    aid = await _agente(tid)
    async with tenant_scope(tid):
        await instalar_en_agente(tid, aid, "pack_cobranza")
    assert await _sql(otro, "select id from pack_installations where agent_id = :a", a=aid) == []


async def test_load_config_combina_negocio_y_agente() -> None:
    """La config del agente pisa SOLO sus campos (`config_agente`); el resto sale del negocio."""
    from core.agents.instalaciones_agente import guardar_config_del_agente, instalar_en_agente
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    plugin_manager.scan()
    tid = await _negocio()
    uno, otro = await _agente(tid), await _agente(tid)
    async with tenant_scope(tid):
        await plugin_manager.save_config("pack_commerce", {"order_prefix": "NEG", "monto_alto_threshold": 500})
        await instalar_en_agente(tid, uno, "pack_commerce")
        # `order_prefix` es del negocio: aunque venga en la config del agente, no pisa.
        await guardar_config_del_agente(tid, uno, "pack_commerce", {"monto_alto_threshold": 9, "order_prefix": "AG"})

        del_uno = await plugin_manager.load_config("pack_commerce", agent_id=uno)
        del_otro = await plugin_manager.load_config("pack_commerce", agent_id=otro)
        del_negocio = await plugin_manager.load_config("pack_commerce")
    assert (del_uno.monto_alto_threshold, del_uno.order_prefix) == (9, "NEG")
    assert (del_otro.monto_alto_threshold, del_otro.order_prefix) == (500, "NEG")
    assert del_negocio.monto_alto_threshold == 500
