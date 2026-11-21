"""HU-AOS-89 · el Coordinador atiende la conexión sin especialistas (ADR-0205 §5), con el loop real y un modelo
guionado (sin LLM ni red).

- **Set cerrado:** el turno solo ofrece tools de recepción (de las 7 fijas) y ninguna de comercio, agenda, cobranza
  o suscripciones.
- **Registra al cliente:** `crear_lead` guarda lo que dijo en el lead base y `update_customer_profile` solo guarda
  nombre, teléfono y correo en el perfil.
- **Ledger aparte:** el turno se anota como `coordinador@recepcion:<versión>` con la versión real del Coordinador.
- **Frontera inmutable:** la API responde 409 `coordinador_inmutable` a instalarle habilidades o cambiarle el flujo
  o el horario.
"""

from __future__ import annotations

import os

import pytest
from test_coordinador_receptor import (  # noqa: F401  (fixture)
    _alta_con_agent_os,
    _negocio_sin_especialistas,
)
from test_editor_agentes_api import _dueno
from test_habilidades_por_agente_loop import (  # noqa: F401  (fixture)
    _conversacion,
    _proceso,
    _sql,
    _turno,
)

from core.agents.recepcion import TOOLS_RECEPCION

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

DE_PACKS = {"crear_pedido", "consultar_inventario", "create_calendar_event", "cob_consultar_saldo", "sub_inscribir"}


async def test_el_coordinador_atiende_con_su_set_cerrado_y_registra_al_cliente(monkeypatch) -> None:
    from core.memory.entity import get_profile
    from core.shared.tenant import tenant_scope

    tid, cid = await _negocio_sin_especialistas()
    conv = await _conversacion(tid, cid)
    guion = await _turno(
        tid, conv, monkeypatch,
        [
            ("update_customer_profile", {"attributes": {"name": "Ana Gómez", "direccion": "Calle 1 # 2-3"}}),
            ("crear_lead", {"name": "Ana Gómez", "interest": "planes para su tienda", "email": "ana@example.com"}),
            "Gracias, Ana. Ya dejé tu mensaje listo: el equipo te va a escribir.",
        ],
        texto="Hola, soy Ana Gómez, quiero saber de sus planes, mi correo es ana@example.com",
    )

    ofrecidas = set(guion.tools[0])
    assert ofrecidas and ofrecidas <= set(TOOLS_RECEPCION), ofrecidas
    assert {"crear_lead", "escalate_to_human", "update_customer_profile"} <= ofrecidas
    assert not ofrecidas & DE_PACKS

    [(nombre, correo, interes)] = await _sql(
        tid, "select l.name, l.email, l.interest from leads l join conversations c on c.customer_id = l.customer_id "
        "where c.id = :c and l.deleted_at is null", c=conv,
    )
    assert (nombre, correo, interes) == ("Ana Gómez", "ana@example.com", "planes para su tienda")

    [(cliente,)] = await _sql(tid, "select customer_id from conversations where id = :c", c=conv)
    async with tenant_scope(tid):
        perfil = await get_profile(cliente)
    assert "Ana Gómez" in str(perfil) and "Calle 1" not in str(perfil)

    [(version_ledger,)] = await _sql(
        tid, "select agent_spec_version from usage_ledger where conversation_id = :c order by created_at desc limit 1",
        c=conv,
    )
    [(version_coordinador,)] = await _sql(
        tid, "select max(v.version) from agent_versions v join agents a on a.id = v.agent_id where a.key = 'coordinador'"
    )
    assert version_ledger == f"coordinador@recepcion:{version_coordinador}"


async def test_las_habilidades_flujo_y_horario_del_coordinador_son_inmutables(app_client) -> None:
    tid, _cid = await _negocio_sin_especialistas()
    h = await _dueno(app_client, tid)
    [(coordinador,)] = await _sql(tid, "select id from agents where key = 'coordinador'")

    r = await app_client.put(
        f"/api/v1/admin/agents/{coordinador}/skills/pack_knowledge", headers=h, json={"installed": True}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "coordinador_inmutable"), r.text
    r = await app_client.put(
        f"/api/v1/admin/agents/{coordinador}/flow", headers=h, json={"flow": {"sections": []}, "lock_version": 1}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "coordinador_inmutable"), r.text
    r = await app_client.put(
        f"/api/v1/admin/agents/{coordinador}/schedule", headers=h, json={"schedule": None, "lock_version": 1}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (409, "coordinador_inmutable"), r.text
    instaladas = await _sql(
        tid, "select count(*) from pack_installations where agent_id = :a and deleted_at is null", a=coordinador
    )
    assert instaladas == [(0,)]
