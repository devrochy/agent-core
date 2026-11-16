"""`write_namespaces_del_turno` fail-closed en todo turno v3 sin agente en foco (hallazgo del
auditor multitenant sobre HU-ARCH-35).

Unit tests sin BD: con la release fijada pero sin foco, o con la ranura de otro negocio, el
`ToolContext` recibe `frozenset()` (ninguna escritura) y no `None` (legacy, sin chequeo).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from core.datos_cliente import guardar as guardar_mod
from core.mcp.host import new_tool_context
from core.memory import entity as entity_mod
from core.shared import release_vigente
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
from core.shared.tenant import tenant_scope
from core.tools.core_skill import CoreSkill

T = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
OTRO = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _especialista() -> AgenteCongelado:
    return AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=uuid.uuid4(), key="ventas", role="specialist",
        version_id=uuid.uuid4(), version=1, contract="agentspec/3",
        content={"memory": {"writeNamespaces": ["perfil"]}},
    )


def _release(tenant_id: uuid.UUID, foco: AgenteCongelado | None) -> ArtefactosDeRelease:
    return ArtefactosDeRelease(
        tenant_id=tenant_id, connection_id=uuid.uuid4(), release_id=uuid.uuid4(), lane="stable",
        idle_minutes=720, agentes=(foco,) if foco is not None else (), foco=foco,
    )


@pytest.mark.asyncio
async def test_fuera_de_turno_y_ranura_vacia_siguen_en_legacy() -> None:
    assert release_vigente.write_namespaces_del_turno() is None
    token = release_vigente.abrir()
    try:
        # Ranura abierta sin marcar (bandera global apagada, conexión interna del Centro de pruebas).
        async with tenant_scope(T):
            assert not release_vigente.exige_concesion()
            assert release_vigente.write_namespaces_del_turno() is None
    finally:
        release_vigente.cerrar(token)


@pytest.mark.asyncio
async def test_release_fijada_sin_foco_niega_toda_escritura() -> None:
    token = release_vigente.abrir()
    try:
        async with tenant_scope(T):
            release_vigente.fijar(_release(T, None))
            assert release_vigente.exige_concesion()
            assert release_vigente.write_namespaces_del_turno() == frozenset()
    finally:
        release_vigente.cerrar(token)


@pytest.mark.asyncio
async def test_con_foco_son_los_del_agente_y_otro_negocio_no_los_hereda() -> None:
    token = release_vigente.abrir()
    try:
        release_vigente.fijar(_release(T, _especialista()))
        async with tenant_scope(T):
            assert release_vigente.write_namespaces_del_turno() == frozenset({"perfil"})
        # La ranura es de T: en el scope de otro negocio no hay foco suyo, y se niega todo.
        async with tenant_scope(OTRO):
            assert release_vigente.agente_del_turno() is None
            assert release_vigente.write_namespaces_del_turno() == frozenset()
    finally:
        release_vigente.cerrar(token)


@pytest.mark.asyncio
async def test_update_customer_profile_sin_foco_no_escribe(monkeypatch: pytest.MonkeyPatch) -> None:
    """La tool llega a la capa de hechos con `concedidos=frozenset()` y se niega antes de tocar la BD."""

    async def sin_aviso(tenant_id: uuid.UUID, customer_id: uuid.UUID) -> tuple[Any, str]:
        return guardar_mod.aviso_mod.desde(None), guardar_mod.consentimiento.SIN_AVISO

    sesiones: list[object] = []

    @asynccontextmanager
    async def sesion_falsa():  # type: ignore[no-untyped-def]
        sesion = object()
        sesiones.append(sesion)
        yield sesion

    monkeypatch.setattr(guardar_mod, "_estado", sin_aviso)
    monkeypatch.setattr(entity_mod, "get_session", sesion_falsa)

    token = release_vigente.abrir()
    try:
        async with tenant_scope(T):
            release_vigente.fijar(_release(T, None))
            ctx = new_tool_context(
                conversation_id=uuid.uuid4(), customer_id=uuid.uuid4(), idempotency_key="k",
                channel="whatsapp", write_namespaces=release_vigente.write_namespaces_del_turno(),
            )
            assert ctx.write_namespaces == frozenset()
            resultado = await CoreSkill()._update_customer_profile({"attributes": {"ciudad": "Cali"}}, ctx)
    finally:
        release_vigente.cerrar(token)
    assert resultado == {"error": "este agente no puede guardar datos en el perfil del cliente"}
    # La sesión se abrió pero el `NamespaceNoConcedido` salió antes de cualquier consulta.
    assert len(sesiones) == 1
