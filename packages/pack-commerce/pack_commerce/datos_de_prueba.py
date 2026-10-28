"""Datos de prueba del agente de ventas y del de suscripciones (HU-QA-10, ADR-0146).

Dos formas, las dos sobre el catálogo:
- `catalogo` (agente con `pack_commerce`): categorías y productos con variantes.
- `planes`: planes como productos de la categoría «Planes» con periodicidad y días de prueba
  (`precios_de_plan`, D-16). Sin habilidad propia: la usa la plantilla del agente que la nombra
  (`datos_de_prueba` en `core/presets/plantillas/`); commerce no nombra al pack de suscripciones.

Los esquemas validan lo que genera el LLM (dato no confiable): topes de cantidad y de largo, sin
SKU propio (los pone el pack con el sufijo del lote, así nunca chocan con los del negocio) y sin
campos fuera del formato. El respaldo es el fragmento `qa` (catálogo) o `suscripciones` (planes).

Retirar es desactivar las variantes y borrar lógicamente productos y categorías creadas
(invariante 8): así el lote desaparece del panel y de las herramientas del agente.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from core.db.models import utcnow
from core.db.session import get_session
from core.qa.contrato_datos import ContextoDeNegocio, FormaDeDatos, RegistroSembrado
from core.shared.tenant import tenant_scope
from pack_commerce.models import Category, Product, ProductVariant

Texto = Annotated[str, Field(min_length=1, max_length=80)]
CATEGORIA_PLANES = "Planes"


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class VarianteDePrueba(_Estricto):
    price: Decimal = Field(ge=0, le=100_000_000, description="Precio en la moneda del negocio, sin decimales si es COP.")
    stock: int = Field(default=10, ge=0, le=500)
    attributes: dict[Texto, Texto | int] = Field(
        default_factory=dict, max_length=3, description="Atributos que distinguen la variante (talla, presentación…)."
    )


class ProductoDePrueba(_Estricto):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=400)
    category_path: list[Texto] = Field(min_length=1, max_length=2, description="Categoría y subcategoría opcional.")
    variants: list[VarianteDePrueba] = Field(min_length=1, max_length=4)


class CatalogoDePrueba(_Estricto):
    category_paths: list[list[Texto]] = Field(default_factory=list, max_length=10)
    products: list[ProductoDePrueba] = Field(min_length=3, max_length=15)


class AtributosDePlan(_Estricto):
    periodicidad: Literal["mensual", "anual"]
    trial_dias: int = Field(default=0, ge=0, le=60)


class VarianteDePlan(_Estricto):
    price: Decimal = Field(ge=0, le=100_000_000)
    attributes: AtributosDePlan


class PlanDePrueba(_Estricto):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=400, description="Qué incluye el plan.")
    variants: list[VarianteDePlan] = Field(min_length=1, max_length=2)


class PlanesDePrueba(_Estricto):
    products: list[PlanDePrueba] = Field(min_length=2, max_length=4)


def formas() -> list[FormaDeDatos]:
    from core.presets import load_fragment

    return [
        FormaDeDatos(
            modo="catalogo",
            titulo="Catálogo",
            habilidad="pack_commerce",
            prioridad=30,
            esquema=CatalogoDePrueba,
            respaldo=load_fragment("pack_commerce", "qa").seed,
        ),
        FormaDeDatos(
            modo="planes",
            titulo="Planes",
            habilidad=None,
            prioridad=20,
            esquema=PlanesDePrueba,
            respaldo=load_fragment("pack_commerce", "suscripciones").seed,
        ),
    ]


def _a_seed(modo: str, datos: BaseModel, sufijo: str) -> dict[str, Any]:
    """Del esquema validado al formato de `seed_catalog`, con SKUs del lote."""
    if modo == "planes":
        assert isinstance(datos, PlanesDePrueba)
        productos = [
            {
                "name": p.name,
                "description": p.description,
                "category_path": [CATEGORIA_PLANES],
                "variants": [{"price": v.price, "stock": 0, "attributes": v.attributes.model_dump()} for v in p.variants],
            }
            for p in datos.products
        ]
        rutas: list[list[str]] = [[CATEGORIA_PLANES]]
    else:
        assert isinstance(datos, CatalogoDePrueba)
        productos = [
            {
                "name": p.name,
                "description": p.description,
                "category_path": list(p.category_path),
                "variants": [v.model_dump() for v in p.variants],
            }
            for p in datos.products
        ]
        rutas = [list(r) for r in datos.category_paths]
    n = 0
    for producto in productos:
        for variante in producto["variants"]:
            n += 1
            variante["sku"] = f"PRUEBA-{sufijo}-{n:02d}"
            variante["price"] = float(variante["price"])
    return {"category_paths": rutas, "products": productos}


async def _snapshots_de_variantes(tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    """Snapshot de los planes creados, para que Suscripciones no tenga que leer Comercio."""
    if not ids:
        return {}
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = (
                await session.execute(
                    select(ProductVariant, Product)
                    .join(Product, Product.id == ProductVariant.product_id)
                    .where(
                        ProductVariant.tenant_id == tenant_id,
                        ProductVariant.id.in_(ids),
                        Product.tenant_id == tenant_id,
                    )
                )
            ).all()
    return {
        variante.id: {
            "nombre": producto.name,
            "precio": float(variante.price or 0),
            "moneda": None,
            "periodicidad": variante.billing_period or "mensual",
            "trial_dias": int(variante.trial_days or 0),
            "sku": variante.sku,
        }
        for variante, producto in filas
    }


async def sembrar(
    tenant_id: uuid.UUID, modo: str, datos: BaseModel, contexto: ContextoDeNegocio
) -> list[RegistroSembrado]:
    from pack_commerce.seed import seed_catalog

    creados: list[tuple[str, uuid.UUID]] = []
    await seed_catalog(tenant_id, _a_seed(modo, datos, contexto.sufijo), creados=creados)
    if modo != "planes":
        return [RegistroSembrado(kind=k, ref=r) for k, r in creados]
    snapshots = await _snapshots_de_variantes(
        tenant_id, [r for k, r in creados if k == "variante"]
    )
    registros: list[RegistroSembrado] = []
    for kind, ref in creados:
        plan = snapshots.get(ref)
        if plan is not None:
            plan["moneda"] = contexto.moneda
        registros.append(
            RegistroSembrado(
                kind=kind,
                ref=ref,
                metadata={"plan": plan} if plan is not None else None,
            )
        )
    return registros


async def retirar(tenant_id: uuid.UUID, registros: list[RegistroSembrado]) -> int:
    variantes = [r.ref for r in registros if r.kind == "variante"]
    categorias = [r.ref for r in registros if r.kind == "categoria"]
    productos = [r.ref for r in registros if r.kind == "producto"]
    total = 0
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            if variantes:
                res = await session.execute(
                    update(ProductVariant)
                    .where(ProductVariant.tenant_id == tenant_id, ProductVariant.id.in_(variantes))
                    .values(active=False)
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
            if productos:
                res = await session.execute(
                    update(Product)
                    .where(
                        Product.tenant_id == tenant_id,
                        Product.id.in_(productos),
                        Product.deleted_at.is_(None),
                    )
                    .values(deleted_at=utcnow())
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
            if categorias:
                res = await session.execute(
                    update(Category)
                    .where(
                        Category.tenant_id == tenant_id,
                        Category.id.in_(categorias),
                        Category.deleted_at.is_(None),
                        # Una categoría del lote que ya tiene productos vivos de otros (p. ej.
                        # «Planes» reutilizada por el negocio) se queda: no deja huérfanos.
                        ~select(Product.id)
                        .where(
                            Product.tenant_id == tenant_id,
                            Product.category_id == Category.id,
                            Product.deleted_at.is_(None),
                            Product.id.not_in(productos or [uuid.uuid4()]),
                        )
                        .exists(),
                    )
                    .values(deleted_at=utcnow(), active=False)
                )
                total += res.rowcount or 0  # type: ignore[attr-defined]
    return total
