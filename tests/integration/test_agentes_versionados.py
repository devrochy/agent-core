"""HU-AOS-12 · agentes con borrador y versiones inmutables por `/api/v1` (ADR-0113, ADR-0117).

Gherkin:
- «Borrador y publicación»: publicar crea una versión inmutable con hash y el borrador sigue
  editable.
- «Concurrencia»: guardar con un `lock_version` viejo devuelve 409.
- «Validación al guardar»: un AgentSpec o KernelSpec inválido se rechaza con los errores del
  contrato (el mismo validador del CLI y de CI).
Más: keys reservadas, escrituras solo para el master (o `aos_runtime = 'v3'`) y aislamiento
entre negocios. Sin LLM ni red.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers, banderas_como_dueno, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agents"
PASSWORD = "clave-segura-123"


def fixture(nombre: str) -> dict[str, Any]:
    return json.loads((FIXTURES / nombre).read_text(encoding="utf-8"))


def ventas_sin_secretos() -> dict[str, Any]:
    spec = fixture("ventas.agentspec.json")
    spec["declarativeTools"][0]["headers"] = {"X-Cliente": "demo"}
    return spec


async def _negocio(aos_runtime: str = "legacy") -> uuid.UUID:
    from core.db.session import get_session

    tid = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text("insert into tenants (id, name, slug) values (:id, :n, :s)"),
            {"id": tid, "n": "AOS-12 API", "s": uniq("aos12-api")},
        )
    if aos_runtime == "v3":
        # `v3` exige AOS_CONNECTION_ROUTING (ADR-0112); el CHECK de BD solo mira el orden.
        await banderas_como_dueno(tid, aos_runtime="v3")
    return tid


async def _dueno(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return email


async def test_borrador_publicacion_concurrencia_y_validacion(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)  # master

    r = await app_client.post(
        "/api/v1/admin/agents", headers=h, json={"key": "ventas", "kind": "specialist", "name": "Ventas"}
    )
    assert r.status_code == 201, r.text
    agente = r.json()
    assert (agente["contract"], agente["latest_version"]) == ("agentspec/3", None)
    aid = agente["id"]

    r = await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=h)
    assert r.status_code == 200 and r.json()["lock_version"] == 0
    assert r.json()["content"]["contract"] == "agentspec/3"

    # Validación al guardar: errores del contrato, por campo, sin guardar nada.
    malo = ventas_sin_secretos()
    malo["kind"] = "specialist"
    malo["declarativeTools"][0]["headers"] = {"Authorization": "Bearer literal-que-no-vuelve"}
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": malo, "lock_version": 0})
    assert r.status_code == 422, r.text
    error = r.json()["error"]
    assert error["code"] == "invalid_spec"
    assert set(error["fields"]) == {"kind", "declarativeTools[0].headers"}
    assert "literal-que-no-vuelve" not in r.text

    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": ventas_sin_secretos(), "lock_version": 0}
    )
    assert r.status_code == 200, r.text
    assert r.json()["lock_version"] == 1

    # Concurrencia: la segunda persona guarda con el lock_version viejo → 409.
    otro = ventas_sin_secretos() | {"description": "Edición de otra persona"}
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": otro, "lock_version": 0})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "stale_lock_version"
    # 1e999 es Infinity: no entra.
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft",
        headers=h | {"Content-Type": "application/json"},
        content=b'{"content": {"contract": "agentspec/3", "model": {"primary": "x", "temperature": 1e999}}, "lock_version": 1}',
    )
    assert r.status_code == 400

    # Publicar: versión inmutable con hash; publicar lo mismo otra vez es idempotente.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={"notes": "primera"})
    assert r.status_code == 200, r.text
    v1 = r.json()
    assert v1["created"] and v1["version"]["version"] == 1 and len(v1["version"]["content_hash"]) == 64
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
    assert not r.json()["created"] and r.json()["version"]["id"] == v1["version"]["id"]

    # El borrador sigue editable; la versión publicada no cambia.
    cambio = ventas_sin_secretos() | {"description": "Segunda versión"}
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": cambio, "lock_version": 1})
    assert r.status_code == 200 and r.json()["base_version_id"] == v1["version"]["id"]
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={})
    assert r.json()["version"]["version"] == 2
    r = await app_client.get(f"/api/v1/admin/agents/{aid}/versions/{v1['version']['id']}", headers=h)
    assert r.json()["content"]["description"] == ventas_sin_secretos()["description"]

    # Inmutable en la base: el runtime no tiene UPDATE (y el trigger lo rechazaría).
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    with pytest.raises(Exception, match=r"permission denied for table agent_versions|agent_versions es append-only"):
        async with tenant_scope(tid):
            async with get_session() as session:
                await session.execute(
                    text("update agent_versions set notes = 'x' where id = :v"), {"v": v1["version"]["id"]}
                )

    r = await app_client.get(f"/api/v1/admin/agents/{aid}", headers=h)
    assert [v["version"] for v in r.json()["versions"]] == [2, 1]
    assert r.json()["latest_version"] == 2


async def test_kernelspec_del_coordinador_y_keys_reservadas(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    for key in ("general", "coordinador"):
        r = await app_client.post(
            "/api/v1/admin/agents", headers=h, json={"key": key, "kind": "specialist", "name": key}
        )
        assert r.status_code == 409, r.text

    r = await app_client.post(
        "/api/v1/admin/agents", headers=h, json={"key": "coord_pruebas", "kind": "core", "name": "Coordinador"}
    )
    aid = r.json()["id"]
    assert r.json()["contract"] == "kernelspec/1"
    kernel = fixture("coordinador.kernelspec.json")
    malo = copy.deepcopy(kernel)
    malo["routing"]["rules"][0]["when"] = {"regex": "^plan"}
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": malo, "lock_version": 0})
    assert r.status_code == 422 and "routing.rules[0].when.regex" in r.json()["error"]["fields"]
    # Un AgentSpec en un agente core no valida: el kind fija el contrato.
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": ventas_sin_secretos(), "lock_version": 0}
    )
    assert r.status_code == 422 and r.json()["error"]["fields"] == {"contract": "Se espera el contrato kernelspec/1."}
    r = await app_client.put(f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": kernel, "lock_version": 0})
    assert r.status_code == 200, r.text


async def test_con_runtime_legacy_solo_el_master_escribe(app_client) -> None:
    tid = await _negocio("legacy")
    h = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    r = await app_client.post(
        "/api/v1/admin/agents", headers=h, json={"key": "ventas", "kind": "specialist", "name": "Ventas"}
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "aos_runtime_legacy"
    assert (await app_client.get("/api/v1/admin/agents", headers=h)).status_code == 200

    v3 = await _negocio("v3")
    h3 = await api_headers(app_client, v3, email=await _dueno(v3), password=PASSWORD)
    r = await app_client.post(
        "/api/v1/admin/agents", headers=h3, json={"key": "ventas", "kind": "specialist", "name": "Ventas"}
    )
    assert r.status_code == 201, r.text


async def test_un_negocio_no_ve_ni_edita_agentes_de_otro(app_client) -> None:
    a, b = await _negocio(), await _negocio()
    ha, hb = await api_headers(app_client, a), await api_headers(app_client, b)
    r = await app_client.post(
        "/api/v1/admin/agents", headers=ha, json={"key": "ventas", "kind": "specialist", "name": "Ventas A"}
    )
    aid = r.json()["id"]
    assert (await app_client.get("/api/v1/admin/agents", headers=hb)).json() == []
    assert (await app_client.get(f"/api/v1/admin/agents/{aid}", headers=hb)).status_code == 404
    assert (await app_client.get(f"/api/v1/admin/agents/{aid}/draft", headers=hb)).status_code == 404
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=hb, json={"content": ventas_sin_secretos(), "lock_version": 0}
    )
    assert r.status_code == 404
    assert (await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=hb, json={})).status_code == 404
    # Misma key en otro negocio: permitida (la unicidad es por negocio).
    r = await app_client.post(
        "/api/v1/admin/agents", headers=hb, json={"key": "ventas", "kind": "specialist", "name": "Ventas B"}
    )
    assert r.status_code == 201


async def test_archivar_y_patch_no_cambia_kind(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    aid = (
        await app_client.post(
            "/api/v1/admin/agents", headers=h, json={"key": "soporte", "kind": "specialist", "name": "Soporte"}
        )
    ).json()["id"]
    r = await app_client.patch(f"/api/v1/admin/agents/{aid}", headers=h, json={"kind": "core"})
    assert r.status_code == 422  # key y kind no se cambian
    r = await app_client.get(f"/api/v1/admin/agents/{aid}", headers=h)
    assert r.json()["kind"] == "specialist"
    # Publicar con un lock_version viejo → 409.
    r = await app_client.post(f"/api/v1/admin/agents/{aid}/versions", headers=h, json={"lock_version": 7})
    assert r.status_code == 409
    r = await app_client.patch(f"/api/v1/admin/agents/{aid}", headers=h, json={"status": "archived"})
    assert r.status_code == 200 and r.json()["status"] == "archived"
    r = await app_client.put(
        f"/api/v1/admin/agents/{aid}/draft", headers=h, json={"content": ventas_sin_secretos(), "lock_version": 0}
    )
    assert r.status_code == 409
