"""Tests del AgentSpec desde BD y playbooks (Sprint 2, ADR-0014).

Unit tests sin BD: el ensamblador y el loader reciben un `AgentSpec` construido,
no tocan la base de datos.
"""

from __future__ import annotations

import pytest
from agentcore_contracts import AgentSpec

from core.agents.playbooks import load_playbook
from core.llm.prompt_builder import PromptContext, build_system_blocks

DEFAULT_PLAYBOOKS = [
    {"name": "identidad", "description": "Identidad del asistente"},
    {"name": "tono", "description": "Voz y tono del asistente"},
    {"name": "reglas-operacion", "description": "Reglas de operación del asistente"},
]


def make_spec(playbooks: list[dict[str, str]] | None = None) -> AgentSpec:
    return AgentSpec(
        id="agent-1",
        version="1.0.0",
        tenantId="t1",
        persona={
            "role": "asistente",
            "tone": "cercano",
            "warmth": 3,
            "emojis": "sparse",
            "maxLength": 500,
            "forbiddenPhrases": [],
        },
        model={"primary": "deepseek-flash", "temperature": 0.3},
        memory={"shortTermTurns": 30},
        playbooks=playbooks if playbooks is not None else DEFAULT_PLAYBOOKS,
        tools=[],
        policies={
            "maxToolCallsPerTurn": 10,
        },
        packs=[],
    )


@pytest.mark.asyncio
async def test_ensamblador_usa_playbooks_y_placeholders() -> None:
    spec = make_spec()
    ctx = PromptContext(
        assistant_name="Mi Asistente",
        business_description="Tienda X",
        language="es",
    )
    blocks = await build_system_blocks(spec, ctx)

    assert any("Eres Mi Asistente, el asistente virtual de: Tienda X" in b for b in blocks)
    assert any("## Tono" in b for b in blocks)
    assert any("## Reglas de operación" in b for b in blocks)
    # El bloque de reglas traduce el language a español
    assert any("Responde SIEMPRE en español" in b for b in blocks)
    # El frontmatter de los playbooks no debe filtrarse al prompt
    assert not any(b.startswith("---") for b in blocks)


@pytest.mark.asyncio
async def test_load_playbook_respeta_allowlist() -> None:
    spec = make_spec(playbooks=[{"name": "tono", "description": "Tono"}])
    content = await load_playbook(spec, "tono")
    assert content is not None and "Tono" in content

    # fuera del índice del tenant: rechazado
    outside = await load_playbook(spec, "identidad")
    assert outside is None


@pytest.mark.asyncio
async def test_load_playbook_rechaza_nombre_con_traversal() -> None:
    """Hallazgo real de `auditor-multitenant` (ADR-0026): `PlaybookRef.name`
    es un `str` libre en el contrato -- si un `name` con "/"/".." llegara al
    índice de un spec (ej. vía el fallback `raw_json` de `/admin/spec`), no
    debe poder escapar `tenant_dir(slug)/playbooks/` (ni ningún root de
    `playbook_roots()`) para leer un archivo fuera de lo previsto, incluido
    el override PRIVADO de otro tenant."""
    malicious = "../otro-tenant/playbooks/ventas"
    spec = make_spec(playbooks=[{"name": malicious, "description": "x"}])
    assert await load_playbook(spec, malicious) is None


@pytest.mark.asyncio
async def test_load_playbook_quita_frontmatter() -> None:
    spec = make_spec()
    content = await load_playbook(spec, "identidad")
    assert content is not None
    assert not content.lstrip().startswith("---")
    assert "Eres" in content


@pytest.mark.asyncio
async def test_load_playbook_usa_override_de_tenant_si_existe(tmp_path, monkeypatch) -> None:
    """ADR-0026: `TENANTS_ROOT/<slug>/playbooks/<name>.md`, si existe, gana
    sobre el default del pack/core."""
    import core.onboarding as onboarding

    slug = "tenant-override-test"
    playbooks_dir = tmp_path / slug / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (playbooks_dir / "tono.md").write_text("Tono a medida de este tenant.", encoding="utf-8")

    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)

    spec = make_spec(playbooks=[{"name": "tono", "description": "Tono"}])
    content = await load_playbook(spec, "tono", tenant_slug=slug)
    assert content == "Tono a medida de este tenant."


