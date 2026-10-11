"""HU-AOS-12 · `cargar_como_v3` sobre la base real (ADR-0113 §5).

Gherkin «Lector tolerante»: el AgentSpec v2 activo de un negocio queda como el agente General de
CADA conexión conversacional (no de las integraciones) y el KernelSpec se deriva en memoria de
la `persona` v2. Sin LLM ni red; con RLS: un negocio no ve las conexiones del otro.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

SECRETO = "Bearer token-del-negocio-que-no-sale"


def _uniq(prefijo: str) -> str:
    return f"{prefijo}-{uuid.uuid4().hex[:8]}"


async def _negocio(spec: dict[str, Any]) -> uuid.UUID:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, :n, :s)"),
            {"id": tid, "n": "AOS-12", "s": _uniq("aos12")},
        )
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text(
                    "insert into agent_specs (id, tenant_id, version, is_active, spec) "
                    "values (gen_random_uuid(), :t, '1.0.0', true, cast(:spec as jsonb))"
                ),
                {"t": tid, "spec": json.dumps(spec | {"tenantId": str(tid)})},
            )
    return tid


async def _conexion(tid: uuid.UUID, tipo: str | None) -> uuid.UUID:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    cid = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text(
                    "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
                    "values (:id, :t, 'whatsapp', 'meta', :ext, :tipo)"
                ),
                {"id": cid, "t": tid, "ext": _uniq("ext"), "tipo": tipo},
            )
    return cid


def _spec_v2() -> dict[str, Any]:
    from core.agents.spec import DEFAULT_SPEC

    return DEFAULT_SPEC | {
        "persona": DEFAULT_SPEC["persona"] | {"role": "Asesora", "tone": "formal", "warmth": 2},
        "policies": {"maxToolCallsPerTurn": 8, "budgetUsdPerConversation": 0.3},
        "packs": ["pack_commerce", "pack_payments"],
        "extensions": {
            "tools": [
                {
                    "name": "consultar_envio",
                    "url": "https://api.transportadora.co/g/{g}",
                    "allowed_domains": ["api.transportadora.co"],
                    "headers": {"Authorization": SECRETO},
                }
            ]
        },
    }


async def test_general_por_conexion_conversacional_y_kernel_derivado() -> None:
    from core.agents.contrato_v3 import revisar_spec
    from core.agents.tolerante import cargar_como_v3
    from core.kernel.procedures import catalogo_de_acciones
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    modules = plugin_manager.scan()
    tid = await _negocio(_spec_v2())
    wa = await _conexion(tid, "whatsapp_meta")
    ig = await _conexion(tid, "instagram_meta")
    await _conexion(tid, "google_calendar")  # integración: no recibe agentes
    await _conexion(tid, None)  # sin tipo de catálogo: aviso

    otro = await _negocio(_spec_v2())
    ajena = await _conexion(otro, "whatsapp_meta")

    async with tenant_scope(tid):
        vista = await cargar_como_v3(tid, modules=modules)
        # Nunca se lee un negocio desde el scope de otro.
        with pytest.raises(PermissionError):
            await cargar_como_v3(otro, modules=modules)

    assert vista.conexiones == [wa, ig]
    assert ajena not in vista.conexiones
    assert vista.identidad.tenant_id == str(tid)
    assert vista.kernel.voice.role == "Asesora" and vista.kernel.voice.tone == "formal"
    assert vista.kernel.policies.budgetUsdPerConversation == 0.3
    assert vista.general.policies.maxToolCallsPerTurn == 8
    assert any(p.id == "pack_commerce.pedido" for p in vista.procedimientos)
    habilitadas = {b.tool for b in vista.general.tools if b.enabled}
    assert {"crear_pedido", "confirmar_pago_pedido", "escalate_to_human"} <= habilitadas
    assert any("sin tipo de catálogo" in a for a in vista.avisos)
    # Sin secretos en la vista: solo el nombre de lo que falta migrar.
    assert vista.secretos_por_migrar == ["consultar_envio_authorization"]
    assert SECRETO not in repr(vista)
    catalogo = catalogo_de_acciones(modules)
    assert revisar_spec(vista.general.model_dump(mode="json", by_alias=True), catalogo) == []
    assert revisar_spec(vista.kernel.model_dump(mode="json", by_alias=True), catalogo) == []


async def test_tenant_id_ajeno_en_el_json_no_cambia_el_negocio() -> None:
    """El `tenantId` del JSON v2 lo puede editar el negocio: la identidad y las conexiones
    salen del `tenant_scope`, no de él."""
    from core.agents.tolerante import cargar_como_v3
    from core.plugin_manager import plugin_manager
    from core.shared.tenant import tenant_scope

    victima = await _negocio(_spec_v2())
    await _conexion(victima, "whatsapp_meta")
    atacante = await _negocio(_spec_v2())
    propia = await _conexion(atacante, "whatsapp_meta")
    async with tenant_scope(atacante):
        from core.db.session import get_session

        async with get_session() as session:
            await session.execute(
                text(
                    "update agent_specs set spec = jsonb_set(spec, '{tenantId}', to_jsonb(cast(:v as text))) "
                    "where tenant_id = :t"
                ),
                {"v": str(victima), "t": atacante},
            )
        vista = await cargar_como_v3(modules=plugin_manager.scan())
    assert vista.identidad.tenant_id == str(atacante)
    assert vista.conexiones == [propia]
    assert any("distinto del negocio de la fila" in a for a in vista.avisos)
