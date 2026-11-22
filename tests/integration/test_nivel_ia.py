"""HU-BILL-24 · nivel de IA por agente (ADR-0210), contra el stack real, el loop real y un modelo guionado (sin LLM).

Gherkin:
- **Elegir el nivel.** Un negocio Premium elige `avanzado`: la versión publicada fija `deepseek-v4-pro` en `primary` y
  `router`, el turno corre con ese modelo y la respuesta de la API no nombra el modelo.
- **Plan que no lo incluye.** Un negocio Basic pide `avanzado`: 409 `plan_nivel_ia_not_included` y sigue en Estándar.
- **Cobro real.** El turno Avanzado escribe en el ledger el costo real del modelo y sus créditos del tarifario vigente.
- **Bajar de plan.** Premium → Basic: el siguiente turno corre en Estándar sin error; al volver a Premium, Avanzado.

Más: con llave propia `publicar` conserva el modelo del negocio; el hash cambia con el nivel y volver al anterior reusa
la versión; el gestor no ve modelos (agent-models, cerebro) y el master sí; aislamiento entre negocios.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _api_helpers import api_headers, uniq
from sqlalchemy import text

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

RAIZ = Path(__file__).resolve().parents[2]
if "journeys" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location("journeys", RAIZ / "evals" / "journeys.py")
    assert _SPEC and _SPEC.loader
    _modulo = importlib.util.module_from_spec(_SPEC)
    sys.modules["journeys"] = _modulo
    _SPEC.loader.exec_module(_modulo)
journeys = sys.modules["journeys"]

PASSWORD = "clave-segura-123"
CATALOGO = {
    "pack": "pack_commerce",
    "category_paths": [["Planes"]],
    "products": [
        {"name": "Plan Premium", "description": "Todo incluido.", "category_path": ["Planes"],
         "variants": [{"sku": "PREMIUM-MES", "price": 600000, "stock": 0,
                       "attributes": {"periodicidad": "mensual", "trial_dias": 0}}]},
    ],
}
MENSAJE = "Hola, ¿qué planes tienen?"
# Tokens grandes para que el costo del modelo se note en los créditos.
TOKENS_IN, TOKENS_OUT = 100_000, 10_000


# ---------------------------------------------------------------- helpers
def _modules() -> list[Any]:
    from core.plugin_manager import plugin_manager

    return plugin_manager.scan()


async def _sql(tid: uuid.UUID | None, sql: str, **params: Any) -> list[Any]:
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            resultado = await session.execute(text(sql), params)
            return list(resultado.all()) if getattr(resultado, "returns_rows", False) else []


async def _negocio(slug: str, pagos: Any, plan: str) -> tuple[uuid.UUID, uuid.UUID]:
    tid = await journeys.preparar_negocio(
        {
            "nombre": f"Nivel IA {plan}",
            "packs": ["pack_commerce", "pack_payments"],
            "plan": plan,
            "config": {
                "pack_payments": {"public_key": f"pub_test_{slug}", "private_key": f"prv_test_{slug}",
                                  "events_secret": f"test_events_{slug}", "sandbox": True},
            },
            "catalogo": CATALOGO,
        },
        pagos,
        slug=slug,
    )
    cid = uuid.uuid4()
    await _sql(
        tid,
        "insert into channel_connections (id, tenant_id, channel, provider, external_id, type_id) "
        "values (:id, :t, 'instagram', 'meta', :ext, 'instagram_meta')",
        id=cid, t=tid, ext=f"ig-{cid.hex[:8]}",
    )
    journeys._CONEXION_V3[tid] = cid
    from release_de_prueba import release_inicial

    reporte = await release_inicial(tid, _modules())
    assert not reporte.fallo, reporte.errores
    return tid, cid


@pytest.fixture(scope="module")
async def negocios() -> AsyncIterator[dict[str, Any]]:
    """Un negocio Premium, uno Basic y uno Premium con llave propia, cada uno con su release 1."""
    import pack_payments.payments as capacidad

    from core.releases.packs import sync_packs
    from core.tenants import deactivate_tenant, purge_tenant

    original = capacidad.build_provider
    await journeys._preparar_proceso()
    assert not (await sync_packs(_modules())).fallo
    pagos = journeys.PagoSimulado(monto_minimo=150000)
    slugs = {k: f"bill24-{k}-{uuid.uuid4().hex[:6]}" for k in ("premium", "basic", "byok")}
    premium, cid = await _negocio(slugs["premium"], pagos, "premium")
    basic, _ = await _negocio(slugs["basic"], pagos, "basic")
    byok, _ = await _negocio(slugs["byok"], pagos, "premium")
    yield {"premium": premium, "cid": cid, "basic": basic, "byok": byok, "pagos": pagos}
    capacidad.build_provider = original
    for clave, tid in (("premium", premium), ("basic", basic), ("byok", byok)):
        await deactivate_tenant(str(tid))
        await purge_tenant(str(tid), f"ELIMINAR {slugs[clave]}")


async def _dueno(app_client, tid: uuid.UUID) -> dict[str, str]:
    """Dueño del negocio: gestor (no master)."""
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno-b24')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return await api_headers(app_client, tid, email=email, password=PASSWORD)


async def _atencion(tid: uuid.UUID) -> uuid.UUID:
    [(aid,)] = await _sql(tid, "select id from agents where tenant_id = :t and key = 'atencion' and deleted_at is null", t=tid)
    return aid


async def _republicar(tid: uuid.UUID, contenido: dict[str, Any] | None = None) -> tuple[Any, bool]:
    """Publica el especialista con el contenido de su última versión (o `contenido`)."""
    from core.agents import versiones
    from core.kernel.procedures import catalogo_de_acciones
    from core.shared.tenant import tenant_scope

    aid = await _atencion(tid)
    if contenido is None:
        [(contenido,)] = await _sql(
            tid, "select content from agent_versions where tenant_id = :t and agent_id = :a order by version desc limit 1",
            t=tid, a=aid,
        )
    async with tenant_scope(tid):
        return await versiones.publicar(
            tid, aid, catalogo=catalogo_de_acciones(_modules()), contenido=contenido, label="nivel", notes="nivel"
        )


async def _construir_y_promover(tid: uuid.UUID, cid: uuid.UUID) -> None:
    from core.releases.build import construir
    from core.releases.promocion import promover
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        resultado = await construir(tid, cid, actor="test", modules=_modules())
        await promover(tid, cid, resultado.release_id, actor="test")


class _Modelo:
    """Modelo guionado que responde con texto. Cobra con el precio real del modelo que pidió el loop."""

    provider_name = "deepseek"

    def __init__(self) -> None:
        self.modelos: list[str] = []
        self.model = ""

    @property
    def pricing_per_mtok(self) -> tuple[float, float]:
        from core.llm.openai_compatible import PRICING

        return PRICING[self.model]

    def estimate_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        entrada, salida = self.pricing_per_mtok
        return (input_tokens * entrada + output_tokens * salida) / 1_000_000

    async def generate(self, **_kwargs: Any) -> Any:
        uso = types.SimpleNamespace(input_tokens=TOKENS_IN, output_tokens=TOKENS_OUT)
        return types.SimpleNamespace(content="Tenemos el plan Premium mensual.", stop_reason="end_turn",
                                     model=self.model, latency_ms=1, usage=uso, tool_calls=[])


async def _turno(tid: uuid.UUID, pagos: Any, monkeypatch: Any) -> tuple[Any, _Modelo, Any]:
    modelo = _Modelo()

    def _proveedor(*_a: Any, **kwargs: Any) -> _Modelo:
        modelo.model = str(kwargs.get("model"))
        modelo.modelos.append(modelo.model)
        return modelo

    monkeypatch.setattr("core.orchestrator.loop.resolve_provider", _proveedor)
    customer_id, conversation_id = await journeys.nuevo_cliente(tid, "Rob")
    ctx = journeys.Contexto(tid, customer_id, conversation_id, pagos)
    resultado = await journeys._turno(ctx, MENSAJE)
    return resultado, modelo, ctx


async def _ledger(tid: uuid.UUID, ctx: Any) -> tuple[Any, ...]:
    [fila] = await _sql(
        tid,
        "select model, cost_usd, credits, key_mode, trm_cop from usage_ledger where tenant_id = :t and conversation_id = :c "
        "order by created_at desc limit 1",
        t=tid, c=ctx.conversation_id,
    )
    return tuple(fila)


async def _poner_plan(tid: uuid.UUID, plan_id: str) -> None:
    await _sql(tid, "update tenant_subscriptions set plan_id = :p where tenant_id = :t and deleted_at is null", p=plan_id, t=tid)


# ---------------------------------------------------------------- Gherkin
async def test_premium_en_avanzado_corre_con_v4_pro_cobra_el_costo_real_y_baja_con_el_plan(
    negocios, app_client, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from core.billing.tarifario import creditos_por_evento

    tid, cid, pagos = negocios["premium"], negocios["cid"], negocios["pagos"]
    aid = await _atencion(tid)
    h = await _dueno(app_client, tid)

    # Estándar de partida: el turno corre con flash y cobra su costo.
    _, modelo, ctx = await _turno(tid, pagos, monkeypatch)
    assert modelo.modelos[0] == "deepseek-flash"
    _, costo_estandar, creditos_estandar, _, _ = await _ledger(tid, ctx)

    # Elegir el nivel: 200, la respuesta trae el nivel y no nombra el modelo.
    r = await app_client.patch(f"/api/v1/admin/agents/{aid}", headers=h, json={"nivel_ia": "avanzado"})
    assert r.status_code == 200, r.text
    assert r.json()["nivel_ia"] == "avanzado"
    assert "deepseek" not in r.text.lower()
    r = await app_client.get("/api/v1/admin/agents", headers=h)
    assert {a["id"]: a["nivel_ia"] for a in r.json()}[str(aid)] == "avanzado"

    # Se aplica al publicar: primary y router de la versión.
    version, creada = await _republicar(tid)
    assert creada
    assert version.content["model"]["primary"] == "deepseek-v4-pro"
    assert version.content["model"]["router"] == "deepseek-v4-pro"
    await _construir_y_promover(tid, cid)

    resultado, modelo, ctx = await _turno(tid, pagos, monkeypatch)
    assert not resultado.escalated
    assert modelo.modelos[0] == "deepseek-v4-pro"

    # Cobro real: el costo del modelo con su precio y los créditos del tarifario vigente, sin fee del nivel.
    modelo_ledger, costo, creditos, key_mode, trm = await _ledger(tid, ctx)
    from core.llm.openai_compatible import PRICING

    entrada, salida = PRICING["deepseek-v4-pro"]
    esperado = round((TOKENS_IN * entrada + TOKENS_OUT * salida) / 1_000_000, 6)
    assert modelo_ledger == "deepseek-v4-pro" and key_mode == "platform"
    assert float(costo) == pytest.approx(esperado)
    assert creditos == creditos_por_evento(Decimal(str(esperado)), "llm_turn", "platform", trm)
    assert costo > costo_estandar and creditos > creditos_estandar

    # Bajar de plan: el siguiente turno corre en Estándar sin republicar y sin error para el cliente.
    await _poner_plan(tid, "basic")
    try:
        resultado, modelo, ctx = await _turno(tid, pagos, monkeypatch)
        assert not resultado.escalated and resultado.text
        assert modelo.modelos == ["deepseek-flash"]
        assert (await _ledger(tid, ctx))[0] == "deepseek-flash"
    finally:
        await _poner_plan(tid, "premium")
    # Al volver a subir, se restaura.
    _, modelo, _ = await _turno(tid, pagos, monkeypatch)
    assert modelo.modelos[0] == "deepseek-v4-pro"


async def test_basic_recibe_409_y_sigue_en_estandar(negocios, app_client) -> None:  # type: ignore[no-untyped-def]
    tid = negocios["basic"]
    aid = await _atencion(tid)
    h = await _dueno(app_client, tid)
    r = await app_client.patch(f"/api/v1/admin/agents/{aid}", headers=h, json={"nivel_ia": "avanzado", "name": "Otro"})
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_nivel_ia_not_included"
    assert error["details"] == {"nivel_ia": "avanzado", "plan": "basic"}
    # No escribió nada: ni el nivel ni el nombre que venía en el mismo PATCH.
    [(nivel, nombre)] = await _sql(tid, "select nivel_ia, name from agents where id = :a", a=aid)
    assert nivel == "estandar" and nombre != "Otro"
    # Estándar siempre se puede elegir.
    r = await app_client.patch(f"/api/v1/admin/agents/{aid}", headers=h, json={"nivel_ia": "estandar"})
    assert r.status_code == 200 and r.json()["nivel_ia"] == "estandar", r.text


async def test_el_coordinador_no_tiene_nivel(negocios, app_client) -> None:  # type: ignore[no-untyped-def]
    tid = negocios["premium"]
    [(coordinador,)] = await _sql(tid, "select id from agents where tenant_id = :t and kind = 'core' and deleted_at is null", t=tid)
    r = await app_client.patch(
        f"/api/v1/admin/agents/{coordinador}", headers=await _dueno(app_client, tid), json={"nivel_ia": "avanzado"}
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "nivel_ia_not_applicable", r.text


# ---------------------------------------------------------------- hash y BYOK
async def test_el_hash_cambia_con_el_nivel_y_volver_reusa_la_version(negocios) -> None:  # type: ignore[no-untyped-def]
    tid = negocios["premium"]
    aid = await _atencion(tid)
    await _sql(tid, "update agents set nivel_ia = 'estandar' where id = :a", a=aid)
    estandar, _ = await _republicar(tid)
    await _sql(tid, "update agents set nivel_ia = 'avanzado' where id = :a", a=aid)
    avanzado, creada = await _republicar(tid, estandar.content)
    assert creada and avanzado.content_hash != estandar.content_hash
    await _sql(tid, "update agents set nivel_ia = 'estandar' where id = :a", a=aid)
    otra_vez, creada = await _republicar(tid, avanzado.content)
    assert not creada and otra_vez.id == estandar.id
    # El master que fija otro modelo a mano: el nivel manda al publicar.
    a_mano = {**estandar.content, "model": {**estandar.content["model"], "primary": "deepseek-v4-pro"}}
    version, _ = await _republicar(tid, a_mano)
    assert version.id == estandar.id and version.content["model"]["primary"] == "deepseek-flash"


async def test_con_llave_propia_publicar_conserva_el_modelo_del_negocio(negocios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from core.shared.crypto import get_secret_box

    tid, pagos = negocios["byok"], negocios["pagos"]
    aid = await _atencion(tid)
    await _sql(
        tid,
        "update app_settings set llm_provider = 'openai_compatible', llm_api_key_encrypted = :k where tenant_id = :t",
        k=get_secret_box().encrypt("sk-test-no-real"), t=tid,
    )
    await _sql(tid, "update agents set nivel_ia = 'avanzado' where id = :a", a=aid)
    [(contenido,)] = await _sql(
        tid, "select content from agent_versions where tenant_id = :t and agent_id = :a order by version desc limit 1",
        t=tid, a=aid,
    )
    propio = {**contenido, "model": {**contenido["model"], "primary": "modelo-propio", "router": None}}
    version, creada = await _republicar(tid, propio)
    assert creada
    assert version.content["model"]["primary"] == "modelo-propio"
    assert version.content["model"].get("router") is None
    [(cid,)] = await _sql(tid, "select id from channel_connections where tenant_id = :t and type_id = 'instagram_meta'", t=tid)
    monkeypatch.setitem(journeys._CONEXION_V3, tid, cid)
    await _construir_y_promover(tid, cid)
    # El turno con llave propia no tiene techo: corre el modelo del negocio y solo se cobra el fee.
    monkeypatch.setattr("core.llm.openai_compatible.PRICING", {"modelo-propio": (0.0, 0.0)})
    _, modelo, ctx = await _turno(tid, pagos, monkeypatch)
    assert modelo.modelos[0] == "modelo-propio"
    assert (await _ledger(tid, ctx))[3] == "byok"


# ---------------------------------------------------------------- el gestor no ve el modelo
async def test_el_gestor_no_ve_modelos_y_el_master_si(negocios, app_client) -> None:  # type: ignore[no-untyped-def]
    tid = negocios["premium"]
    aid = await _atencion(tid)
    gestor, master = await _dueno(app_client, tid), await api_headers(app_client, tid)

    r = await app_client.get(f"/api/v1/admin/agent-models?agent_id={aid}", headers=gestor)
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["items"] == [] and cuerpo["total"] == 0
    assert cuerpo["business_model"] == "" and cuerpo["provider"] == ""
    assert cuerpo["byok_model"] is None and cuerpo["effective_model"] is None and cuerpo["min_phrases"] >= 1
    r = await app_client.get(f"/api/v1/admin/agent-models?agent_id={aid}", headers=master)
    assert r.json()["items"] and r.json()["business_model"] and r.json()["effective_model"]

    r = await app_client.get("/api/v1/admin/settings", headers=gestor)
    assert r.status_code == 200, r.text
    cerebro = r.json()["brain"]
    assert (cerebro["model_primary"], cerebro["model_fallback"], cerebro["model_router"]) == (None, None, None)
    assert (await app_client.get("/api/v1/admin/settings", headers=master)).json()["brain"]["model_primary"]

    # El gestor no cambia los modelos por defecto: se ignoran, el resto se guarda.
    antes = (await app_client.get("/api/v1/admin/settings", headers=master)).json()["brain"]
    r = await app_client.put(
        "/api/v1/admin/settings/brain", headers=gestor,
        json={"model_primary": "deepseek-v4-pro", "model_router": "deepseek-v4-pro", "model_temperature": 0.4},
    )
    assert r.status_code == 200, r.text
    assert r.json()["model_primary"] is None and r.json()["model_temperature"] == pytest.approx(0.4)
    despues = (await app_client.get("/api/v1/admin/settings", headers=master)).json()["brain"]
    assert (despues["model_primary"], despues["model_router"]) == (antes["model_primary"], antes["model_router"])

    # El catálogo de opciones tampoco le ofrece los modelos.
    claves = {o["clave"] for o in (await app_client.get("/api/v1/admin/settings/catalog", headers=gestor)).json()["opciones"]}
    assert not claves & {"model_primary", "model_router", "model_fallback"}


# ---------------------------------------------------------------- aislamiento
async def test_otro_negocio_no_cambia_el_nivel_de_un_agente_ajeno(negocios, app_client) -> None:  # type: ignore[no-untyped-def]
    premium, basic = negocios["premium"], negocios["basic"]
    aid = await _atencion(premium)
    [(antes,)] = await _sql(premium, "select nivel_ia from agents where id = :a", a=aid)
    r = await app_client.patch(
        f"/api/v1/admin/agents/{aid}", headers=await _dueno(app_client, basic), json={"nivel_ia": "estandar"}
    )
    assert r.status_code == 404, r.text
    [(despues,)] = await _sql(premium, "select nivel_ia from agents where id = :a", a=aid)
    assert despues == antes
    # El techo de un negocio no depende del plan de otro.
    from core.llm.niveles import techo_del_negocio
    from core.shared.tenant import tenant_scope

    async with tenant_scope(premium):
        assert await techo_del_negocio(premium) == "avanzado"
    async with tenant_scope(basic):
        assert await techo_del_negocio(basic) == "estandar"
