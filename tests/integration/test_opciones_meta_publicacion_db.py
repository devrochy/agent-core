"""SPEC-40 · publicar una versión como plantilla de Meta contra Postgres y Redis REALES (HU-OPC-04, ADR-0172 §2).

Solo se simula el borde: `graph_fetch` (Meta) y las credenciales de la conexión. Todo lo demás es real: la reserva
de la fila en `message_templates`, el vínculo `option_template_versions.message_template_id` bajo el trigger de
inmutabilidad, el candado de Redis, el límite de creación, el webhook de estado de plantillas y el envío del PR 5.

Qué se prueba:
- creación (`<clave>_v<n>`, categoría visible antes de Meta, componentes que pasa el envío), rechazo local, rechazo
  de Meta, pausa y rechazo posteriores por el webhook, límite de creación y de cupo;
- nunca se edita una aprobada: v2 es `_v2`, v1 sigue ligada y válida, y no hay ninguna llamada de edición;
- reintento seguro: un fallo de red (antes o después de que Meta cree) no duplica la plantilla ni la fila;
- dos publicaciones simultáneas, aislamiento entre negocios, y el JOURNEY publicar → aprobada → envío por plantilla.

Requiere el stack local (Postgres + Redis) y `AGENT_CORE_INTEGRATION=1`."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from core.gateway.meta_errors import GraphError

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

HASH = "d" * 64


# ---------------------------------------------------------------- datos
def definicion(n: int = 3, *, body: str = "Hola {cliente.nombre}, ¿qué hacemos?", footer: str | None = "Responde con un toque") -> dict:
    return {
        "body": body,
        "footer": footer,
        "variables": ["cliente.nombre"],
        "exclusivo": True,
        "ttl_horas": 24,
        "opciones": [
            {
                "id": f"o{i}",
                "titulo": f"Opción {i}",
                "accion": {"tool": "x_tool", "args_fijos": {}, "args_contexto": {}, "confirmar": False},
                "respuesta_ok": "Listo, {ctx.cliente.nombre}.",
                "respuesta_error": "No pude.",
                "result_keys": [],
            }
            for i in range(1, n + 1)
        ],
    }


class Negocio:
    def __init__(self, tid: uuid.UUID, slug: str) -> None:
        self.tid, self.slug = tid, slug


async def _scoped(tid: uuid.UUID, fn: Callable[[Any], Any]) -> Any:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as s:
            return await fn(s)


async def _sql(tid: uuid.UUID, sql: str, **params: Any) -> list[Any]:
    async def _f(s: Any) -> list[Any]:
        r = await s.execute(text(sql), params)
        return list(r.all()) if getattr(r, "returns_rows", False) else []

    return await _scoped(tid, _f)


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[tuple[Negocio, Negocio]]:
    from core.alta_negocio import onboard_tenant
    from core.kernel.opciones import bandera
    from core.shared.tenant import tenant_scope
    from core.tenants import deactivate_tenant, purge_tenant

    salida: list[Negocio] = []
    for pref in ("mpa", "mpb"):
        slug = f"{pref}-{uuid.uuid4().hex[:8]}"
        tid, _ = await onboard_tenant(name=f"Meta {slug}", slug=slug, packs=[], sembrar_ejemplos=False)
        async with tenant_scope(tid):
            await bandera.fijar(tid, True, actor="test")
        salida.append(Negocio(tid, slug))
    yield salida[0], salida[1]
    for n in salida:
        await deactivate_tenant(str(n.tid))
        await purge_tenant(str(n.tid), f"ELIMINAR {n.slug}")


async def conexion(tid: uuid.UUID, *, cupo: tuple[int, int] | None = None) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Conexión de Meta con su WABA: `(connection_id, fila de la WABA, waba_id de Meta)`."""
    from core.db.models import MetaConnection, WabaAccount, utcnow

    async def _f(s: Any) -> tuple[uuid.UUID, uuid.UUID, str]:
        conn = MetaConnection(
            tenant_id=tid, channel="whatsapp", environment="sandbox", origin="byok", meta_app_id=f"app-{uuid.uuid4().hex[:8]}",
            status="connected", connected_at=utcnow(), health={},
        )
        s.add(conn)
        await s.flush()
        waba_id = f"waba-{uuid.uuid4().hex[:8]}"
        waba = WabaAccount(tenant_id=tid, connection_id=conn.id, waba_id=waba_id, subscribed=True)
        if cupo is not None:
            waba.message_template_count, waba.message_template_limit = cupo
        s.add(waba)
        await s.flush()
        return conn.id, waba.id, waba_id

    return await _scoped(tid, _f)


