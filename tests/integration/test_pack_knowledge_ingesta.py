"""Ingesta del pack de conocimiento contra el stack real (ADR-0145 §2, §6 y §7; HU-AOS-49 parte 2).

Criterio «Subir un documento»: dado un agente, al subir un documento queda procesado y listado con
su estado. Además, los tests de aislamiento que exige la parte 49-2:

- la API con un agente o documento de otro negocio responde 404 en subir, listar, borrar y
  reprocesar;
- una tarea con el `tenant_id` de A y el documento de B no hace nada;
- el original vive en la carpeta del tenant, y borrar vacía el contenido y el archivo;
- el tope del plan cuenta solo los documentos del negocio (409 `plan_limit_documentos`);
- purgar el negocio borra su carpeta, y un alta nueva con el mismo slug manda sus datos a
  cuarentena.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import func, select, text

pytestmark = [
    pytest.mark.aislamiento,  # subconjunto de las PRs a develop (ADR-0120)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

FAQ = (
    "# Envíos\nHacemos envíos a todo el país en 3 días hábiles. El envío es gratis desde 100.000 pesos.\n\n"
    "# Cambios\nAceptamos cambios hasta 30 días después de la compra, con la factura.\n"
)


@pytest.fixture
def raiz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """TENANTS_ROOT temporal: los originales del test no tocan el volumen real."""
    from core.config import get_settings

    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def cola(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Captura lo que la ingesta encola (sin broker): `_vaciar_cola` lo corre en orden."""
    from pack_knowledge import ingesta

    encoladas: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(ingesta.task_queue, "enqueue", lambda nombre, **kw: encoladas.append((nombre, kw)))
    return encoladas


async def _vaciar_cola(encoladas: list[tuple[str, dict[str, Any]]]) -> list[str]:
    from pack_knowledge import ingesta

    corridas: list[str] = []
    while encoladas:
        nombre, kwargs = encoladas.pop(0)
        corridas.append(nombre)
        # El broker es Redis: solo ids, nunca texto del documento.
        esperado = {"tenant_id", "document_id"} | ({"sha256"} if nombre == ingesta.TAREA_EMBEBER else set())
        assert set(kwargs) == esperado, kwargs
        tarea = {ingesta.TAREA_EXTRAER: ingesta.tarea_extraer, ingesta.TAREA_EMBEBER: ingesta.tarea_embeber}[nombre]
        await tarea(**kwargs)
    return corridas


async def _negocio() -> tuple[uuid.UUID, str, uuid.UUID]:
    """Negocio dado de alta por el camino real, con un especialista. Devuelve (tenant, slug, agente)."""
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    slug = uniq("kb")
    tid, _ = await onboard_tenant(name=f"Conocimiento {slug}", slug=slug, persona={"tone": "cercano"})
    agente = uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            await session.execute(
                text("insert into agents (id, tenant_id, key, kind, name) values (:id, :t, 'ventas', 'specialist', 'Ventas')"),
                {"id": agente, "t": tid},
            )
    return tid, slug, agente


async def _subir(app_client, headers, agente, contenido: bytes, nombre="faq.md", mime="text/markdown"):  # type: ignore[no-untyped-def]
    return await app_client.post(
        f"/api/v1/pack/pack_knowledge/agents/{agente}/documents",
        headers=headers,
        files={"archivo": (nombre, contenido, mime)},
    )


async def test_subir_procesar_listar_y_borrar(app_client, raiz, cola) -> None:
    from pack_knowledge.models import KnowledgeChunk, KnowledgeDocument

    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, slug, agente = await _negocio()
    headers = await api_headers(app_client, tid)

    r = await _subir(app_client, headers, agente, FAQ.encode())
    assert r.status_code == 202, r.text
    doc = r.json()
    assert (doc["estado"], doc["titulo"], doc["mime"]) == ("pendiente", "faq.md", "text/markdown")
    original = raiz / slug / "conocimiento" / f"{doc['id']}.md"
    assert original.read_bytes() == FAQ.encode()

    assert await _vaciar_cola(cola) == ["pack_knowledge.extraer", "pack_knowledge.embeber"]

    r = await app_client.get(f"/api/v1/pack/pack_knowledge/agents/{agente}/documents", headers=headers)
    assert r.status_code == 200
    [listado] = r.json()
    assert listado["estado"] == "listo"
    assert listado["num_fragmentos"] == 2
    assert listado["error_codigo"] is None
    assert listado["desactualizado"] is False

    async with tenant_scope(tid):
        async with get_session() as session:
            fragmentos = (
                (await session.execute(select(KnowledgeChunk).order_by(KnowledgeChunk.ordinal))).scalars().all()
            )
            assert [f.seccion for f in fragmentos] == ["Envíos", "Cambios"]
            assert all(f.embedding is not None for f in fragmentos)
            fila = (await session.execute(select(UsageLedger).where(UsageLedger.kind == "embedding"))).scalar_one()
            assert fila.result == {"proposito": "ingesta", "document_id": doc["id"]}
            guardado = await session.get(KnowledgeDocument, uuid.UUID(doc["id"]))
            assert guardado.tokens_embebidos == fila.input_tokens > 0

    # El mismo archivo otra vez: 409 sin crear nada.
    r = await _subir(app_client, headers, agente, FAQ.encode())
    assert r.status_code == 409 and r.json()["error"]["code"] == "documento_duplicado"

    r = await app_client.delete(f"/api/v1/pack/pack_knowledge/agents/{agente}/documents/{doc['id']}", headers=headers)
    assert r.status_code == 204
    assert not original.exists()
    async with tenant_scope(tid):
        async with get_session() as session:
            vivos = (await session.execute(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.deleted_at.is_(None)))).scalar_one()
            assert vivos == 0
            contenidos = (await session.execute(select(KnowledgeChunk.contenido, KnowledgeChunk.embedding))).all()
            assert all(c is None and e is None for c, e in contenidos)
    r = await app_client.get(f"/api/v1/pack/pack_knowledge/agents/{agente}/documents", headers=headers)
    assert r.json() == []


