"""Verifica que los contratos congelados importan y mantienen su forma (ADR-0011).

Antes vivía en `packages/contracts/tests/`, fuera de `testpaths`, y no corría."""

from agentcore_contracts import (
    AgentSpec,
    InboundMessage,
    Tone,
    __version__,
)


def test_version_congelada() -> None:
    # 3.0.0: `agentspec/3` y `kernelspec/1` (ADR-0113, HU-AOS-12); `AgentSpec` sigue siendo v2.
    # 3.1.0: `AgentSpecV3.knowledge` opcional (ADR-0145, adenda de ADR-0113 del 2026-10-20).
    assert __version__ == "3.1.0"


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


def test_agent_spec_shape() -> None:
    spec = AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "vendedor",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
            "locale": "es-CO",
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30, "summarize": True},
        playbooks=[{"name": "ventas", "description": "Playbook de ventas"}],
        tools=[{"tool": "crear_lead", "enabled": True, "scopes": ["crm:lead:w"]}],
        policies={
            "escalation": {"triggers": ["asesor"]},
            "confirmBeforeWrite": True,
            "maxToolCallsPerTurn": 5,
        },
        packs=["pack_commerce"],
    )
    assert spec.persona.tone in Tone.__args__
    # v2.0.0 (ADR-0050): los campos de v1 que se eliminaron se ignoran al cargar un spec viejo.
    assert not hasattr(spec.policies, "escalation")
    assert "locale" not in spec.persona.model_dump()
    assert "tenantId" in spec.model_dump()