async def plantilla(tid: uuid.UUID, definiciones: list[dict], *, clave: str | None = None) -> tuple[str, list[uuid.UUID]]:
    """Una plantilla con opciones ya PUBLICADA con una versión por definición. `(clave, [version_id…])`."""
    clave = clave or f"t{uuid.uuid4().hex[:10]}"
    template_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in definiciones]

    async def _f(s: Any) -> None:
        await s.execute(
            text(
                "insert into option_templates (id, tenant_id, key, name, status, current_version_n) "
                "values (:i, :t, :k, 'x', 'published', :n)"
            ),
            {"i": template_id, "t": tid, "k": clave, "n": len(definiciones)},
        )
        for n, (vid, d) in enumerate(zip(ids, definiciones, strict=True), 1):
            await s.execute(
                text(
                    "insert into option_template_versions (id, tenant_id, option_template_id, version_n, definition, "
                    "definition_hash) values (:i, :t, :tp, :n, cast(:d as jsonb), :h)"
                ),
                {"i": vid, "t": tid, "tp": template_id, "n": n, "d": json.dumps(d), "h": HASH},
            )

    await _scoped(tid, _f)
    return clave, ids


class MetaFalsa:
    """Meta simulado en `graph_fetch`: guarda las plantillas creadas y registra cada llamada. Los fallos son de una
    sola vez: `falla_antes` (el POST no llega a Meta), `falla_despues` (Meta crea y la respuesta se pierde)."""

    def __init__(self) -> None:
        self.llamadas: list[tuple[str, str, Any, Any]] = []
        self.plantillas: dict[str, dict[str, Any]] = {}
        self.falla_antes: list[GraphError] = []
        self.falla_despues: list[GraphError] = []
        self.pausa_s = 0.0
        self.id_fijo: str | None = None

    @property
    def posts(self) -> list[tuple[str, str, Any, Any]]:
        return [c for c in self.llamadas if c[0] == "POST"]

    async def __call__(self, path: str, *, method: str = "GET", body: Any = None, query: Any = None, **_k: Any) -> Any:
        self.llamadas.append((method, path, body, query))
        if method == "POST" and path.endswith("/message_templates"):
            if self.falla_antes:
                raise self.falla_antes.pop(0)
            if self.pausa_s:
                await asyncio.sleep(self.pausa_s)
            item = {"id": self.id_fijo or f"meta-{uuid.uuid4().hex[:10]}", "status": "PENDING", **{k: body[k] for k in ("name", "language", "category")}}
            self.plantillas[body["name"]] = item
            if self.falla_despues:
                raise self.falla_despues.pop(0)
            return {"id": item["id"], "status": "PENDING"}
        if method == "GET" and path.endswith("/message_templates"):
            nombre = (query or {}).get("name")
            return {"data": [p for n, p in self.plantillas.items() if nombre in (None, n)], "paging": {}}
        raise AssertionError(f"llamada no esperada a Meta: {method} {path}")


def corte() -> GraphError:
    return GraphError("Timeout llamando a Graph API", category="TRANSIENT", is_transient=True, retry_after_ms=5000)


def rechazo() -> GraphError:
    return GraphError("Invalid parameter", category="PERMANENT", http_status=400, code=100)


