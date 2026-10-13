"""Centro de pruebas (SPEC-30, HU-QA-01/02): catálogo, ejecución paso a paso, capturas,
informe, consolidación entre testers, ciclos y permisos de la sección global `qa`."""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import api_headers
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PASSWORD = "clave-segura-123"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

CATALOGO = """
version: 7
fecha: '2026-10-13'
items:
- id: pruebas-uno
  seccion: pruebas
  modulo: Panel
  grupo: Grupo A
  titulo: Prueba uno
  prioridad: P0
  pasos:
  - {accion: Abrir el panel, esperado: Carga}
  - {accion: Pulsar el botón, esperado: Pasa algo}
  hu: [HU-QA-03]
- id: pruebas-dos
  seccion: pruebas
  grupo: Grupo A
  titulo: Prueba dos
  pasos: [Hacer algo]
  esperado: Resultado final
- id: test-smtp
  seccion: test
  grupo: Correo
  titulo: SMTP de test
  porque: Sin SMTP no llegan correos.
  como: Cargar las variables.
"""


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:6]}@example.test"


async def _limpiar_qa() -> None:
    """Las tablas `qa_*` son globales: cada corrida arranca sin ciclos ni catálogo."""
    engine = create_async_engine(os.environ["MIGRATIONS_DATABASE_URL"])
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "truncate qa_events, qa_attachments, qa_step_results, qa_item_results, qa_runs, "
                    "qa_config_status, qa_cycles, qa_items, qa_catalog_versions cascade"
                )
            )
    finally:
        await engine.dispose()


async def _make_admin(email: str, role: str = "colaborador") -> uuid.UUID:
    from core.admin.auth import hash_password
    from core.db.models import Admin
    from core.db.session import get_session

    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role=role)
        session.add(admin)
        await session.flush()
        return admin.id


async def _grant(admin_id: uuid.UUID, section: str, access: str) -> None:
    from core.db.models import AdminPermission
    from core.db.session import get_session

    async with get_session() as session:
        session.add(AdminPermission(admin_id=admin_id, section=section, access=access, tenant_id=None))


async def _link(admin_id: uuid.UUID, tenant_id: uuid.UUID, role: str) -> None:
    from core.db.models import TenantUser
    from core.db.session import get_session

    async with get_session() as session:
        session.add(TenantUser(tenant_id=tenant_id, admin_id=admin_id, role=role))


@pytest.fixture(scope="module")
async def qa_listo(app_client, tenant_setup):
    await _limpiar_qa()
    t1, _t2 = tenant_setup
    master = await api_headers(app_client, t1)
    r = await app_client.post("/api/v1/admin/qa/catalog/import", json={"contenido": CATALOGO, "commit_docs": "abc1234"}, headers=master)
    assert r.status_code == 200, r.text
    yield master, t1
    await _limpiar_qa()


async def test_importar_es_idempotente_y_abre_el_primer_ciclo(app_client, qa_listo) -> None:
    master, _ = qa_listo
    r = await app_client.post("/api/v1/admin/qa/catalog/import", json={"contenido": CATALOGO}, headers=master)
    assert r.status_code == 200
    body = r.json()
    assert body["creada"] is False and body["version"] == 7 and body["items"] == 3

    r = await app_client.get("/api/v1/admin/qa/catalog", headers=master)
    cat = r.json()
    assert cat["catalogo"]["version"] == 7 and cat["catalogo"]["commit_docs"] == "abc1234"
    assert cat["ciclo"]["nombre"].startswith("Ciclo 2026-10-13")
    assert [i["slug"] for i in cat["items"]] == ["pruebas-uno", "pruebas-dos", "test-smtp"]
    assert cat["progreso"]["total"] == 2 and cat["progreso"]["sin_correr"] == 2
    assert cat["items"][2]["estado"] == "pendiente"


async def test_catalogo_invalido_se_rechaza_con_el_detalle(app_client, qa_listo) -> None:
    master, _ = qa_listo
    r = await app_client.post(
        "/api/v1/admin/qa/catalog/import",
        json={"contenido": "version: 1\nfecha: x\nitems: [{id: a, seccion: otra, grupo: g, titulo: t}]"},
        headers=master,
    )
    assert r.status_code == 422
    assert "seccion" in r.json()["error"]["message"]