@pytest.mark.asyncio
async def test_load_playbook_sin_override_cae_al_default_del_pack(tmp_path, monkeypatch) -> None:
    """Sin archivo de override (carpeta del tenant vacía/inexistente), sigue
    devolviendo el default -- el fallback de siempre queda intacto."""
    import core.onboarding as onboarding

    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)

    spec = make_spec(playbooks=[{"name": "tono", "description": "Tono"}])
    content = await load_playbook(spec, "tono", tenant_slug="otro-tenant-sin-override")
    assert content is not None
    assert "Tono" in content
    assert "a medida de este tenant" not in content


@pytest.mark.asyncio
async def test_load_playbook_override_respeta_allowlist(tmp_path, monkeypatch) -> None:
    """Un override no agrega nombres nuevos: si `name` no está en la
    allowlist del tenant, se rechaza ANTES de mirar el filesystem."""
    import core.onboarding as onboarding

    slug = "tenant-override-allowlist"
    playbooks_dir = tmp_path / slug / "playbooks"
    playbooks_dir.mkdir(parents=True)
    (playbooks_dir / "identidad.md").write_text("Identidad falsa.", encoding="utf-8")

    monkeypatch.setattr(onboarding, "tenant_dir", lambda s: tmp_path / s)

    spec = make_spec(playbooks=[{"name": "tono", "description": "Tono"}])
    content = await load_playbook(spec, "identidad", tenant_slug=slug)
    assert content is None


@pytest.mark.asyncio
async def test_ensamblador_respeta_orden_del_indice() -> None:
    spec = make_spec(
        playbooks=[
            {"name": "reglas-operacion", "description": "Reglas"},
            {"name": "identidad", "description": "Identidad"},
        ]
    )
    ctx = PromptContext(assistant_name="A", business_description="B")
    blocks = await build_system_blocks(spec, ctx)
    assert "## Reglas de operación" in blocks[0]
    assert "Eres A" in blocks[1]

# --------------------------------------------------------------------- v2.0.0 (ADR-0050)
@pytest.mark.asyncio
async def test_persona_llega_como_variables_de_plantilla() -> None:
    """El tono/calidez/emojis del spec se sustituyen en los playbooks (ADR-0050)."""
    ctx = PromptContext(
        assistant_name="A",
        business_description="B",
        tone="informal",
        warmth=5,
        emojis="free",
        max_length=300,
    )
    vars_ = ctx.template_vars
    assert vars_["tone"] == "informal"
    assert vars_["warmth"] == "5"
    assert "emojis" in vars_["emojis"]
    assert vars_["max_length"] == "300"


def test_persona_frase_prohibida_es_guardrail_duro() -> None:
    from core.orchestrator.loop import _persona_frase_prohibida

    spec = make_spec()
    spec.persona.forbiddenPhrases = ["garantizado"]
    spec.persona.maxLength = 20

    verdict = _persona_frase_prohibida("Está GARANTIZADO el resultado", spec)
    assert verdict is not None and verdict.reason == "persona_forbidden_phrase"
    assert "garantizado" not in verdict.retry_instruction.lower(), "la frase no se interpola"
    assert _persona_frase_prohibida("Respuesta corta", spec) is None
    # El largo ya no es un fallo de guardrail (adenda de ADR-0050).
    assert _persona_frase_prohibida("X" * 30, spec) is None


def test_persona_excede_largo_es_tope_blando() -> None:
    from core.orchestrator.loop import _persona_excede_largo

    spec = make_spec()
    spec.persona.maxLength = 20
    assert _persona_excede_largo("X" * 21, spec)
    assert not _persona_excede_largo("X" * 20, spec)
    spec.persona.maxLength = 0
    assert not _persona_excede_largo("X" * 5000, spec), "0 apaga el tope"


async def test_instruccion_largo_sale_del_playbook_o_del_respaldo(monkeypatch) -> None:
    from core.orchestrator import loop

    spec = make_spec()
    spec.persona.maxLength = 320

    async def sin_seccion(_spec, _key):
        return None

    monkeypatch.setattr(loop, "load_response", sin_seccion)
    assert await loop._instruccion_largo(spec) == loop.INSTRUCCION_LARGO.replace("{max_length}", "320")

    async def con_seccion(_spec, key):
        return "Acorta a {max_length} caracteres." if key == "persona_max_length" else None

    monkeypatch.setattr(loop, "load_response", con_seccion)
    assert await loop._instruccion_largo(spec) == "Acorta a 320 caracteres."
