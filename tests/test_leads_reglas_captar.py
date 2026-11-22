"""HU-AOS-70 · las reglas de Captar leads y el campo «negocio» de `actualizar_lead`.

- Las reglas (confirmar el dato, preguntar antes de reemplazar uno que contradice y no deducirlo) viven en el
  playbook del kernel `captar-leads` (invariante 3) y entran al prompt solo de los agentes con alguna acción de
  Captar leads encendida, tengan o no comercio.
- `negocio` va al `custom` del lead (sin columna nueva) y al perfil del cliente con los `writeNamespaces` del agente.

Sin base de datos ni LLM.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents.playbooks import load_core_playbook
from core.leads import servicio, tools
from core.leads.tools import LeadSkill
from core.llm import prompt_builder
from core.mcp.types import ToolContext

ENCABEZADO = "## Captar leads"


def _spec(bindings: list[dict[str, Any]]) -> Any:
    from core.agents.turno import agente_desde_defaults

    return agente_desde_defaults(
        {
            "persona": {"role": "asistente", "tone": "cercano", "warmth": 3, "emojis": "sparse", "maxLength": 500,
                         "forbiddenPhrases": []},
            "model": {"primary": "deepseek-flash", "temperature": 0.3},
            "memory": {"shortTermTurns": 30},
            "playbooks": [],
            "tools": bindings,
            "policies": {"maxToolCallsPerTurn": 10},
        }
    )


def _ctx() -> prompt_builder.PromptContext:
    return prompt_builder.PromptContext(assistant_name="Sofía", business_description="", language="es")


def test_el_playbook_tiene_las_reglas_del_gherkin() -> None:
    texto = load_core_playbook(prompt_builder.PLAYBOOK_CAPTAR_LEADS)
    assert texto.startswith(ENCABEZADO)
    for regla in ("Solo lo que dijo el cliente", "Confirma antes de guardar", "Si contradice uno guardado"):
        assert regla in texto
    assert "actualizar_lead" in texto and "negocio" in texto


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(tools.ACCIONES_CAPTAR_LEADS))
async def test_entra_con_cualquier_accion_de_captar_leads(tool: str) -> None:
    bloques = await prompt_builder.bloques_fijos(
        _spec([{"tool": tool, "enabled": True}]), _ctx(), tenant_slug=""
    )
    assert [b for b in bloques if b.startswith(ENCABEZADO)] == [
        load_core_playbook(prompt_builder.PLAYBOOK_CAPTAR_LEADS).strip()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bindings",
    [[], [{"tool": "actualizar_lead", "enabled": False}], [{"tool": "consultar_inventario", "enabled": True}]],
)
async def test_no_entra_sin_captar_leads(bindings: list[dict[str, Any]]) -> None:
    bloques = await prompt_builder.bloques_fijos(_spec(bindings), _ctx(), tenant_slug="")
    assert not any(b.startswith(ENCABEZADO) for b in bloques)


def test_el_esquema_de_actualizar_lead_trae_negocio() -> None:
    [tool] = [t for t in LeadSkill().get_tools() if t.name == "actualizar_lead"]
    assert tool.parameters["properties"]["negocio"]["type"] == "string"


def test_fijar_negocio_no_borra_con_vacio_y_no_muta_en_sitio() -> None:
    original = {"utm": "ig"}
    lead = SimpleNamespace(custom=original, last_activity_at=None)
    assert servicio.fijar_negocio(lead, "") is False and lead.custom is original
    assert servicio.fijar_negocio(lead, "Panadería La Espiga") is True
    assert lead.custom == {"utm": "ig", "negocio": "Panadería La Espiga"} and original == {"utm": "ig"}
    assert servicio.fijar_negocio(lead, "Panadería La Espiga") is False


@pytest.mark.asyncio
async def test_actualizar_lead_guarda_el_negocio_en_el_lead_y_el_perfil(monkeypatch: pytest.MonkeyPatch) -> None:
    lead = SimpleNamespace(
        id=uuid.uuid4(), name=None, phone=None, email=None, interest="", custom={}, last_activity_at=None
    )
    perfil: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _get_session():  # type: ignore[no-untyped-def]
        yield object()

    async def _vivo(*_a: Any, **_k: Any) -> Any:
        return lead

    async def _guardar(_cid: uuid.UUID, atributos: dict[str, Any], **_kw: Any) -> None:
        perfil.append(dict(atributos))

    monkeypatch.setattr(tools, "get_session", _get_session)
    monkeypatch.setattr(tools, "get_current_tenant_id", lambda: uuid.uuid4())
    monkeypatch.setattr(tools.servicio, "vivo", _vivo)
    monkeypatch.setattr(tools, "guardar_datos", _guardar)
    ctx = ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=uuid.uuid4(),
        idempotency_key="test:negocio",
        channel="whatsapp",
        agent_id=uuid.uuid4(),
        write_namespaces=frozenset({"perfil"}),
    )

    res = await LeadSkill().actualizar_lead({"negocio": "  Panadería   La Espiga "}, ctx)

    assert res == {"lead_id": str(lead.id), "status": "actualizado"}
    assert lead.custom == {"negocio": "Panadería La Espiga"}
    assert perfil == [{"negocio": "Panadería La Espiga"}]
