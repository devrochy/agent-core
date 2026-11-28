"""HU-FB-03 · enviar un reporte con evidencia y ver los propios, con la base, la RLS y el almacenamiento REALES.

- Envío: 201 con el reporte en `recibido` y la captura en `<slug>/feedback/<report_id>/`; el detalle la sirve con URL
  firmada. Firma falsa → 422 `adjunto_invalido` sin guardar nada; demasiados archivos → 422; archivo o total
  demasiado grandes → 413.
- Idempotencia: la misma `Idempotency-Key` devuelve 200 y un solo reporte; sin clave, 400; la clave de otro, 409.
- Lo propio: un colaborador no ve los de otro (404 en el detalle); el owner sí con `alcance=negocio`; otro negocio, 404.
- Cupo: el sexto envío de la hora responde 429 con `Retry-After`.
- Limpieza: si falla el almacenamiento o la transacción, se borran los archivos del envío y no queda fila.
- Esquema: RLS forzada, CHECKs de la decisión, inmutabilidad del decidido y sin borrado, funciones definer que solo
  devuelven ids, `purge_tenant` (tablas y archivos) y la migración sube y baja.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from _ayudas_sql import _sql
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = [
    pytest.mark.aislamiento,  # reportes por negocio (ADR-0213, RLS)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
PW = "Colab-pass-123"
URL = "/api/v1/feedback/reports"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%prueba\n"
EXE = b"MZ\x90\x00" + b"\x00" * 64


@pytest.fixture(autouse=True)
def bandera(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "feedback_enabled", True)


async def _negocio() -> tuple[uuid.UUID, str]:
    from _habilidades import onboard_tenant

    slug = uniq("fb03")
    tid, _ = await onboard_tenant(name=f"Reportes {slug}", slug=slug, sembrar_ejemplos=False, plan="basic")
    return tid, slug


async def _usuario(tid: uuid.UUID, rol: str = "colaborador") -> tuple[uuid.UUID, str]:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('fb03')}-{rol}@test.local"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PW), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role=rol))
        return admin.id, email


async def _cabeceras(app_client, tid: uuid.UUID, email: str) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return await api_headers(app_client, tid, email=email, password=PW)


def _form(**extra: str) -> dict[str, str]:
    return {
        "tipo": "error",
        "area": "dashboard",
        "titulo": "No carga el resumen",
        "descripcion": "Al abrir el resumen la página queda en blanco.",
        **extra,
    }


async def _enviar(  # type: ignore[no-untyped-def]
    app_client,
    headers: dict[str, str],
    archivos: list[tuple[str, bytes, str]] | None = None,
    *,
    clave: str | None = None,
    **form: str,
):
    cabeceras = dict(headers)
    cabeceras["Idempotency-Key"] = clave or uuid.uuid4().hex
    files = [("adjuntos", (nombre, contenido, mime)) for nombre, contenido, mime in (archivos or [])]
    return await app_client.post(URL, data=_form(**form), files=files or None, headers=cabeceras)


async def _filas(tid: uuid.UUID) -> int:
    return (await _sql(tid, "select count(*) from feedback_reports where tenant_id = :t", t=tid))[0][0]


def _carpeta(slug: str) -> Path:
    from core.onboarding import tenants_root

    return tenants_root() / slug / "feedback"


def _archivos(slug: str) -> list[Path]:
    carpeta = _carpeta(slug)
    return [p for p in carpeta.rglob("*") if p.is_file()] if carpeta.exists() else []


# --------------------------------------------------------------------------- envío


async def test_un_colaborador_envia_un_error_con_captura(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, slug = await _negocio()
    autor, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)

    r = await _enviar(app_client, h, [("captura.png", PNG, "image/png"), ("log.pdf", PDF, "application/pdf")])
    assert r.status_code == 201, r.text
    reporte = r.json()
    assert reporte["estado"] == "recibido"
    assert reporte["autor_admin_id"] == str(autor)
    assert [a["mimetype"] for a in reporte["adjuntos"]] == ["image/png", "application/pdf"]
    # El cliente nunca recibe el revisor, la nota interna ni el id del duplicado.
    for oculto in ("revisado_por", "nota_interna", "duplicado_de", "idempotency_key"):
        assert oculto not in reporte

    guardados = _archivos(slug)
    assert len(guardados) == 2
    assert {p.parent.name for p in guardados} == {reporte["id"]}

    detalle = await app_client.get(f"{URL}/{reporte['id']}", headers=h)
    assert detalle.status_code == 200
    url = detalle.json()["adjuntos"][0]["url"]
    descarga = await app_client.get("/api/v1/files/" + url.rsplit("/api/v1/files/", 1)[1])
    assert descarga.status_code == 200 and descarga.content == PNG


async def test_firma_falsa_da_422_y_no_guarda_nada(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)

    r = await _enviar(app_client, h, [("ok.png", PNG, "image/png"), ("virus.png", EXE, "image/png")])
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "adjunto_invalido"
    assert r.json()["error"]["details"]["adjunto"] == 1
    assert await _filas(tid) == 0
    assert _archivos(slug) == []


async def test_limites_de_cantidad_y_tamano(app_client) -> None:  # type: ignore[no-untyped-def]
    from core.feedback import catalogo

    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)

    seis = [(f"c{i}.png", PNG + bytes([i]), "image/png") for i in range(6)]
    r = await _enviar(app_client, h, seis)
    assert (r.status_code, r.json()["error"]["code"]) == (422, "demasiados_adjuntos")

    grande = PNG + b"\x00" * catalogo.MAX_BYTES_POR_ARCHIVO
    r = await _enviar(app_client, h, [("grande.png", grande, "image/png")])
    assert (r.status_code, r.json()["error"]["code"]) == (413, "adjunto_muy_grande")

    siete_mb = 7 * 1024 * 1024
    cuatro = [(f"t{i}.png", PNG + bytes([i]) * siete_mb, "image/png") for i in range(4)]
    r = await _enviar(app_client, h, cuatro)
    assert (r.status_code, r.json()["error"]["code"]) == (413, "adjuntos_muy_grandes")

    r = await _enviar(app_client, h, [("a.png", PNG, "image/png"), ("b.png", PNG, "image/png")])
    assert (r.status_code, r.json()["error"]["code"]) == (422, "adjunto_repetido")

    r = await _enviar(app_client, h, area="no_existe")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "area_invalida")
    r = await _enviar(app_client, h, tipo="queja")
    assert (r.status_code, r.json()["error"]["code"]) == (422, "tipo_invalido")
    assert await _filas(tid) == 0
    assert _archivos(slug) == []


async def test_sin_bandera_responde_404(app_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.config import get_settings

    tid, _ = await _negocio()
    h = await api_headers(app_client, tid)
    monkeypatch.setattr(get_settings(), "feedback_enabled", False)
    assert (await _enviar(app_client, h)).status_code == 404
    assert (await app_client.get(URL, headers=h)).status_code == 404
    assert (await app_client.get("/api/v1/feedback/catalogo", headers=h)).status_code == 404


async def test_catalogo_trae_tipos_areas_y_limites(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, _ = await _negocio()
    _, email = await _usuario(tid)
    r = await app_client.get("/api/v1/feedback/catalogo", headers=await _cabeceras(app_client, tid, email))
    assert r.status_code == 200
    cuerpo = r.json()
    assert [t["clave"] for t in cuerpo["tipos"]] == ["error", "mejora", "idea"]
    areas = [a["clave"] for a in cuerpo["areas"]]
    assert "dashboard" in areas and areas[-1] == "otra"
    assert "tenants" not in areas and "qa" not in areas
    assert cuerpo["limites"]["max_adjuntos"] == 5


# --------------------------------------------------------------------------- idempotencia


async def test_el_replay_devuelve_el_mismo_reporte(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    _, otro = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)
    clave = uuid.uuid4().hex

    primero = await _enviar(app_client, h, [("c.png", PNG, "image/png")], clave=clave)
    segundo = await _enviar(app_client, h, [("c.png", PNG, "image/png")], clave=clave)
    assert (primero.status_code, segundo.status_code) == (201, 200)
    assert segundo.json()["id"] == primero.json()["id"]
    assert await _filas(tid) == 1
    assert len(_archivos(slug)) == 1

    ajeno = await _enviar(app_client, await _cabeceras(app_client, tid, otro), clave=clave)
    assert (ajeno.status_code, ajeno.json()["error"]["code"]) == (409, "idempotency_key_en_uso")

    sin_clave = dict(h)
    r = await app_client.post(URL, data=_form(), headers=sin_clave)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "idempotency_key_requerida")
    assert await _filas(tid) == 1


async def test_dos_envios_simultaneos_con_la_misma_clave_dejan_uno(monkeypatch) -> None:
    """Carrera: los dos pasan la lectura previa; el segundo choca con el índice único, limpia y devuelve el primero."""
    import asyncio

    from core.feedback import reportes as svc

    tid, slug = await _negocio()
    autor, _ = await _usuario(tid)
    original = svc._replay
    barrera = asyncio.Barrier(2)

    async def _replay_lento(*args: Any, **kwargs: Any) -> Any:
        resultado = await original(*args, **kwargs)
        if resultado is None:
            await barrera.wait()
        return resultado

    monkeypatch.setattr(svc, "_replay", _replay_lento)
    clave = uuid.uuid4().hex

    def _envio(contenido: bytes) -> Any:
        return svc.enviar_reporte(
            tenant_id=tid,
            autor_admin_id=autor,
            idempotency_key=clave,
            tipo="idea",
            area="otra",
            titulo="Modo oscuro",
            descripcion="Sería útil un modo oscuro en el panel.",
            adjuntos=[svc.AdjuntoEntrada("c.png", "image/png", contenido)],
        )

    (a, creado_a), (b, creado_b) = await asyncio.gather(_envio(PNG + b"1"), _envio(PNG + b"2"))
    assert a["id"] == b["id"]
    assert sorted([creado_a, creado_b]) == [False, True]
    assert await _filas(tid) == 1
    assert len(_archivos(slug)) == 1


# --------------------------------------------------------------------------- lo propio


async def test_cada_quien_ve_lo_suyo_y_el_owner_todo_el_negocio(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, _ = await _negocio()
    otro_tid, _ = await _negocio()
    _, email_a = await _usuario(tid)
    _, email_b = await _usuario(tid)
    _, email_owner = await _usuario(tid, "owner")
    _, email_otro = await _usuario(otro_tid, "owner")
    ha = await _cabeceras(app_client, tid, email_a)
    hb = await _cabeceras(app_client, tid, email_b)
    ho = await _cabeceras(app_client, tid, email_owner)

    de_a = (await _enviar(app_client, ha, titulo="Reporte de A")).json()
    de_b = (await _enviar(app_client, hb, titulo="Reporte de B")).json()

    lista_a = (await app_client.get(URL, headers=ha)).json()
    assert [r["id"] for r in lista_a["items"]] == [de_a["id"]]
    assert (await app_client.get(f"{URL}/{de_b['id']}", headers=ha)).status_code == 404
    r = await app_client.get(URL, params={"alcance": "negocio"}, headers=ha)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "alcance_no_permitido")

    todos = (await app_client.get(URL, params={"alcance": "negocio"}, headers=ho)).json()
    assert {r["id"] for r in todos["items"]} == {de_a["id"], de_b["id"]}
    assert (await app_client.get(f"{URL}/{de_a['id']}", headers=ho)).status_code == 200
    # El owner sin `alcance` ve solo los suyos.
    assert (await app_client.get(URL, headers=ho)).json()["items"] == []

    # Otro negocio: 404 en el detalle y nada en la lista, aunque sea su owner.
    ho_otro = await _cabeceras(app_client, otro_tid, email_otro)
    assert (await app_client.get(f"{URL}/{de_a['id']}", headers=ho_otro)).status_code == 404
    assert (await app_client.get(URL, params={"alcance": "negocio"}, headers=ho_otro)).json()["items"] == []


async def test_la_lista_pagina_por_cursor(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, _ = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)
    ids = [(await _enviar(app_client, h, titulo=f"Reporte {i}")).json()["id"] for i in range(3)]

    pagina = (await app_client.get(URL, params={"limit": 2}, headers=h)).json()
    assert len(pagina["items"]) == 2 and pagina["next_cursor"]
    resto = (await app_client.get(URL, params={"limit": 2, "cursor": pagina["next_cursor"]}, headers=h)).json()
    assert resto["next_cursor"] is None
    assert [r["id"] for r in pagina["items"] + resto["items"]] == list(reversed(ids))
    r = await app_client.get(URL, params={"cursor": "basura"}, headers=h)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "cursor_invalido")


# --------------------------------------------------------------------------- cupo y limpieza


async def test_el_sexto_envio_de_la_hora_da_429_con_retry_after(app_client) -> None:  # type: ignore[no-untyped-def]
    tid, _ = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)
    for i in range(5):
        assert (await _enviar(app_client, h, titulo=f"Reporte {i}")).status_code == 201
    r = await _enviar(app_client, h, titulo="Reporte 6")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "demasiados_reportes"
    assert r.headers["Retry-After"] == "3600"
    assert await _filas(tid) == 5


async def test_si_falla_el_almacenamiento_se_borra_lo_escrito(app_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core import almacenamiento

    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)
    original = almacenamiento.guardar
    llamadas = {"n": 0}

    def _guardar_falla_al_segundo(*args: Any, **kwargs: Any) -> Any:
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            raise OSError("disco lleno")
        return original(*args, **kwargs)

    monkeypatch.setattr(almacenamiento, "guardar", _guardar_falla_al_segundo)
    r = await _enviar(app_client, h, [("a.png", PNG, "image/png"), ("b.pdf", PDF, "application/pdf")])
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "almacenamiento_no_disponible"
    assert "Retry-After" in r.headers
    assert await _filas(tid) == 0
    assert _archivos(slug) == []


async def test_si_falla_la_transaccion_se_borran_los_archivos(app_client, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.feedback import reportes as svc

    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)

    class _Rompe:
        def __init__(self, **_kwargs: Any) -> None:
            raise RuntimeError("falla simulada en la transacción")

    monkeypatch.setattr(svc, "FeedbackAttachment", _Rompe)
    with pytest.raises(RuntimeError):
        await _enviar(app_client, h, [("a.png", PNG, "image/png")])
    assert await _filas(tid) == 0
    assert _archivos(slug) == []


# --------------------------------------------------------------------------- esquema


async def _reporte_directo(tid: uuid.UUID, autor: uuid.UUID) -> uuid.UUID:
    rid = uuid.uuid4()
    await _sql(
        tid,
        "insert into feedback_reports (id, tenant_id, autor_admin_id, tipo, area, titulo, descripcion, idempotency_key)"
        " values (:id, :t, :a, 'mejora', 'otra', 'Un título', 'Una descripción larga', :k)",
        id=rid, t=tid, a=autor, k=uuid.uuid4().hex,
    )
    return rid


async def test_rls_aisla_los_reportes_entre_negocios() -> None:
    a, _ = await _negocio()
    b, _ = await _negocio()
    autor, _ = await _usuario(a)
    rid = await _reporte_directo(a, autor)
    await _sql(
        a,
        "insert into feedback_attachments (tenant_id, report_id, orden, storage_key, mimetype, bytes, sha256)"
        " values (:t, :r, 0, 'x/feedback/y/z.png', 'image/png', 10, :s)",
        t=a, r=rid, s="a" * 64,
    )
    for tabla in ("feedback_reports", "feedback_attachments"):
        assert await _sql(b, f"select 1 from {tabla}") == [], f"{tabla} se filtró a otro negocio"
    assert await _sql(None, "select 1 from feedback_reports where id = :r", r=rid) == []

    with pytest.raises(DBAPIError):
        await _reporte_directo_en(b, a, autor)


async def _reporte_directo_en(scope: uuid.UUID, tid: uuid.UUID, autor: uuid.UUID) -> None:
    await _sql(
        scope,
        "insert into feedback_reports (tenant_id, autor_admin_id, tipo, area, titulo, descripcion, idempotency_key)"
        " values (:t, :a, 'mejora', 'otra', 'Un título', 'Una descripción larga', :k)",
        t=tid, a=autor, k=uuid.uuid4().hex,
    )


async def test_un_reporte_decidido_es_inmutable_y_nada_se_borra() -> None:
    tid, _ = await _negocio()
    autor, _ = await _usuario(tid)
    revisor = uuid.uuid4()
    rid = await _reporte_directo(tid, autor)

    # CHECK: aprobar sin créditos o rechazar sin motivo no entra.
    with pytest.raises(DBAPIError):
        await _sql(tid, "update feedback_reports set estado = 'aprobado', revisado_por = :v, decidido_en = now() "
                        "where id = :r", v=revisor, r=rid)
    with pytest.raises(DBAPIError):
        await _sql(tid, "update feedback_reports set estado = 'rechazado', revisado_por = :v, decidido_en = now(), "
                        "motivo_codigo = 'duplicado' where id = :r", v=revisor, r=rid)

    await _sql(tid, "update feedback_reports set estado = 'aprobado', revisado_por = :v, decidido_en = now(), "
                    "creditos_otorgados = 500 where id = :r", v=revisor, r=rid)
    with pytest.raises(DBAPIError, match="no se puede modificar"):
        await _sql(tid, "update feedback_reports set creditos_otorgados = 5000 where id = :r", r=rid)
    with pytest.raises(DBAPIError):
        await _sql(tid, "delete from feedback_reports where id = :r", r=rid)

    otro = await _reporte_directo(tid, autor)
    await _sql(
        tid,
        "insert into feedback_attachments (tenant_id, report_id, orden, storage_key, mimetype, bytes, sha256)"
        " values (:t, :r, 0, 'x/feedback/y/z.png', 'image/png', 10, :s)",
        t=tid, r=otro, s="b" * 64,
    )
    with pytest.raises(DBAPIError):
        await _sql(tid, "update feedback_attachments set bytes = 11 where report_id = :r", r=otro)
    with pytest.raises(DBAPIError):
        await _sql(tid, "delete from feedback_attachments where report_id = :r", r=otro)
    assert (await _sql(tid, "select creditos_otorgados from feedback_reports where id = :r", r=rid)) == [(500,)]


async def test_las_funciones_definer_solo_devuelven_ids() -> None:
    a, _ = await _negocio()
    b, _ = await _negocio()
    ra = await _reporte_directo(a, (await _usuario(a))[0])
    rb = await _reporte_directo(b, (await _usuario(b))[0])

    filas = await _sql(None, "select * from feedback_bandeja('recibido', null, null, null, null, 100)")
    ids = {f[0] for f in filas}
    assert {ra, rb} <= ids
    assert all(len(f) == 5 for f in filas)
    solo_a = await _sql(None, "select id from feedback_bandeja(null, null, :t, null, null, 100)", t=a)
    assert [f[0] for f in solo_a] == [ra]
    assert await _sql(None, "select feedback_reporte_tenant(:r)", r=rb) == [(b,)]
    assert await _sql(None, "select feedback_reporte_tenant(:r)", r=uuid.uuid4()) == [(None,)]
    # PUBLIC no puede ejecutarlas: solo `app_runtime` (y el dueño).
    sin_public = await _sql(
        None,
        "select count(*) from pg_proc p where p.proname in ('feedback_bandeja', 'feedback_reporte_tenant') "
        "and has_function_privilege('public', p.oid, 'execute')",
    )
    assert sin_public == [(0,)]


async def test_purge_tenant_borra_reportes_adjuntos_y_archivos(app_client) -> None:  # type: ignore[no-untyped-def]
    from core.tenants import deactivate_tenant, purge_tenant

    tid, slug = await _negocio()
    _, email = await _usuario(tid)
    h = await _cabeceras(app_client, tid, email)
    r = await _enviar(app_client, h, [("c.png", PNG, "image/png")])
    assert r.status_code == 201
    assert _archivos(slug)

    await deactivate_tenant(str(tid))
    await purge_tenant(str(tid), f"ELIMINAR {slug}")
    async with _como_dueno() as conn:
        for tabla in ("feedback_reports", "feedback_attachments"):
            n = (await conn.execute(text(f"select count(*) from {tabla} where tenant_id = :t"), {"t": tid})).scalar()
            assert n == 0, tabla
    assert not _carpeta(slug).exists()


class _como_dueno:
    """Conexión como dueño de las tablas (sin RLS)."""

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        from sqlalchemy.ext.asyncio import create_async_engine

        from core.config import get_settings

        settings = get_settings()
        self._engine = create_async_engine(settings.migrations_database_url or settings.database_url)
        self._ctx = self._engine.begin()
        return await self._ctx.__aenter__()

    async def __aexit__(self, *exc: Any) -> None:
        await self._ctx.__aexit__(*exc)
        await self._engine.dispose()


def _alembic(*args: str) -> None:
    resultado = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=RAIZ, capture_output=True, text=True, timeout=300, check=False
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]


async def _existen() -> set[str]:
    async with _como_dueno() as conn:
        filas = (
            await conn.execute(
                text(
                    "select tablename from pg_tables where tablename in ('feedback_reports', 'feedback_attachments') "
                    "union all select proname from pg_proc where proname in "
                    "('feedback_bandeja', 'feedback_reporte_tenant', 'feedback_reports_decidido_inmutable')"
                )
            )
        ).all()
    return {f[0] for f in filas}


async def test_la_migracion_0162_sube_y_baja() -> None:
    todo = {
        "feedback_reports",
        "feedback_attachments",
        "feedback_bandeja",
        "feedback_reporte_tenant",
        "feedback_reports_decidido_inmutable",
    }
    assert await _existen() == todo
    try:
        _alembic("downgrade", "0162-1")
        assert await _existen() == set()
    finally:
        _alembic("upgrade", "head")
    assert await _existen() == todo
