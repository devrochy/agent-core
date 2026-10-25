"""Foto de producto: una `image_key` con `..` no llega a archivos de otro negocio (API real).

Auditoría multi-tenant 2026-10-25: el prefijo `<slug>/productos/` bastaba para colar
`<slug>/productos/../../<otro>/…`, firmar su URL y, al reemplazar la foto, borrarlo.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from _api_helpers import api_headers, uniq

pytestmark = [
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.aislamiento,
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
BASE = "/api/v1/pack/commerce"


@pytest.fixture
def raiz(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from core.config import get_settings

    monkeypatch.setenv("TENANTS_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _negocio() -> tuple[uuid.UUID, str]:
    from core.alta_negocio import onboard_tenant

    slug = uniq("foto")
    tid, _ = await onboard_tenant(
        name=f"Foto {slug}", slug=slug, admin_email="admin@local", packs=["pack_commerce"],
        sembrar_ejemplos=False,
    )
    return tid, slug


async def _subir(app_client, headers: dict[str, str]) -> str:
    r = await app_client.post(f"{BASE}/products/image", headers=headers, files={"archivo": ("a.png", PNG, "image/png")})
    assert r.status_code == 201, r.text
    return r.json()["image_key"]


async def _productos(app_client, headers: dict[str, str]) -> list[dict]:
    r = await app_client.get(f"{BASE}/products", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def test_image_key_con_traversal_se_rechaza_y_no_toca_al_otro_negocio(app_client, raiz: Path) -> None:
    tid_a, slug_a = await _negocio()
    tid_b, _slug_b = await _negocio()
    ha = await api_headers(app_client, tid_a)
    hb = await api_headers(app_client, tid_b)
    key_b = await _subir(app_client, hb)
    archivo_b = raiz / key_b
    assert archivo_b.is_file()

    carpeta_b, nombre_b = key_b.rsplit("/", 1)
    ataque = f"{slug_a}/productos/../../{carpeta_b}/{nombre_b}"
    # Creación: se rechaza.
    r = await app_client.post(f"{BASE}/products", headers=ha, json={"name": "Robado", "price": 1000, "image_key": ataque})
    assert r.status_code == 400 and r.json()["error"]["code"] == "image_key_invalida", r.text
    assert await _productos(app_client, ha) == []

    # Edición: un producto con foto propia no puede cambiarla a la ajena, ni se borra la propia.
    propia = await _subir(app_client, ha)
    r = await app_client.post(f"{BASE}/products", headers=ha, json={"name": "Mio", "price": 1000, "image_key": propia})
    assert r.status_code == 204, r.text
    producto_id = (await _productos(app_client, ha))[0]["id"]
    r = await app_client.post(
        f"{BASE}/products", headers=ha, json={"product_id": producto_id, "name": "Mio", "price": 1000, "image_key": ataque}
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "image_key_invalida", r.text
    assert (raiz / propia).is_file()
    assert archivo_b.is_file()


async def test_image_key_hostil_ya_guardada_no_se_sirve_ni_se_borra(app_client, raiz: Path) -> None:
    from pack_commerce.models import Product
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid_a, slug_a = await _negocio()
    tid_b, _slug_b = await _negocio()
    ha = await api_headers(app_client, tid_a)
    hb = await api_headers(app_client, tid_b)
    key_b = await _subir(app_client, hb)
    carpeta_b, nombre_b = key_b.rsplit("/", 1)
    ataque = f"{slug_a}/productos/../../{carpeta_b}/{nombre_b}"

    r = await app_client.post(f"{BASE}/products", headers=ha, json={"name": "Viejo", "price": 1000})
    assert r.status_code == 204, r.text
    producto = (await _productos(app_client, ha))[0]
    async with tenant_scope(tid_a):
        async with get_session() as s:
            fila = (await s.execute(select(Product).where(Product.id == uuid.UUID(producto["id"])))).scalar_one()
            fila.image_key = ataque  # dato de antes del arreglo
    # No se firma una URL del archivo ajeno.
    listado = (await _productos(app_client, ha))[0]
    assert listado["image_url"] is None

    # Reemplazar la foto ya no borra el archivo de B.
    propia = await _subir(app_client, ha)
    r = await app_client.post(
        f"{BASE}/products", headers=ha, json={"product_id": producto["id"], "name": "Viejo", "price": 1000, "image_key": propia}
    )
    assert r.status_code == 204, r.text
    assert (raiz / key_b).is_file()
    assert (await _productos(app_client, ha))[0]["image_url"]


async def test_reemplazar_la_foto_propia_borra_la_anterior(app_client, raiz: Path) -> None:
    tid, _slug = await _negocio()
    h = await api_headers(app_client, tid)
    primera = await _subir(app_client, h)
    r = await app_client.post(f"{BASE}/products", headers=h, json={"name": "P", "price": 1000, "image_key": primera})
    assert r.status_code == 204, r.text
    producto_id = (await _productos(app_client, h))[0]["id"]
    segunda = await _subir(app_client, h)
    r = await app_client.post(
        f"{BASE}/products", headers=h, json={"product_id": producto_id, "name": "P", "price": 1000, "image_key": segunda}
    )
    assert r.status_code == 204, r.text
    assert not (raiz / primera).exists()
    assert (raiz / segunda).is_file()
