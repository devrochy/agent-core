"""HU-CRM-03 · aliados (proveedores) opcionales por negocio, contra el stack real.

- **Apagado por defecto:** `/pack/commerce/allies` responde 404 `aliados_deshabilitados` y
  `/modulos` dice `aliados: false`; asignar un aliado a un producto también da 404.
- **Habilitar:** solo el master, por `/pack/commerce/platform/tenants/{id}/aliados`; un dueño
  recibe 403. Queda en `audit_log`.
- **Crear y asignar:** CRUD, filtro `?ally_id=` (y `none` para «sin aliado»); editar un producto
  sin mandar `ally_id` no lo borra (la APK no conoce los aliados).
- **Import CSV:** columna `aliado`; un nombre que no existe sale en `unknown_allies` y la fila
  se crea sin aliado.
- **Promoción por aliado:** aplica si todo el carrito es de ese aliado; con un carrito mixto, no.
- **Borrar:** sus productos quedan sin aliado y sus promociones se desactivan.
- **Aislamiento:** el aliado de otro negocio no se ve ni se puede asignar, y encender los
  aliados de un negocio no los enciende en otro.
"""

from __future__ import annotations

import os
import uuid

import pytest
from _api_helpers import ADMIN_EMAIL, api_headers, uniq

pytestmark = [
    pytest.mark.aislamiento,
    pytest.mark.asyncio(loop_scope="session"),
    pytest.mark.skipif(
        os.environ.get("AGENT_CORE_INTEGRATION") != "1",
        reason="tests de integración: requiere AGENT_CORE_INTEGRATION=1 y el stack local",
    ),
]

BASE = "/api/v1/pack/commerce"
PASSWORD = "clave-segura-aliados-123"


async def _negocio() -> uuid.UUID:
    from core.alta_negocio import onboard_tenant

    slug = uniq("aliados")
    tid, _ = await onboard_tenant(
        name=f"Tenant aliados {slug}",
        slug=slug,
        business_description="Test de aliados.",
        persona={"tone": "cercano"},
        admin_email=ADMIN_EMAIL,
    )
    return tid


async def _dueno(tid: uuid.UUID) -> str:
    from core.admin.auth import hash_password
    from core.db.models import Admin, TenantUser
    from core.db.session import get_session

    email = f"{uniq('dueno-aliados')}@example.test"
    async with get_session() as session:
        admin = Admin(email=email, password_hash=hash_password(PASSWORD), role="colaborador")
        session.add(admin)
        await session.flush()
        session.add(TenantUser(tenant_id=tid, admin_id=admin.id, role="owner"))
    return email


async def _encender(app_client, tid: uuid.UUID, master: dict[str, str], habilitado: bool = True) -> None:
    r = await app_client.put(
        f"{BASE}/platform/tenants/{tid}/aliados", headers=master, json={"habilitado": habilitado}
    )
    assert r.status_code == 200, r.text
    assert r.json()["habilitado"] is habilitado