async def test_pdf_escaneado_queda_fallido_y_se_puede_reprocesar(app_client, raiz, cola) -> None:
    tid, _, agente = await _negocio()
    headers = await api_headers(app_client, tid)
    from _pdf_minimo import pdf_minimo

    pdf_sin_texto = pdf_minimo([""])
    r = await _subir(app_client, headers, agente, pdf_sin_texto, nombre="escaneado.pdf", mime="application/pdf")
    assert r.status_code == 202, r.text
    doc_id = r.json()["id"]
    await _vaciar_cola(cola)
    [listado] = (await app_client.get(f"/api/v1/pack/pack_knowledge/agents/{agente}/documents", headers=headers)).json()
    assert (listado["estado"], listado["error_codigo"]) == ("fallido", "sin_texto")

    r = await app_client.post(
        f"/api/v1/pack/pack_knowledge/agents/{agente}/documents/{doc_id}/reprocess", headers=headers
    )
    assert r.status_code == 202 and r.json()["estado"] == "pendiente"
    assert [n for n, _ in cola] == ["pack_knowledge.extraer"]


async def test_tipo_no_admitido_y_contenido_falso(app_client, raiz, cola) -> None:
    tid, _, agente = await _negocio()
    headers = await api_headers(app_client, tid)
    r = await _subir(app_client, headers, agente, b"\x89PNG\r\n\x1a\n", nombre="foto.png", mime="image/png")
    assert r.status_code == 422 and r.json()["error"]["code"] == "formato"
    r = await _subir(app_client, headers, agente, b"no soy un pdf", nombre="faq.pdf", mime="application/pdf")
    assert r.status_code == 422 and r.json()["error"]["code"] == "formato"
    assert cola == []


async def test_otro_negocio_responde_404_en_todo(app_client, raiz, cola) -> None:
    tid_a, _, agente_a = await _negocio()
    tid_b, _, agente_b = await _negocio()
    headers_a = await api_headers(app_client, tid_a)
    headers_b = await api_headers(app_client, tid_b)
    doc_b = (await _subir(app_client, headers_b, agente_b, FAQ.encode())).json()["id"]
    cola.clear()

    base = "/api/v1/pack/pack_knowledge/agents"
    for r in (
        await app_client.get(f"{base}/{agente_b}/documents", headers=headers_a),
        await _subir(app_client, headers_a, agente_b, b"Otro texto con suficientes letras."),
        await app_client.delete(f"{base}/{agente_b}/documents/{doc_b}", headers=headers_a),
        await app_client.post(f"{base}/{agente_b}/documents/{doc_b}/reprocess", headers=headers_a),
        # El documento de B por el agente de A tampoco.
        await app_client.delete(f"{base}/{agente_a}/documents/{doc_b}", headers=headers_a),
    ):
        assert r.status_code == 404, r.text
        assert doc_b not in r.text and str(agente_b) not in r.text
    assert cola == []
    [sigue] = (await app_client.get(f"{base}/{agente_b}/documents", headers=headers_b)).json()
    assert sigue["estado"] == "pendiente"


async def test_tarea_con_tenant_ajeno_no_hace_nada(app_client, raiz, cola) -> None:
    from pack_knowledge import ingesta

    tid_a, _, _ = await _negocio()
    tid_b, _, agente_b = await _negocio()
    headers_b = await api_headers(app_client, tid_b)
    doc_b = (await _subir(app_client, headers_b, agente_b, FAQ.encode())).json()["id"]
    cola.clear()
    await ingesta.tarea_extraer(tenant_id=str(tid_a), document_id=doc_b)
    await ingesta.tarea_embeber(tenant_id=str(tid_a), document_id=doc_b, sha256="0" * 64)
    assert cola == []
    [sigue] = (await app_client.get(f"/api/v1/pack/pack_knowledge/agents/{agente_b}/documents", headers=headers_b)).json()
    assert (sigue["estado"], sigue["num_fragmentos"]) == ("pendiente", 0)


