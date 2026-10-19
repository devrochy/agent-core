"""HU-UX-17: motivos de escalación legibles (código cerrado + etiqueta en español)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from core.orchestrator import motivos_escalacion as m


@pytest.mark.parametrize(
    ("reason", "summary", "codigo"),
    [
        ("tool:queja", "", "queja"),
        ("tool:monto_alto", "", "monto_alto"),
        ("tool:solicitud_cliente", "", "solicitud_cliente"),
        ("tool:otro", "", "otro"),
        # Texto libre de antes de HU-UX-17: se conserva, cae en `otro`.
        ("tool:el cliente quiere hablar con el gerente", "", "otro"),
        ("tool:manual", "", "otro"),
        ("keyword", "", "palabra_clave"),
        ("llm_error", "", "falla_ia"),
        ("llm_refusal", "", "rechazo_ia"),
        ("plugin:crear_pedido", "", "accion_habilidad"),
        ("regla:sin_descuentos", "", "regla_negocio"),
        ("guardrail", "regla", "regla_negocio"),
        ("guardrail", "precio_inventado", "verificacion"),
        ("precio_inventado: el precio no sale de una tool", "", "verificacion"),
        ("persona_forbidden_phrase", "", "frase_prohibida"),
        ("send_failed", "", "envio_fallido"),
        ("turns_unresolved", "", "sin_resolver"),
        ("max_iterations", "", "limite_iteraciones"),
        ("algo_nuevo", "", "otro"),
    ],
)
def test_codigo(reason: str, summary: str, codigo: str) -> None:
    assert m.codigo(reason, summary) == codigo
    assert codigo in m.MOTIVOS


def test_todo_codigo_tiene_etiqueta() -> None:
    assert set(m.ETIQUETAS) == set(m.MOTIVOS)
    assert set(m.MOTIVOS_TOOL) <= set(m.MOTIVOS)


def test_etiquetas_en_espanol() -> None:
    assert m.etiqueta("tool:queja") == "Queja"
    assert m.etiqueta("keyword") == "Pidió hablar con una persona"
    assert m.etiqueta("regla:sin_descuentos") == "Regla del negocio: Sin descuentos"
    assert m.etiqueta("guardrail", "regla") == "Regla del negocio"
    # El texto libre viejo no se muestra como etiqueta (va crudo en `reason`).
    assert m.etiqueta("tool:el cliente quiere al gerente") == "Otro"


def test_accion_de_habilidad_con_y_sin_resolutor() -> None:
    assert m.etiqueta("plugin:crear_pedido", resolver_habilidad=lambda t: "Comercio") == "Acción de Comercio"
    assert m.etiqueta("plugin:crear_pedido", resolver_habilidad=lambda t: None) == "Acción de Crear pedido"
    assert m.etiqueta("plugin:crear_pedido") == "Acción de Crear pedido"
    assert m.etiqueta("plugin:") == "Acción de una habilidad"


@pytest.mark.parametrize(
    ("reason", "codigo"),
    [("tool:queja", "queja"), ("tool:queja_extra", "otro"), ("tool:solicitudXcliente", "otro")],
)
def test_tool_solo_reconoce_codigos_exactos(reason: str, codigo: str) -> None:
    """`tool:<x>` cuenta como código solo si `x` está tal cual en el catálogo."""
    assert m.codigo(reason) == codigo


def test_motivo_de_tool() -> None:
    assert m.motivo_de_tool("queja") == ("queja", "")
    assert m.motivo_de_tool(" QUEJA ") == ("queja", "")
    assert m.motivo_de_tool("el cliente está molesto") == ("otro", "el cliente está molesto")
    assert m.motivo_de_tool("") == ("otro", "")


def _sql(cod: str) -> str:
    return str(m.condicion_sql(cod).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_condicion_sql_por_codigo() -> None:
    assert "'tool:queja'" in _sql("queja")
    assert "'keyword'" in _sql("palabra_clave")
    assert "LIKE 'plugin:'" in _sql("accion_habilidad")
    otro = _sql("otro")
    assert otro.startswith("NOT")
    assert "'tool:queja'" in otro and "'keyword'" in otro


async def test_tool_guarda_codigo_y_detalle(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.tools.core_skill as cs

    llamadas: list[dict[str, Any]] = []

    async def fake_escalate(conversation_id, reason, summary=""):  # type: ignore[no-untyped-def]
        llamadas.append({"reason": reason, "summary": summary})

    monkeypatch.setattr(cs, "escalate", fake_escalate)
    skill = cs.CoreSkill()
    ctx: Any = type("Ctx", (), {"conversation_id": uuid.uuid4()})()

    await skill._escalate_to_human({"reason": "queja", "summary": "Pedido tarde"}, ctx)
    await skill._escalate_to_human({"reason": "otro", "detalle": "pide factura a otro NIT", "summary": "S"}, ctx)
    await skill._escalate_to_human({"reason": "quiere al gerente"}, ctx)

    assert llamadas[0] == {"reason": "tool:queja", "summary": "Pedido tarde"}
    assert llamadas[1] == {"reason": "tool:otro", "summary": "S\n\nDetalle del motivo: pide factura a otro NIT"}
    assert llamadas[2] == {"reason": "tool:otro", "summary": "Detalle del motivo: quiere al gerente"}


def test_schema_de_la_tool_tiene_el_catalogo() -> None:
    from core.tools.core_skill import CoreSkill

    tool = next(t for t in CoreSkill().get_tools() if t.name == "escalate_to_human")
    props = tool.parameters["properties"]
    assert props["reason"]["enum"] == list(m.MOTIVOS_TOOL)
    assert "detalle" in props