async def _crear_aliado(app_client, headers: dict[str, str], nombre: str) -> str:
    r = await app_client.post(
        f"{BASE}/allies", headers=headers, json={"name": nombre, "contact_info": {"telefono": "3001234567"}}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _crear_producto(app_client, headers: dict[str, str], nombre: str, **extra) -> str:  # type: ignore[no-untyped-def]
    sku = f"AL-{uuid.uuid4().hex[:6]}"
    r = await app_client.post(
        f"{BASE}/products", headers=headers, json={"name": nombre, "sku": sku, "price": 10_000, "stock": 5, **extra}
    )
    assert r.status_code == 204, r.text
    productos = (await app_client.get(f"{BASE}/products", headers=headers)).json()
    return next(p["id"] for p in productos if p["name"] == nombre)


async def test_apagado_por_defecto_responde_404(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)

    assert (await app_client.get(f"{BASE}/modulos", headers=h)).json() == {"aliados": False}
    for metodo, url, body in (
        ("get", f"{BASE}/allies", None),
        ("post", f"{BASE}/allies", {"name": "Distribuidora Norte"}),
        ("patch", f"{BASE}/allies/{uuid.uuid4()}", {"name": "x"}),
        ("delete", f"{BASE}/allies/{uuid.uuid4()}", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        r = await getattr(app_client, metodo)(url, headers=h, **kwargs)
        assert r.status_code == 404, (metodo, url, r.text)
        assert r.json()["error"]["code"] == "aliados_deshabilitados"

    # Asignar un aliado con el módulo apagado tampoco pasa; sin aliado, el producto se guarda igual.
    r = await app_client.post(
        f"{BASE}/products", headers=h, json={"name": "Con aliado", "price": 1, "ally_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404
    pid = await _crear_producto(app_client, h, "Sin aliado")
    producto = next(p for p in (await app_client.get(f"{BASE}/products", headers=h)).json() if p["id"] == pid)
    assert producto["ally_id"] is None


async def test_solo_el_master_enciende_los_aliados(app_client) -> None:
    from sqlalchemy import text

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    master = await api_headers(app_client, tid)
    dueno = await api_headers(app_client, tid, email=await _dueno(tid), password=PASSWORD)
    url = f"{BASE}/platform/tenants/{tid}/aliados"

    assert (await app_client.get(url, headers=dueno)).status_code == 403
    assert (await app_client.put(url, headers=dueno, json={"habilitado": True})).status_code == 403
    assert (await app_client.get(f"{BASE}/modulos", headers=dueno)).json() == {"aliados": False}

    assert (await app_client.get(f"{BASE}/platform/tenants/{uuid.uuid4()}/aliados", headers=master)).status_code == 404
    await _encender(app_client, tid, master)
    estado = (await app_client.get(url, headers=master)).json()
    assert estado["habilitado"] is True
    assert estado["por"] == f"admin:{ADMIN_EMAIL}"
    # El dueño ve la sección y puede usar el CRUD.
    assert (await app_client.get(f"{BASE}/modulos", headers=dueno)).json() == {"aliados": True}
    assert (await app_client.get(f"{BASE}/allies", headers=dueno)).status_code == 200

    async with tenant_scope(tid):
        async with get_session() as session:
            acciones = (
                await session.execute(
                    text("select action from audit_log where tenant_id = :t and action like 'aliados_%'"),
                    {"t": tid},
                )
            ).scalars().all()
    assert acciones == ["aliados_habilitados"]


async def test_crear_asignar_y_filtrar(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    await _encender(app_client, tid, h)

    norte = await _crear_aliado(app_client, h, "Distribuidora Norte")
    # Nombre único sin distinguir mayúsculas.
    r = await app_client.post(f"{BASE}/allies", headers=h, json={"name": "distribuidora norte"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "aliado_duplicado"

    con = await _crear_producto(app_client, h, "Camiseta Norte", ally_id=norte)
    sin = await _crear_producto(app_client, h, "Camiseta propia")

    del_norte = (await app_client.get(f"{BASE}/products", headers=h, params={"ally_id": norte})).json()
    assert [p["id"] for p in del_norte] == [con]
    assert del_norte[0]["ally_id"] == norte
    sin_aliado = (await app_client.get(f"{BASE}/products", headers=h, params={"ally_id": "none"})).json()
    assert [p["id"] for p in sin_aliado] == [sin]
    assert (await app_client.get(f"{BASE}/products", headers=h, params={"ally_id": "x"})).status_code == 400

    # Editar sin mandar `ally_id` (la APK) no lo borra; mandarlo en null sí.
    r = await app_client.post(
        f"{BASE}/products", headers=h, json={"product_id": con, "name": "Camiseta Norte v2", "price": 12_000, "stock": 5}
    )
    assert r.status_code == 204, r.text
    producto = next(p for p in (await app_client.get(f"{BASE}/products", headers=h)).json() if p["id"] == con)
    assert producto["ally_id"] == norte
    r = await app_client.post(
        f"{BASE}/products", headers=h, json={"product_id": con, "name": "Camiseta Norte v2", "stock": 5, "ally_id": None}
    )
    assert r.status_code == 204, r.text
    producto = next(p for p in (await app_client.get(f"{BASE}/products", headers=h)).json() if p["id"] == con)
    assert producto["ally_id"] is None

    # PATCH y conteo de productos.
    r = await app_client.post(
        f"{BASE}/products", headers=h, json={"product_id": con, "name": "Camiseta Norte v2", "stock": 5, "ally_id": norte}
    )
    assert r.status_code == 204
    r = await app_client.patch(
        f"{BASE}/allies/{norte}", headers=h, json={"active": False, "contact_info": {"email": "norte@example.test"}}
    )
    assert r.status_code == 200, r.text
    assert r.json()["active"] is False
    assert r.json()["products"] == 1
    assert r.json()["contact_info"]["email"] == "norte@example.test"
    assert r.json()["contact_info"]["telefono"] is None
    # Campos de contacto desconocidos: 422.
    r = await app_client.patch(f"{BASE}/allies/{norte}", headers=h, json={"contact_info": {"x": "y"}})
    assert r.status_code == 422

    # Apagar el módulo conserva los datos: al volver a encenderlo todo sigue ahí.
    await _encender(app_client, tid, h, habilitado=False)
    assert (await app_client.get(f"{BASE}/allies", headers=h)).status_code == 404
    await _encender(app_client, tid, h)
    lista = (await app_client.get(f"{BASE}/allies", headers=h)).json()
    assert [(a["id"], a["products"]) for a in lista] == [(norte, 1)]


async def test_import_csv_con_columna_aliado(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    await _encender(app_client, tid, h)
    norte = await _crear_aliado(app_client, h, "Distribuidora Norte")

    csv = (
        "name,price,sku,aliado\n"
        f"Gorra,15000,G-{uuid.uuid4().hex[:4]},distribuidora norte\n"
        f"Bolso,30000,B-{uuid.uuid4().hex[:4]},Proveedor Fantasma\n"
        f"Media,5000,M-{uuid.uuid4().hex[:4]},\n"
    )
    r = await app_client.post(
        f"{BASE}/products/import", headers=h, files={"file": ("productos.csv", csv.encode(), "text/csv")}
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"created": 3, "unknown_allies": ["Proveedor Fantasma"]}

    por_nombre = {p["name"]: p["ally_id"] for p in (await app_client.get(f"{BASE}/products", headers=h)).json()}
    assert por_nombre == {"Gorra": norte, "Bolso": None, "Media": None}

    # Aliado por defecto (selector del panel): lo toman las filas sin aliado; el nombre de la
    # columna manda y uno desconocido deja la fila sin aliado.
    sur = await _crear_aliado(app_client, h, "Distribuidora Sur")
    csv = (
        "name,price,aliado\n"
        "Termo,20000,\n"
        "Vaso,8000,Distribuidora Norte\n"
        "Plato,9000,Nadie\n"
    )
    r = await app_client.post(
        f"{BASE}/products/import",
        headers=h,
        files={"file": ("productos.csv", csv.encode(), "text/csv")},
        data={"ally_id": sur},
    )
    assert r.status_code == 201, r.text
    assert r.json()["unknown_allies"] == ["Nadie"]
    por_nombre = {p["name"]: p["ally_id"] for p in (await app_client.get(f"{BASE}/products", headers=h)).json()}
    assert (por_nombre["Termo"], por_nombre["Vaso"], por_nombre["Plato"]) == (sur, norte, None)


async def test_import_csv_con_aliados_apagados_ignora_la_columna(app_client) -> None:
    tid = await _negocio()
    h = await api_headers(app_client, tid)
    csv = f"name,price,sku,aliado\nGorra,15000,G-{uuid.uuid4().hex[:4]},Distribuidora Norte\n"
    r = await app_client.post(
        f"{BASE}/products/import", headers=h, files={"file": ("productos.csv", csv.encode(), "text/csv")}
    )
    assert r.status_code == 201, r.text
    assert r.json() == {"created": 1, "unknown_allies": []}
    # Con los aliados apagados, mandar un aliado por defecto da 404.
    r = await app_client.post(
        f"{BASE}/products/import",
        headers=h,
        files={"file": ("productos.csv", csv.encode(), "text/csv")},
        data={"ally_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404


async def _cotizacion(tid: uuid.UUID, customer_id: uuid.UUID, product_ids: list[str]) -> str:
    from pack_commerce.models import ProductVariant, Quote, QuoteItem
    from sqlalchemy import select

    from core.db.session import get_session
    from core.shared.tenant import tenant_scope

    async with tenant_scope(tid):
        async with get_session() as session:
            variantes = (
                await session.execute(
                    select(ProductVariant).where(
                        ProductVariant.tenant_id == tid,
                        ProductVariant.product_id.in_([uuid.UUID(p) for p in product_ids]),
                    )
                )
            ).scalars().all()
            subtotal = 10_000.0 * len(variantes)
            quote = Quote(
                tenant_id=tid, conversation_id=uuid.uuid4(), customer_id=customer_id,
                subtotal=subtotal, discount=0, total=subtotal, status="draft",
            )
            session.add(quote)
            await session.flush()
            for v in variantes:
                session.add(QuoteItem(tenant_id=tid, quote_id=quote.id, variant_id=v.id, quantity=1, unit_price=10_000))
            return str(quote.id)


def _ctx(customer_id: uuid.UUID):  # type: ignore[no-untyped-def]
    from core.mcp.types import ToolContext

    return ToolContext(
        conversation_id=uuid.uuid4(),
        customer_id=customer_id,
        idempotency_key=f"test:{uuid.uuid4().hex[:8]}",
        channel="whatsapp",
    )


async def test_promocion_por_aliado_no_aplica_a_un_carrito_mixto(app_client) -> None:
    from pack_commerce.skills import OrderSkill

    from core.shared.tenant import tenant_scope

    tid = await _negocio()
    h = await api_headers(app_client, tid)
    await _encender(app_client, tid, h)
    norte = await _crear_aliado(app_client, h, "Distribuidora Norte")
    sur = await _crear_aliado(app_client, h, "Distribuidora Sur")
    p_norte = await _crear_producto(app_client, h, "Producto Norte", ally_id=norte)
    p_norte2 = await _crear_producto(app_client, h, "Producto Norte 2", ally_id=norte)
    p_sur = await _crear_producto(app_client, h, "Producto Sur", ally_id=sur)
    p_propio = await _crear_producto(app_client, h, "Producto propio")

    r = await app_client.post(
        f"{BASE}/promotions",
        headers=h,
        json={"type": "temporada", "rules": {"label": "10% Norte", "discount_pct": 10}, "ally_id": norte},
    )
    assert r.status_code == 204, r.text
    promo = (await app_client.get(f"{BASE}/promotions", headers=h)).json()[0]
    assert promo["ally_id"] == norte

    cliente = uuid.uuid4()
    async with tenant_scope(tid):
        solo_norte = await OrderSkill().aplicar_promocion(
            {"quote_id": await _cotizacion(tid, cliente, [p_norte, p_norte2])}, _ctx(cliente)
        )
        mixto = await OrderSkill().aplicar_promocion(
            {"quote_id": await _cotizacion(tid, cliente, [p_norte, p_sur])}, _ctx(cliente)
        )
        con_propio = await OrderSkill().aplicar_promocion(
            {"quote_id": await _cotizacion(tid, cliente, [p_norte, p_propio])}, _ctx(cliente)
        )
    assert solo_norte["applied"] is True
    assert solo_norte["promotion_id"] == promo["id"]
    assert solo_norte["discount"] == 2_000
    assert mixto["applied"] is False
    assert con_propio["applied"] is False

    # Editar la promoción sin mandar `ally_id` lo conserva.
    r = await app_client.post(
        f"{BASE}/promotions",
        headers=h,
        json={"promotion_id": promo["id"], "type": "temporada", "rules": {"label": "15% Norte", "discount_pct": 15}},
    )
    assert r.status_code == 204, r.text
    assert (await app_client.get(f"{BASE}/promotions", headers=h)).json()[0]["ally_id"] == norte

    # Borrar el aliado: sus productos quedan sin aliado y su promoción se desactiva (no se abre
    # a todo el catálogo).
    assert (await app_client.delete(f"{BASE}/allies/{norte}", headers=h)).status_code == 204
    assert (await app_client.delete(f"{BASE}/allies/{norte}", headers=h)).status_code == 404
    por_id = {p["id"]: p["ally_id"] for p in (await app_client.get(f"{BASE}/products", headers=h)).json()}
    assert por_id[p_norte] is None and por_id[p_norte2] is None
    assert por_id[p_sur] == sur
    promo = (await app_client.get(f"{BASE}/promotions", headers=h)).json()[0]
    assert promo["active"] is False
    assert promo["ally_id"] == norte
    assert [a["id"] for a in (await app_client.get(f"{BASE}/allies", headers=h)).json()] == [sur]
    # El nombre queda libre para un aliado nuevo.
    await _crear_aliado(app_client, h, "Distribuidora Norte")


async def test_aislamiento_entre_negocios(app_client) -> None:
    a = await _negocio()
    b = await _negocio()
    ha = await api_headers(app_client, a)
    hb = await api_headers(app_client, b)
    await _encender(app_client, a, ha)

    # Encender en A no enciende en B.
    assert (await app_client.get(f"{BASE}/modulos", headers=hb)).json() == {"aliados": False}
    assert (await app_client.get(f"{BASE}/allies", headers=hb)).status_code == 404

    await _encender(app_client, b, hb)
    aliado_b = await _crear_aliado(app_client, hb, "Proveedor de B")
    aliado_a = await _crear_aliado(app_client, ha, "Proveedor de A")

    # A no ve, no edita, no borra ni asigna el aliado de B.
    assert [x["id"] for x in (await app_client.get(f"{BASE}/allies", headers=ha)).json()] == [aliado_a]
    assert (await app_client.patch(f"{BASE}/allies/{aliado_b}", headers=ha, json={"name": "robado"})).status_code == 404
    assert (await app_client.delete(f"{BASE}/allies/{aliado_b}", headers=ha)).status_code == 404
    r = await app_client.post(f"{BASE}/products", headers=ha, json={"name": "Intruso", "price": 1, "ally_id": aliado_b})
    assert r.status_code == 404
    r = await app_client.post(
        f"{BASE}/promotions", headers=ha, json={"type": "temporada", "rules": {"discount_pct": 5}, "ally_id": aliado_b}
    )
    assert r.status_code == 404
    assert (await app_client.get(f"{BASE}/products", headers=ha, params={"ally_id": aliado_b})).json() == []

    # El mismo nombre puede existir en dos negocios.
    await _crear_aliado(app_client, ha, "Proveedor de B")
    lista_b = (await app_client.get(f"{BASE}/allies", headers=hb)).json()
    assert [(x["id"], x["name"]) for x in lista_b] == [(aliado_b, "Proveedor de B")]