@pytest.fixture
def meta(monkeypatch: pytest.MonkeyPatch) -> MetaFalsa:
    falsa = MetaFalsa()
    monkeypatch.setattr("core.gateway.meta_templates.graph_fetch", falsa)

    async def credenciales(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(access_token="token-de-prueba", app_secret="secreto-de-prueba", graph_api_version="v21.0")

    monkeypatch.setattr("core.gateway.meta_credentials.credentials_for", credenciales)

    async def alerta(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr("core.shared.alerts.send_alert", alerta)
    return falsa


async def publicar(tid: uuid.UUID, version_id: uuid.UUID, connection_id: uuid.UUID, categoria: str = "UTILITY", **kw: Any):
    from core.gateway.opciones_meta import publicar_en_meta

    return await publicar_en_meta(tid, version_id, connection_id, categoria, **kw)


async def filas(tid: uuid.UUID, nombre: str) -> list[Any]:
    return await _sql(
        tid,
        "select id, name, status, category, meta_template_id, sync_error, components, edits_this_month, waba_account_id "
        "from message_templates where name = :n order by created_at",
        n=nombre,
    )


async def ligada(tid: uuid.UUID, version_id: uuid.UUID) -> uuid.UUID | None:
    f = await _sql(tid, "select message_template_id from option_template_versions where id = :v", v=version_id)
    return f[0][0]


async def webhook(tid: uuid.UUID, meta_id: str, nombre: str, evento: str, **extra: Any) -> None:
    from core.gateway.meta_template_webhooks import handle_template_status_update

    await handle_template_status_update(
        {"message_template_id": meta_id, "message_template_name": nombre, "message_template_language": "es", "event": evento, **extra},
        expected_tenant_id=tid,
    )


# ---------------------------------------------------------------- creación
async def test_publicar_crea_la_plantilla_con_el_nombre_la_categoria_y_el_vinculo(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])

    r = await publicar(a.tid, v1, conn, "marketing", ejemplos={"cliente.nombre": "Mariana"})

    assert r.ok and r.outcome == "published" and r.nombre == f"{clave}_v1"
    assert r.categoria == "MARKETING" and r.estado == "PENDING" and r.meta_template_id
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.category == "MARKETING" and fila.status == "PENDING" and fila.meta_template_id == r.meta_template_id
    assert await ligada(a.tid, v1) == fila.id == r.message_template_id
    (_m, _p, cuerpo, _q), = meta.posts
    assert cuerpo["name"] == f"{clave}_v1" and cuerpo["category"] == "MARKETING" and cuerpo["language"] == "es"
    assert cuerpo["parameter_format"] == "NAMED" and cuerpo["allow_category_change"] is False
    assert cuerpo["components"][0]["example"]["body_text_named_params"] == [{"param_name": "cliente_nombre", "example": "Mariana"}]
    assert [c["type"] for c in cuerpo["components"]] == ["BODY", "FOOTER", "BUTTONS"]


async def test_la_plantilla_creada_pasa_la_compatibilidad_del_envio(negocios, meta) -> None:
    from core.kernel.opciones import envio
    from core.kernel.opciones.definicion import leer

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    d = definicion(10)
    clave, (v1,) = await plantilla(a.tid, [d])
    await publicar(a.tid, v1, conn)
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert envio.compatibilidad_plantilla(fila.components, leer(d)) is None


async def test_la_categoria_queda_en_la_fila_ANTES_de_llamar_a_meta(negocios, meta, monkeypatch) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    visto: list[Any] = []

    async def espiar(path: str, **k: Any) -> Any:
        if k.get("method") == "POST":
            visto.append(await filas(a.tid, f"{clave}_v1"))
        return await meta(path, **k)

    meta.falla_antes.append(rechazo())
    monkeypatch.setattr("core.gateway.meta_templates.graph_fetch", espiar)
    r = await publicar(a.tid, v1, conn, "UTILITY")
    assert r.outcome == "meta_rejected"
    (previa,) = visto[0]
    assert previa.status == "DRAFT" and previa.category == "UTILITY" and previa.meta_template_id is None


# ---------------------------------------------------------------- validación previa
@pytest.mark.parametrize("categoria", ["AUTHENTICATION", "otra", ""])
async def test_una_categoria_invalida_se_rechaza_sin_reservar_ni_llamar(negocios, meta, categoria) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r = await publicar(a.tid, v1, conn, categoria)
    assert not r.ok and r.outcome == "invalid_category"
    assert not meta.llamadas and await filas(a.tid, f"{clave}_v1") == [] and await ligada(a.tid, v1) is None


async def test_una_definicion_que_no_cumple_se_rechaza_antes_de_llamar_a_meta_y_sin_reservar(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [{"body": "sin opciones", "opciones": []}])
    r = await publicar(a.tid, v1, conn)
    assert not r.ok and r.outcome == "invalid_definition" and not r.reintentable
    assert not meta.llamadas and await filas(a.tid, f"{clave}_v1") == [] and await ligada(a.tid, v1) is None


async def test_un_idioma_invalido_y_una_version_retirada_se_rechazan(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    assert (await publicar(a.tid, v1, conn, idioma="ES-ar")).outcome == "invalid_language"
    await _sql(a.tid, "update option_template_versions set retired_at = now() where id = :v", v=v1)
    assert (await publicar(a.tid, v1, conn)).outcome == "version_retired"
    assert not meta.llamadas


async def test_sin_conexion_lista_no_se_reserva_nada(negocios, meta) -> None:
    a, _ = negocios
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r = await publicar(a.tid, v1, uuid.uuid4())
    assert r.outcome == "connection_not_ready" and not meta.llamadas
    assert await filas(a.tid, f"{clave}_v1") == [] and await ligada(a.tid, v1) is None


# ---------------------------------------------------------------- nunca se edita una aprobada
async def test_la_version_nueva_es_otra_plantilla_y_la_anterior_sigue_ligada_y_aprobada(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1, v2) = await plantilla(a.tid, [definicion(2), definicion(3, body="Texto nuevo {cliente.nombre}")])

    r1 = await publicar(a.tid, v1, conn)
    await webhook(a.tid, r1.meta_template_id or "", f"{clave}_v1", "APPROVED")
    r2 = await publicar(a.tid, v2, conn)

    assert r2.outcome == "published" and r2.nombre == f"{clave}_v2" and r2.message_template_id != r1.message_template_id
    (f1,) = await filas(a.tid, f"{clave}_v1")
    (f2,) = await filas(a.tid, f"{clave}_v2")
    assert f1.status == "APPROVED" and f1.edits_this_month == 0 and f2.status == "PENDING"
    assert await ligada(a.tid, v1) == f1.id and await ligada(a.tid, v2) == f2.id
    # Dos creaciones y NINGUNA edición (`POST /{id}`): nada de `update_template`.
    assert [p[1] for p in meta.llamadas if p[0] == "POST"] == [p[1] for p in meta.posts]
    assert all(p[1].endswith("/message_templates") for p in meta.posts) and len(meta.posts) == 2


# ---------------------------------------------------------------- rechazo, pausa y límites
async def test_si_meta_rechaza_la_creacion_queda_el_motivo_y_la_version_sigue_publicada(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_antes.append(rechazo())

    r = await publicar(a.tid, v1, conn)

    assert not r.ok and r.outcome == "meta_rejected" and not r.reintentable and "Invalid parameter" in r.motivo
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.status == "DRAFT" and fila.meta_template_id is None and "Invalid parameter" in (fila.sync_error or "")
    st = (await _sql(a.tid, "select status from option_templates where key = :k", k=clave))[0][0]
    assert st == "published"  # publicar en Meta no deshace la publicación


async def test_el_rechazo_y_la_pausa_posteriores_llegan_por_el_webhook_y_el_envio_los_respeta(negocios, meta, monkeypatch) -> None:
    from core.gateway.opciones_meta import estado_de_publicacion

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r = await publicar(a.tid, v1, conn)
    mid = r.meta_template_id or ""

    await webhook(a.tid, mid, f"{clave}_v1", "REJECTED", rejection_info={"reason": "INVALID_FORMAT", "recommendation": "Revisa el pie"})
    est = (await estado_de_publicacion(a.tid, [v1]))[v1]
    assert est.estado == "REJECTED" and est.motivo_rechazo == "INVALID_FORMAT" and est.recomendacion == "Revisa el pie"

    await webhook(a.tid, mid, f"{clave}_v1", "PAUSED")
    assert (await estado_de_publicacion(a.tid, [v1]))[v1].estado == "PAUSED"
    await webhook(a.tid, mid, f"{clave}_v1", "APPROVED")
    assert (await estado_de_publicacion(a.tid, [v1]))[v1].estado == "APPROVED"


async def test_el_limite_propio_de_creacion_no_llama_a_meta_y_es_reintentable(negocios, meta, monkeypatch) -> None:
    import core.gateway.meta_templates as mt

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    real = mt._check_and_bump_create_rate_limit
    topes = [mt.TemplateRateLimitError("Límite de 100 plantillas/hora")]

    async def tope(waba: str) -> None:
        if topes:
            raise topes.pop()
        await real(waba)

    monkeypatch.setattr(mt, "_check_and_bump_create_rate_limit", tope)
    r = await publicar(a.tid, v1, conn)
    assert not r.ok and r.outcome == "rate_limited" and r.reintentable and not meta.posts

    assert (await publicar(a.tid, v1, conn)).outcome == "published"
    assert len(await filas(a.tid, f"{clave}_v1")) == 1


async def test_con_el_cupo_de_plantillas_de_la_cuenta_lleno_no_se_llama_a_meta(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid, cupo=(250, 250))
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r = await publicar(a.tid, v1, conn)
    assert r.outcome == "template_quota_full" and not r.ok and not meta.llamadas
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.status == "DRAFT" and fila.meta_template_id is None  # reservada: al liberar cupo, el reintento la completa


async def test_la_cuenta_llena_no_bloquea_el_reintento_de_una_version_que_ya_esta_en_meta(negocios, meta) -> None:
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    conn, waba_row, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    assert (await publicar(a.tid, v1, conn)).outcome == "published"
    await _sql(a.tid, "update waba_accounts set message_template_count = 250, message_template_limit = 250 where id = :w", w=waba_row)
    async with tenant_scope(a.tid):
        r = await publicar(a.tid, v1, conn)
    assert r.ok and r.outcome == "already_published" and len(meta.posts) == 1


async def test_un_5xx_de_meta_es_ambiguo_y_el_reintento_concilia(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_despues.append(GraphError("Bad Gateway", category="PERMANENT", http_status=502))
    r1 = await publicar(a.tid, v1, conn)
    assert r1.outcome == "network_error" and r1.reintentable and len(meta.plantillas) == 1
    r2 = await publicar(a.tid, v1, conn)
    assert r2.outcome == "already_published" and len(meta.posts) == 1


async def test_si_meta_responde_sin_id_no_se_da_por_publicada(negocios, meta, monkeypatch) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])

    async def sin_id(path: str, **k: Any) -> Any:
        if k.get("method") == "POST":
            return {"status": "PENDING"}
        return {"data": []}

    monkeypatch.setattr("core.gateway.meta_templates.graph_fetch", sin_id)
    r = await publicar(a.tid, v1, conn)
    assert not r.ok and r.outcome == "network_error" and r.motivo == "respuesta_sin_id"
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.status == "DRAFT" and fila.meta_template_id is None


async def test_un_error_de_conciliacion_se_clasifica_igual_que_uno_de_creacion(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_antes.append(corte())
    assert (await publicar(a.tid, v1, conn)).outcome == "network_error"  # deja la fila DRAFT

    async def get_con_token_vencido(path: str, **k: Any) -> Any:
        raise GraphError("Token inválido", category="AUTH", http_status=401, code=190)

    import core.gateway.meta_templates as mt

    original = mt.graph_fetch
    mt.graph_fetch = get_con_token_vencido  # type: ignore[assignment]
    try:
        r = await publicar(a.tid, v1, conn)
    finally:
        mt.graph_fetch = original  # type: ignore[assignment]
    assert r.outcome == "connection_not_ready" and not r.reintentable


async def test_el_limite_de_uso_de_meta_es_reintentable_y_una_credencial_vencida_pide_reconectar(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1, v2) = await plantilla(a.tid, [definicion(), definicion(2)])
    meta.falla_antes.append(GraphError("Rate limit", category="RATE_LIMIT", http_status=429, code=4))
    r = await publicar(a.tid, v1, conn)
    assert r.outcome == "rate_limited" and r.reintentable
    meta.falla_antes.append(GraphError("Token inválido", category="AUTH", http_status=401, code=190))
    r = await publicar(a.tid, v2, conn)
    assert r.outcome == "connection_not_ready" and not r.reintentable


# ---------------------------------------------------------------- reintento seguro
async def test_un_corte_ANTES_de_que_meta_cree_se_reintenta_sin_duplicar_la_fila(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_antes.append(corte())

    r1 = await publicar(a.tid, v1, conn)
    assert r1.outcome == "network_error" and r1.reintentable and r1.estado == "DRAFT"
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.meta_template_id is None and await ligada(a.tid, v1) == fila.id

    r2 = await publicar(a.tid, v1, conn)
    assert r2.outcome == "published" and r2.message_template_id == fila.id
    assert len(await filas(a.tid, f"{clave}_v1")) == 1 and len(meta.plantillas) == 1


async def test_un_corte_DESPUES_de_que_meta_cree_el_reintento_concilia_y_no_vuelve_a_crear(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_despues.append(corte())  # Meta la creó pero la respuesta se perdió

    r1 = await publicar(a.tid, v1, conn)
    assert r1.outcome == "network_error" and len(meta.plantillas) == 1 and len(meta.posts) == 1

    r2 = await publicar(a.tid, v1, conn)
    assert r2.ok and r2.outcome == "already_published" and r2.meta_template_id == meta.plantillas[f"{clave}_v1"]["id"]
    assert len(meta.posts) == 1  # NO se creó otra: tampoco se gastó otro cupo del límite de creación
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.meta_template_id == r2.meta_template_id and fila.status == "PENDING"


async def test_repetir_una_publicacion_ya_hecha_no_llama_a_meta(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r1 = await publicar(a.tid, v1, conn)
    await webhook(a.tid, r1.meta_template_id or "", f"{clave}_v1", "APPROVED")
    r2 = await publicar(a.tid, v1, conn)
    assert r2.outcome == "already_published" and r2.estado == "APPROVED"  # el estado del webhook no se pisa
    assert len(meta.llamadas) == 1


async def test_el_completado_no_pisa_el_estado_que_ya_puso_el_webhook(negocios, meta) -> None:
    from core.gateway.opciones_meta import _completar

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_despues.append(corte())
    await publicar(a.tid, v1, conn)
    (fila,) = await filas(a.tid, f"{clave}_v1")
    from core.shared.tenant import tenant_scope

    async with tenant_scope(a.tid):
        await _completar(a.tid, fila.id, "meta-1", "PENDING", None)
        await webhook(a.tid, "meta-1", f"{clave}_v1", "APPROVED")
        otra = await _completar(a.tid, fila.id, "meta-2", "PENDING", "MARKETING")  # un segundo guardado tardío
    assert otra.meta_template_id == "meta-1" and otra.status == "APPROVED"


async def test_dos_publicaciones_simultaneas_de_la_misma_version_hacen_un_solo_post(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    meta.pausa_s = 0.3

    r1, r2 = await asyncio.gather(publicar(a.tid, v1, conn), publicar(a.tid, v1, conn))

    assert sorted([r1.outcome, r2.outcome]) in (["in_progress", "published"], ["already_published", "published"])
    assert len(meta.posts) == 1 and len(await filas(a.tid, f"{clave}_v1")) == 1


async def test_los_ejemplos_del_dueno_se_acotan_y_no_llevan_saltos_de_linea(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    await publicar(a.tid, v1, conn, ejemplos={"cliente.nombre": "Ana\n" + "x" * 500, "otra.ruta": "no se usa"})
    ejemplo = meta.posts[0][2]["components"][0]["example"]["body_text_named_params"][0]["example"]
    assert "\n" not in ejemplo and 1 < len(ejemplo) <= 60


async def test_un_id_de_meta_que_ya_esta_en_otra_fila_del_negocio_no_se_reintenta_solo(negocios, meta) -> None:
    a, _ = negocios
    conn, waba_row, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    await _sql(
        a.tid,
        "insert into message_templates (id, tenant_id, waba_account_id, meta_template_id, name, language, category, status) "
        "values (gen_random_uuid(), :t, :w, 'meta-repetido', 'otra_plantilla', 'es', 'UTILITY', 'APPROVED')",
        t=a.tid, w=waba_row,
    )
    meta.id_fijo = "meta-repetido"
    r = await publicar(a.tid, v1, conn)
    assert r.outcome == "meta_id_taken" and not r.ok and not r.reintentable
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.meta_template_id is None and fila.sync_error == "meta_id_taken"


# ---------------------------------------------------------------- conflictos de nombre y de cuenta
async def test_una_plantilla_ajena_con_el_mismo_nombre_y_otro_cuerpo_no_se_adopta(negocios, meta) -> None:
    """La clave se borró y se recreó: `<clave>_v1` ya existe en la WABA con OTRO texto. No se liga ni se pisa."""
    from core.kernel.opciones import mapeo
    from core.kernel.opciones.definicion import leer

    a, _ = negocios
    conn, waba_row, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    vieja = mapeo.componentes_meta(leer(definicion(body="Texto de la clave anterior {cliente.nombre}")))
    await _sql(
        a.tid,
        "insert into message_templates (id, tenant_id, waba_account_id, meta_template_id, name, language, category, status, components) "
        "values (gen_random_uuid(), :t, :w, 'meta-vieja', :n, 'es', 'UTILITY', 'APPROVED', cast(:c as jsonb))",
        t=a.tid, w=waba_row, n=f"{clave}_v1", c=json.dumps(vieja),
    )
    r = await publicar(a.tid, v1, conn)
    assert r.outcome == "name_taken" and not r.reintentable and not meta.llamadas
    assert await ligada(a.tid, v1) is None
    (fila,) = await filas(a.tid, f"{clave}_v1")
    assert fila.meta_template_id == "meta-vieja" and fila.status == "APPROVED"


async def test_una_plantilla_igual_pero_de_otra_categoria_no_se_liga_en_silencio(negocios, meta) -> None:
    from core.kernel.opciones import mapeo
    from core.kernel.opciones.definicion import leer

    a, _ = negocios
    conn, waba_row, _ = await conexion(a.tid)
    d = definicion()
    clave, (v1,) = await plantilla(a.tid, [d])
    await _sql(
        a.tid,
        "insert into message_templates (id, tenant_id, waba_account_id, meta_template_id, name, language, category, status, components) "
        "values (gen_random_uuid(), :t, :w, 'meta-mkt', :n, 'es', 'MARKETING', 'APPROVED', cast(:c as jsonb))",
        t=a.tid, w=waba_row, n=f"{clave}_v1", c=json.dumps(mapeo.componentes_meta(leer(d))),
    )
    r = await publicar(a.tid, v1, conn, "UTILITY")
    assert r.outcome == "name_taken" and r.motivo == "categoria_distinta" and await ligada(a.tid, v1) is None
    assert (await publicar(a.tid, v1, conn, "MARKETING")).outcome == "already_published"


async def test_una_plantilla_igual_que_ya_trajo_el_sync_se_adopta_sin_llamar_a_meta(negocios, meta) -> None:
    from core.kernel.opciones import mapeo
    from core.kernel.opciones.definicion import leer

    a, _ = negocios
    conn, waba_row, _ = await conexion(a.tid)
    d = definicion()
    clave, (v1,) = await plantilla(a.tid, [d])
    await _sql(
        a.tid,
        "insert into message_templates (id, tenant_id, waba_account_id, meta_template_id, name, language, category, status, components) "
        "values (gen_random_uuid(), :t, :w, 'meta-sync', :n, 'es', 'UTILITY', 'APPROVED', cast(:c as jsonb))",
        t=a.tid, w=waba_row, n=f"{clave}_v1", c=json.dumps(mapeo.componentes_meta(leer(d))),
    )
    r = await publicar(a.tid, v1, conn)
    assert r.outcome == "already_published" and r.meta_template_id == "meta-sync" and r.estado == "APPROVED"
    assert not meta.llamadas and await ligada(a.tid, v1) == r.message_template_id


async def test_una_version_ligada_a_la_WABA_de_otra_conexion_no_se_religa(negocios, meta) -> None:
    a, _ = negocios
    conn1, _, _ = await conexion(a.tid)
    conn2, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    r1 = await publicar(a.tid, v1, conn1)
    r2 = await publicar(a.tid, v1, conn2)
    assert r2.outcome == "other_account" and not r2.ok
    assert await ligada(a.tid, v1) == r1.message_template_id and len(meta.posts) == 1


async def test_si_la_plantilla_ligada_se_borro_en_meta_hace_falta_una_version_nueva(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion()])
    r1 = await publicar(a.tid, v1, conn)
    await webhook(a.tid, r1.meta_template_id or "", f"{clave}_v1", "DELETED")
    r2 = await publicar(a.tid, v1, conn)
    assert r2.outcome == "template_deleted" and not r2.reintentable and len(meta.posts) == 1


# ---------------------------------------------------------------- aislamiento entre negocios
async def test_un_negocio_no_publica_ni_lee_la_version_de_otro(negocios, meta) -> None:
    from core.gateway.opciones_meta import estado_de_publicacion

    a, b = negocios
    conn_a, _, _ = await conexion(a.tid)
    conn_b, _, _ = await conexion(b.tid)
    clave, (va,) = await plantilla(a.tid, [definicion()])
    await publicar(a.tid, va, conn_a)

    assert (await publicar(b.tid, va, conn_b)).outcome == "version_not_found"
    assert (await publicar(a.tid, va, conn_b)).outcome == "connection_not_ready"  # la conexión de B no es de A
    assert await estado_de_publicacion(b.tid, [va]) == {}
    assert len(meta.posts) == 1 and await filas(b.tid, f"{clave}_v1") == []


async def test_el_estado_de_varias_versiones_se_lee_en_una_consulta(negocios, meta) -> None:
    from core.gateway.opciones_meta import estado_de_publicacion

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1, v2, v3) = await plantilla(a.tid, [definicion(1), definicion(2), definicion(3)])
    await publicar(a.tid, v1, conn)
    await publicar(a.tid, v2, conn, "MARKETING")
    estados = await estado_de_publicacion(a.tid, [v1, v2, v3])
    assert set(estados) == {v1, v2} and estados[v2].categoria == "MARKETING" and estados[v1].nombre == f"{clave}_v1"
    assert await estado_de_publicacion(a.tid, []) == {}


# ---------------------------------------------------------------- auditoría
async def test_la_auditoria_lleva_ids_y_resultado_sin_el_texto_de_meta(negocios, meta) -> None:
    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    _, (v1,) = await plantilla(a.tid, [definicion()])
    meta.falla_antes.append(GraphError("Texto largo de Meta con detalle", category="PERMANENT", http_status=400, code=100))
    await publicar(a.tid, v1, conn, actor="dueño-1")
    (fila,) = await _sql(
        a.tid, "select actor, input, output from audit_log where action = 'option_meta_publish' order by created_at desc limit 1"
    )
    assert fila.actor == "dueño-1"
    assert fila.input["version_id"] == str(v1) and fila.output["outcome"] == "meta_rejected"
    assert "Texto largo" not in json.dumps(fila.input) + json.dumps(fila.output)


# ---------------------------------------------------------------- JOURNEY: publicar → aprobada → envío por plantilla (PR 5)
@pytest.fixture
def cola(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    encolados: list[str] = []
    monkeypatch.setattr("core.tasks.outbound_send.outbound_send_task.delay", lambda mid, tid: encolados.append(str(mid)))

    async def credenciales_envio(*_a: Any, **_k: Any) -> tuple[str, dict[str, str]]:
        return "meta", {"phone_number_id": "ph-test", "access_token": "token-de-prueba"}

    monkeypatch.setattr("core.gateway.sender.resolve_credentials", credenciales_envio)
    return encolados


async def test_journey_la_plantilla_publicada_y_aprobada_la_usa_el_envio_fuera_de_la_ventana(negocios, meta, cola) -> None:
    from core.db.models import Conversation, Customer
    from core.gateway.sender import send_options_to_conversation
    from core.shared.tenant import tenant_scope

    a, _ = negocios
    conn, _, _ = await conexion(a.tid)
    clave, (v1,) = await plantilla(a.tid, [definicion(3)])

    async def conversacion_cerrada(s: Any) -> uuid.UUID:
        c = Customer(tenant_id=a.tid, channel="whatsapp", external_id=f"mp:{uuid.uuid4().hex[:8]}", name="Ana")
        s.add(c)
        await s.flush()
        conv = Conversation(tenant_id=a.tid, customer_id=c.id, channel="whatsapp", meta_connection_id=conn, service_window_expires_at=None)
        s.add(conv)
        await s.flush()
        return conv.id

    conv = await _scoped(a.tid, conversacion_cerrada)

    async def enviar() -> Any:
        async with tenant_scope(a.tid):
            return await send_options_to_conversation(conv, clave)

    # Sin publicar en Meta: nada sale.
    sin = await enviar()
    assert not sin.enviado and sin.outcome == "sin_plantilla" and sin.motivo == "version_sin_plantilla_de_meta"

    r = await publicar(a.tid, v1, conn)
    pendiente = await enviar()
    assert not pendiente.enviado and pendiente.motivo == "plantilla_no_aprobada"

    await webhook(a.tid, r.meta_template_id or "", f"{clave}_v1", "APPROVED")
    ok = await enviar()
    assert ok.enviado and ok.via == "template" and ok.ejecucion_directa and len(cola) == 1

    await webhook(a.tid, r.meta_template_id or "", f"{clave}_v1", "PAUSED")
    pausada = await enviar()
    assert not pausada.enviado and pausada.motivo == "plantilla_no_aprobada" and len(cola) == 1
