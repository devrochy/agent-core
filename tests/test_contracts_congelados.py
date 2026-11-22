"""Verifica que los contratos congelados importan y mantienen su forma (ADR-0011).

Antes vivía en `packages/contracts/tests/`, fuera de `testpaths`, y no corría."""

import agentcore_contracts
from agentcore_contracts import (
    InboundMessage,
    Tone,
    __version__,
)


def test_version_congelada() -> None:
    # 3.0.0: `agentspec/3` y `kernelspec/1` (ADR-0113, HU-AOS-12); `AgentSpec` seguía siendo v2.
    # 3.1.0: `AgentSpecV3.knowledge` opcional (ADR-0145, adenda de ADR-0113 del 2026-10-20).
    # 3.2.0: `ExposedQuery.action` opcional (adenda HU-AOS-19 de ADR-0113).
    # 4.0.0: sale el `AgentSpec` v2 y los tipos que solo usaba él (ADR-0206 §8, HU-AOS-91).
    assert __version__ == "4.0.0"


def test_inbound_message_shape() -> None:
    msg = InboundMessage(
        tenantId="t1",
        channelId="c1",
        channel="whatsapp",
        externalMessageId="m1",
        conversationKey="c1|u1",
        contact={"externalId": "u1", "displayName": "Ana"},
        content=[{"type": "text", "text": "hola"}],
        receivedAt="2026-08-26T10:00:00Z",
        raw={"entry": [{"id": "x"}]},
    )
    assert msg.channel == "whatsapp"
    assert msg.content[0].type == "text"
    assert msg.contact.displayName == "Ana"
    # La forma congelada usa camelCase, no snake_case.
    assert "tenantId" in msg.model_dump()
    assert "tenant_id" not in msg.model_dump()


def test_inbound_message_content_variants() -> None:
    for part in (
        {"type": "image", "url": "https://x/img.png", "mimeType": "image/png"},
        {"type": "location", "lat": 4.7, "lng": -74.1, "label": "Oficina"},
        {"type": "interactive", "payload": {"button": "ok"}},
    ):
        msg = InboundMessage(
            tenantId="t1",
            channelId="c1",
            channel="web",
            externalMessageId="m1",
            conversationKey="c1|u1",
            contact={"externalId": "u1"},
            content=[part],
            receivedAt="2026-08-26T10:00:00Z",
            raw={},
        )
        assert msg.content[0].type == part["type"]


def test_el_agent_spec_v2_ya_no_se_exporta() -> None:
    """ADR-0206 §8: el agente es `AgentSpecV3` y el Coordinador `KernelSpec`; `Tone` sigue."""
    for nombre in ("AgentSpec", "Persona", "ToolBinding", "PlaybookRef", "AgentModel", "AgentMemory", "AgentPolicies", "RagConfig"):
        assert not hasattr(agentcore_contracts, nombre), nombre
        assert nombre not in agentcore_contracts.__all__, nombre
    assert set(Tone.__args__) == {"formal", "neutral", "cercano", "informal"}
