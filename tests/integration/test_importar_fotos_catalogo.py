"""ADR-0194 §2 y §3 · Importar el catálogo desde fotos: una foto = una fila, cotizada y cobrada.

- **Cotizar** dice cuánto cuestan N fotos y si el crédito alcanza; no escribe nada.
- **Una foto:** se guarda (perfil `producto`), se reconoce con visión (doble, sin red) y devuelve la
  fila propuesta con la foto; el consumo queda en el ledger (`vision`, créditos reales, `image_key`).
- **Idempotente:** la misma foto otra vez se responde desde el ledger, sin llamar ni cobrar.
- **Bloqueo:** sin crédito, 409 `plan_limit_creditos` y no se llama al proveedor.
- **Confirmar** la fila con la precarga crea el producto con su foto; una clave de foto ajena es error.
- **Producción y aislamiento:** funciona sin `QA_TESTERS_ENABLED`; un agente de otro negocio es 404.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any

import pytest
from _api_helpers import api_headers
from _ayudas_sql import _sql
from test_qa_datos_generados import DESCRIPCION, _agente

pytestmark = [
    pytest.mark.aislamiento,  # crédito y fotos por negocio (ADR-0194)
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

_BASE = "/api/v1/admin/agents"
# PNG 1x1 válido (firma correcta); el segundo cambia un byte del final para otra huella.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
OTRA = PNG[:-1] + b"\x83"


class Vision:
    def __init__(self, texto: str | None) -> None:
        self.texto = texto
        self.llamadas = 0

    async def __call__(self, contenido: bytes, mime_type: str, **kwargs: Any) -> Any:
        from core.media.vision import VisionResult

        self.llamadas += 1
        return VisionResult(
            text=self.texto, input_tokens=1500, output_tokens=200, cost_usd=0.003, provider="openai", model="falso"
        )


def _vision(monkeypatch, texto: str | None = '{"nombre": "Bolso de cuero", "categoria": "Bolsos", "precio": "120.000"}') -> Vision:
    from core.media.config import VisionConfig
    from core.qa import importar_fotos

    doble = Vision(texto)
    monkeypatch.setattr("core.media.vision.describir_imagen", doble)
    monkeypatch.setattr(
        importar_fotos,
        "_config_vision",
        lambda _ajustes: VisionConfig(
            enabled=True, mode="describir", provider="openai", model="falso", api_key="k", key_mode="platform"
        ),
    )
    return doble


@pytest.fixture
def produccion(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "qa_testers_enabled", False)


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = f"fotos-{uuid.uuid4().hex[:8]}"
    tid, _ = await onboard_tenant(
        name=f"Fotos {slug}",
        slug=slug,
        business_description=DESCRIPCION,
        packs=["pack_commerce"],
        admin_email="admin@local",
        sembrar_ejemplos=False,
        plan="free",
    )
    return tid


async def _foto(app_client, h, agente, contenido: bytes = PNG):
    return await app_client.post(
        f"{_BASE}/{agente}/importar-datos/fotos", headers=h, files={"archivo": ("foto.png", contenido, "image/png")}
    )


async def _ledger_vision(tid: uuid.UUID) -> list[tuple]:
    return await _sql(
        tid,
        "select result->>'image_key', result->>'proposito', credits from usage_ledger "
        "where tenant_id = :t and kind = 'vision' order by created_at",
        t=tid,
    )


async def test_foto_cotizada_reconocida_idempotente_y_confirmada(app_client, monkeypatch, produccion) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    vision = _vision(monkeypatch)

    estado = (await app_client.get(f"{_BASE}/{ventas}/importar-datos", headers=h)).json()
    assert estado["plantilla"]["importa_fotos"] is True
    cot = await app_client.get(f"{_BASE}/{ventas}/importar-datos/fotos/cotizacion?fotos=3", headers=h)
    assert cot.status_code == 200, cot.text
    cot = cot.json()
    assert cot["alcanza"] is True and cot["llave_propia"] is False and cot["limite"] == 2500
    assert cot["por_foto"] >= 2 and cot["creditos"] >= cot["por_foto"] * 3 - 1
    assert await _ledger_vision(tid) == []

    r = await _foto(app_client, h, ventas)
    assert r.status_code == 200, r.text
    fila = r.json()
    assert fila["repetida"] is False and Decimal(str(fila["creditos"])) > Decimal("1.0")
    valores = fila["valores"]
    assert valores["nombre"] == "Bolso de cuero" and valores["precio"] == "120000"
    assert valores["sku"].startswith("FOTO-") and valores["imagen"] == fila["image_key"]
    assert fila["image_key"].endswith(".png") and "/productos/" in fila["image_key"]
    ((key, proposito, creditos),) = await _ledger_vision(tid)
    assert (key, proposito) == (fila["image_key"], "foto_catalogo") and creditos > 0

    # La misma foto otra vez: misma fila, sin llamar ni cobrar.
    r = await _foto(app_client, h, ventas)
    assert r.status_code == 200 and r.json()["repetida"] is True and r.json()["valores"] == valores
    assert vision.llamadas == 1 and len(await _ledger_vision(tid)) == 1

    # Confirmar con la precarga: el producto queda con su foto.
    r = await app_client.post(
        f"{_BASE}/{ventas}/importar-datos/filas", headers=h, json={"filas": [{"fila": 1, "valores": valores}]}
    )
    assert r.status_code == 201, r.text
    filas = await _sql(
        tid,
        "select p.name, p.image_key from pack_commerce_products p where p.tenant_id = :t and p.deleted_at is null",
        t=tid,
    )
    assert filas == [("Bolso de cuero", fila["image_key"])]


async def test_sin_credito_no_llama_ni_cobra(app_client, monkeypatch, produccion) -> None:
    from core.db.models import UsageLedger
    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    vision = _vision(monkeypatch)
    async with tenant_scope(tid), get_session() as session:
        session.add(
            UsageLedger(
                tenant_id=tid, kind="turn", model="m", input_tokens=1, output_tokens=1, cost_usd=Decimal("0"),
                credits=Decimal("2499.5"), key_mode="platform", tarifario_version="v1", trm_cop=Decimal("4000"),
            )
        )

    cot = (await app_client.get(f"{_BASE}/{ventas}/importar-datos/fotos/cotizacion?fotos=1", headers=h)).json()
    assert cot["alcanza"] is False and cot["disponible"] == 0
    r = await _foto(app_client, h, ventas)
    assert r.status_code == 409, r.text
    error = r.json()["error"]
    assert error["code"] == "plan_limit_creditos" and error["details"]["metric"] == "creditos"
    assert vision.llamadas == 0 and await _ledger_vision(tid) == []


async def test_foto_no_reconocida_se_cobra_una_vez(app_client, monkeypatch, produccion) -> None:
    tid = await _negocio()
    ventas = await _agente(tid, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, tid)
    vision = _vision(monkeypatch, "{}")

    for _ in range(2):
        r = await _foto(app_client, h, ventas, OTRA)
        assert r.status_code == 422 and r.json()["error"]["code"] == "foto_no_reconocida"
    assert vision.llamadas == 1 and len(await _ledger_vision(tid)) == 1


async def test_archivo_que_no_es_foto_clave_ajena_y_otro_negocio(app_client, monkeypatch, produccion) -> None:
    mio = await _negocio()
    suyo = await _negocio()
    mio_ventas = await _agente(mio, "ventas", ["pack_commerce.tomar_pedido"])
    suyo_ventas = await _agente(suyo, "ventas", ["pack_commerce.tomar_pedido"])
    h = await api_headers(app_client, mio)
    h_suyo = await api_headers(app_client, suyo)
    vision = _vision(monkeypatch)

    r = await _foto(app_client, h, mio_ventas, b"no soy una imagen")
    assert r.status_code == 422 and r.json()["error"]["code"].startswith("foto_")
    for ruta in ("/fotos/cotizacion?fotos=1",):
        r = await app_client.get(f"{_BASE}/{suyo_ventas}/importar-datos{ruta}", headers=h)
        assert r.status_code == 404 and str(suyo_ventas) not in r.text
    r = await _foto(app_client, h, suyo_ventas)
    assert r.status_code == 404 and str(suyo_ventas) not in r.text
    assert vision.llamadas == 0

    # Una clave de foto del otro negocio no se puede pegar en mi precarga.
    ajena = (await _foto(app_client, h_suyo, suyo_ventas)).json()
    valores = dict(ajena["valores"], sku="AJENA-1")
    r = await app_client.post(
        f"{_BASE}/{mio_ventas}/importar-datos/filas/validar", headers=h, json={"filas": [{"fila": 1, "valores": valores}]}
    )
    assert r.json()["errores"][0]["columna"] == "imagen"
    assert await _ledger_vision(mio) == []