async def test_tope_del_plan_cuenta_solo_el_negocio(app_client, raiz, cola) -> None:
    from core.db.session import get_session

    tid_a, _, agente_a = await _negocio()
    tid_b, _, agente_b = await _negocio()
    from core.billing.service import ensure_free_subscription
    from core.shared.tenant import tenant_scope

    for tid in (tid_a, tid_b):
        async with tenant_scope(tid):
            async with get_session() as session:
                # Plan free: 3 documentos (ADR-0145 §2).
                assert (await ensure_free_subscription(session, tid)).plan_id == "free"
    headers_a = await api_headers(app_client, tid_a)
    headers_b = await api_headers(app_client, tid_b)
    for i in range(3):
        r = await _subir(app_client, headers_b, agente_b, f"Documento {i} de B con texto suficiente.".encode(), nombre=f"b{i}.txt", mime="text/plain")
        assert r.status_code == 202, r.text
    for i in range(3):
        r = await _subir(app_client, headers_a, agente_a, f"Documento {i} de A con texto suficiente.".encode(), nombre=f"a{i}.txt", mime="text/plain")
        assert r.status_code == 202, r.text
    r = await _subir(app_client, headers_a, agente_a, b"El cuarto documento de A no entra.", nombre="a3.txt", mime="text/plain")
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_documentos"
    assert error["details"] == {"metric": "documentos", "used": 3, "limit": 3}


async def test_purga_borra_la_carpeta_y_el_alta_nueva_no_hereda(app_client, raiz, cola) -> None:
    from core import tenants as tenants_mod
    from core.alta_negocio import onboard_tenant
    from core.db.session import get_session

    tid, slug, agente = await _negocio()
    headers = await api_headers(app_client, tid)
    assert (await _subir(app_client, headers, agente, FAQ.encode())).status_code == 202
    carpeta = raiz / slug
    assert (carpeta / "conocimiento").is_dir()

    async with get_session() as session:
        await session.execute(text("update tenants set status = 'suspended' where id = :t"), {"t": tid})
    assert await tenants_mod.purge_tenant(slug, f"ELIMINAR {slug}") is True
    assert not carpeta.exists()

    # Un residuo (p. ej. una purga cuyo borrado de disco falló) no lo hereda un alta nueva.
    (carpeta / "conocimiento").mkdir(parents=True)
    (carpeta / "conocimiento" / "viejo.pdf").write_bytes(b"%PDF-1.4 de otro negocio")
    (carpeta / "docs").mkdir()
    (carpeta / "docs" / "voz-de-marca.md").write_text("Preparada antes del alta", encoding="utf-8")
    await onboard_tenant(name=f"Nuevo {slug}", slug=slug, persona={"tone": "cercano"})
    assert not (carpeta / "conocimiento").exists()
    cuarentena = list((raiz / ".purgados").glob(f"{slug}-conocimiento-*"))
    assert len(cuarentena) == 1 and (cuarentena[0] / "viejo.pdf").is_file()
    # Lo que se prepara antes del alta (docs/) se queda.
    assert (carpeta / "docs" / "voz-de-marca.md").read_text(encoding="utf-8") == "Preparada antes del alta"


async def test_otro_agente_y_el_coordinador_del_mismo_negocio(app_client, raiz, cola) -> None:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid, _, ventas = await _negocio()
    agenda, coordinador = uuid.uuid4(), uuid.uuid4()
    async with tenant_scope(tid):
        async with get_session() as session:
            for aid, key, kind in ((agenda, "agenda", "specialist"), (coordinador, "coordinador", "core")):
                await session.execute(
                    text("insert into agents (id, tenant_id, key, kind, name) values (:id, :t, :k, :kind, :k)"),
                    {"id": aid, "t": tid, "k": key, "kind": kind},
                )
    headers = await api_headers(app_client, tid)
    doc = (await _subir(app_client, headers, ventas, FAQ.encode())).json()["id"]
    base = "/api/v1/pack/pack_knowledge/agents"
    # El documento de Ventas no se ve ni se borra desde Agenda.
    assert (await app_client.get(f"{base}/{agenda}/documents", headers=headers)).json() == []
    assert (await app_client.delete(f"{base}/{agenda}/documents/{doc}", headers=headers)).status_code == 404
    # El Coordinador no tiene conocimiento propio.
    assert (await _subir(app_client, headers, coordinador, FAQ.encode())).status_code == 404
    assert (await app_client.get(f"{base}/{coordinador}/documents", headers=headers)).status_code == 404
