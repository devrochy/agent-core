"""HU-AOS-72 · la release efímera del borrador, sin base de datos.

- Solo la prueba del panel (canal `test` + marcador `panel`) corre un borrador; un journey o un canal real no.
- Con la release efímera fijada, el dueño de las tareas y el ledger no reciben ids que no existen.
"""

from __future__ import annotations

import types
import uuid

import pytest

from core.kernel.release_del_turno import _borrador_en_prueba
from core.shared import release_vigente
from core.shared.release_vigente import AgenteCongelado, ArtefactosDeRelease
from core.shared.tenant import tenant_scope

AGENTE = uuid.uuid4()


def _conv(channel: str = "test", **state: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(channel=channel, state=state)


@pytest.mark.parametrize(
    ("conv", "esperado"),
    [
        (_conv(panel=True, draft_agent_id=str(AGENTE)), AGENTE),
        (_conv(panel=True), None),
        (_conv(draft_agent_id=str(AGENTE)), None),  # journeys/evals: canal test sin el marcador del panel
        (_conv("whatsapp", panel=True, draft_agent_id=str(AGENTE)), None),
        (_conv(panel=True, draft_agent_id="no-es-uuid"), None),
    ],
)
def test_solo_la_prueba_del_panel_corre_un_borrador(conv: types.SimpleNamespace, esperado: uuid.UUID | None) -> None:
    assert _borrador_en_prueba(conv) == esperado


def _artefactos(tenant: uuid.UUID, *, efimera: bool) -> ArtefactosDeRelease:
    foco = AgenteCongelado(
        instance_id=uuid.uuid4(), agent_id=AGENTE, key="prueba", role="specialist", version_id=uuid.uuid4(),
        version=0, contract="agentspec/3", content={}, is_default=True,
    )
    return ArtefactosDeRelease(
        tenant_id=tenant, connection_id=uuid.uuid4(), release_id=uuid.uuid4(), lane="stable", idle_minutes=720,
        agentes=(foco,), foco=foco, efimera=efimera,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("efimera", [True, False])
async def test_la_release_efimera_no_deja_su_instancia_como_duena(efimera: bool) -> None:
    from core.kernel.procedures.ejecucion import _agente_del_turno_id

    tenant = uuid.uuid4()
    async with tenant_scope(tenant):
        token = release_vigente.abrir()
        try:
            arts = _artefactos(tenant, efimera=efimera)
            release_vigente.fijar(arts)
            assert release_vigente.efimera() is efimera
            assert release_vigente.agente_del_turno() is arts.foco, "el turno sí corre con el borrador"
            assert _agente_del_turno_id() == (None if efimera else arts.foco.instance_id)
        finally:
            release_vigente.cerrar(token)