async def test_ejecucion_paso_a_paso_captura_e_informe(app_client, qa_listo, monkeypatch, tmp_path) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "tenants_root", str(tmp_path))
    enviados: list[tuple[str, str, str]] = []

    async def _fake_send(to: str, subject: str, body: str) -> str:
        enviados.append((to, subject, body))
        return "id"

    import core.gateway.system_email as system_email

    monkeypatch.setattr(system_email, "send_system_email", _fake_send)
    master, _ = qa_listo

    r = await app_client.get("/api/v1/admin/qa/runs/me", headers=master)
    assert r.status_code == 200 and r.json() is None
    r = await app_client.post("/api/v1/admin/qa/runs/me/start", headers=master)
    assert r.status_code == 200 and r.json()["editable"] is True

    base = "/api/v1/admin/qa/runs/me/items/pruebas-uno"
    r = await app_client.put(f"{base}/steps/1", json={"estado": "ok"}, headers=master)
    assert r.json()["resultados"]["pruebas-uno"]["estado"] == "en_curso"
    r = await app_client.put(f"{base}/steps/2", json={"estado": "falla", "nota": "No pasa nada"}, headers=master)
    run = r.json()
    assert run["resultados"]["pruebas-uno"]["estado"] == "falla"
    assert run["resultados"]["pruebas-uno"]["pasos"]["2"] == {"estado": "falla", "nota": "No pasa nada"}
    assert run["progreso"]["falla"] == 1 and run["progreso"]["terminadas"] == 1

    r = await app_client.put(f"{base}/steps/3", json={"estado": "ok"}, headers=master)
    assert r.status_code == 422
    r = await app_client.put(f"{base}/steps/1", json={"estado": "raro"}, headers=master)
    assert r.status_code == 422

    r = await app_client.post(
        "/api/v1/admin/qa/runs/me/attachments",
        data={"item_slug": "pruebas-uno", "paso": "2"},
        files={"archivo": ("pantalla.png", PNG, "image/png")},
        headers=master,
    )
    assert r.status_code == 201, r.text
    adjunto = r.json()
    assert (tmp_path / ".plataforma" / "pruebas").is_dir()
    r = await app_client.get(f"/api/v1/admin/qa/attachments/{adjunto['id']}", headers=master)
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["x-content-type-options"] == "nosniff"

    r = await app_client.post(
        "/api/v1/admin/qa/runs/me/attachments",
        data={"item_slug": "pruebas-uno", "paso": "2"},
        files={"archivo": ("virus.exe", b"MZ...", "application/x-msdownload")},
        headers=master,
    )
    assert r.status_code == 422

    r = await app_client.put(f"{base}", json={"nota": "Revisar el botón"}, headers=master)
    assert r.json()["resultados"]["pruebas-uno"]["nota"] == "Revisar el botón"

    r = await app_client.get("/api/v1/admin/qa/items/pruebas-uno/history", headers=master)
    campos = [e["campo"] for e in r.json()]
    assert "paso.estado" in campos and "adjunto.agregado" in campos and "prueba.nota" in campos
    assert all(e["por"] for e in r.json())

    r = await app_client.post("/api/v1/admin/qa/runs/me/submit", json={"comentario": "Listo"}, headers=master)
    assert r.status_code == 200, r.text
    enviada = r.json()
    assert enviada["estado"] == "enviada" and enviada["editable"] is False
    assert enviados and "Informe de pruebas" in enviados[0][1] and "Prueba uno" in enviados[0][2]

    r = await app_client.put(f"{base}/steps/1", json={"estado": "ok"}, headers=master)
    assert r.status_code == 409

    r = await app_client.get(f"/api/v1/admin/qa/runs/{enviada['id']}", headers=master)
    informe = r.json()["informe"]
    assert informe["pruebas"][0]["slug"] == "pruebas-uno" and informe["sin_correr"][0]["slug"] == "pruebas-dos"
    r = await app_client.get(f"/api/v1/admin/qa/runs/{enviada['id']}/report.md", headers=master)
    assert r.status_code == 200 and "### Falla · Prueba uno" in r.text


async def test_otro_tester_consolida_y_no_ve_lo_ajeno(app_client, qa_listo) -> None:
    master, t1 = qa_listo
    email = _uniq("qa-tester")
    tester_id = await _make_admin(email)
    await _grant(tester_id, "qa", "write")
    tester = await api_headers(app_client, t1, email=email, password=PASSWORD)

    await app_client.post("/api/v1/admin/qa/runs/me/start", headers=tester)
    base = "/api/v1/admin/qa/runs/me/items/pruebas-uno"
    await app_client.put(f"{base}/steps/1", json={"estado": "ok"}, headers=tester)
    r = await app_client.put(f"{base}/steps/2", json={"estado": "ok"}, headers=tester)
    assert r.json()["resultados"]["pruebas-uno"]["estado"] == "ok"

    # El master la falló: el ciclo la muestra en falla aunque este tester la aprobó.
    r = await app_client.get("/api/v1/admin/qa/catalog?seccion=pruebas", headers=tester)
    uno = next(i for i in r.json()["items"] if i["slug"] == "pruebas-uno")
    assert uno["estado"] == "falla" and uno["ejecuciones"] == {"falla": 1, "ok": 1}

    r = await app_client.get("/api/v1/admin/qa/runs", headers=tester)
    assert [x["tester"]["email"] for x in r.json()] == [email]
    r = await app_client.get("/api/v1/admin/qa/runs", headers=master)
    assert len(r.json()) == 2

    ajena = next(x for x in r.json() if x["tester"]["email"] != email)
    r = await app_client.get(f"/api/v1/admin/qa/runs/{ajena['id']}", headers=tester)
    assert r.status_code == 404

    # Solo el master importa el catálogo y cierra el ciclo.
    r = await app_client.post("/api/v1/admin/qa/catalog/import", json={"contenido": CATALOGO}, headers=tester)
    assert r.status_code == 403
    r = await app_client.post("/api/v1/admin/qa/cycles/close", json={"nombre": "x"}, headers=tester)
    assert r.status_code == 403

    # El historial no muestra las notas de la ejecución del master a otro tester.
    r = await app_client.get("/api/v1/admin/qa/items/pruebas-uno/history", headers=tester)
    textos = [e["despues"] for e in r.json()]
    assert "No pasa nada" not in textos and "Revisar el botón" not in textos
    assert all(e["por"] != "admin@local" for e in r.json() if e["run_id"])
    r = await app_client.get("/api/v1/admin/qa/items/pruebas-uno/history", headers=master)
    assert "No pasa nada" in [e["despues"] for e in r.json()]


async def test_config_y_cierre_de_ciclo(app_client, qa_listo) -> None:
    master, _ = qa_listo
    r = await app_client.put("/api/v1/admin/qa/config/test-smtp", json={"estado": "listo", "nota": "Brevo"}, headers=master)
    assert r.status_code == 200 and r.json()["estado"] == "listo"
    r = await app_client.put("/api/v1/admin/qa/config/pruebas-uno", json={"estado": "listo"}, headers=master)
    assert r.status_code == 404

    r = await app_client.post("/api/v1/admin/qa/cycles/close", json={"nombre": "Release 2026-11"}, headers=master)
    assert r.status_code == 200, r.text
    r = await app_client.get("/api/v1/admin/qa/cycles", headers=master)
    ciclos = r.json()
    assert ciclos[0]["nombre"] == "Release 2026-11" and ciclos[0]["abierto"] is True
    assert ciclos[0]["progreso"]["sin_correr"] == 2
    cerrado = ciclos[1]
    assert cerrado["abierto"] is False and cerrado["por_prueba"]["pruebas-uno"] == "falla"
    assert len(cerrado["ejecuciones"]) == 2

    # La config no se reinicia con el ciclo; las pruebas sí.
    r = await app_client.get("/api/v1/admin/qa/catalog", headers=master)
    items = {i["slug"]: i for i in r.json()["items"]}
    assert items["test-smtp"]["estado"] == "listo" and items["pruebas-uno"]["estado"] == "sin_correr"
    r = await app_client.get("/api/v1/admin/qa/runs/me", headers=master)
    assert r.json() is None


async def test_historial_es_de_solo_agregar_para_el_runtime(qa_listo) -> None:
    engine = create_async_engine(os.environ["MIGRATIONS_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            fila = (
                await conn.execute(
                    text(
                        "select has_table_privilege('app_runtime', 'qa_events', 'UPDATE'), "
                        "has_table_privilege('app_runtime', 'qa_events', 'DELETE'), "
                        "has_table_privilege('app_runtime', 'qa_events', 'INSERT'), "
                        "has_table_privilege('app_runtime', 'qa_runs', 'DELETE')"
                    )
                )
            ).one()
    finally:
        await engine.dispose()
    assert tuple(fila) == (False, False, True, False)


@pytest.mark.aislamiento
async def test_colaborador_de_negocio_no_entra_a_pruebas(app_client, qa_listo) -> None:
    _, t1 = qa_listo
    email = _uniq("qa-owner")
    owner_id = await _make_admin(email)
    await _link(owner_id, t1, "owner")
    owner = await api_headers(app_client, t1, email=email, password=PASSWORD)

    r = await app_client.get("/api/v1/admin/me", headers=owner)
    assert r.json()["permissions"].get("qa") is None
    for metodo, ruta in (
        ("get", "/api/v1/admin/qa/catalog"),
        ("get", "/api/v1/admin/qa/runs"),
        ("post", "/api/v1/admin/qa/runs/me/start"),
        ("get", "/api/v1/admin/qa/cycles"),
    ):
        r = await getattr(app_client, metodo)(ruta, headers=owner)
        assert r.status_code == 403, f"{metodo} {ruta}: {r.status_code}"
