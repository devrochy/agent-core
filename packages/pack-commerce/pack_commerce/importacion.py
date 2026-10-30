"""Importar productos que el dueño ya tiene desde un CSV (HU-QA-12, ADR-0180).

Una plantilla sobre el catálogo:
- `productos` (forma `catalogo`): SKU, nombre, precio, categoría, descripción y stock.

Importar planes por CSV ya no está en Comercio: los planes son de pack_subscriptions (SPEC-43) y
su importación queda para una HU aparte.

La clave es el SKU, que es único por negocio. Un SKU de un producto vivo ya existe y no se toca; el de
un producto eliminado a mano es un error de la fila (no se resucita lo que el negocio borró). Los
productos que retira una importación quedan con el SKU liberado (`retirar` en `datos_de_prueba.py`),
así que volver a importar el archivo después de borrar los crea otra vez. Reusa `seed_catalog`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from core.db.session import get_session
from core.qa.contrato_datos import RegistroSembrado
from core.qa.contrato_importacion import (
    ColumnaDeImportacion,
    ContextoDeImportacion,
    FilaDeImportacion,
    PlantillaDeImportacion,
    ResultadoDeImportacion,
    entero,
    numero,
)
from core.shared.tenant import tenant_scope
from pack_commerce.models import Product, ProductVariant

CATEGORIA_SIN_CLASIFICAR = "Sin categoría"
MAX_PRECIO = Decimal("9999999999")


def _precio(valor: Any) -> Decimal:
    n = numero(valor)
    if n < 0:
        raise ValueError("El precio no puede ser negativo.")
    if n > MAX_PRECIO:
        raise ValueError("El precio es demasiado grande.")
    return n.quantize(Decimal("0.01"))


class _ConSku(FilaDeImportacion):
    sku: Annotated[str, Field(min_length=1, max_length=60)]
    nombre: Annotated[str, Field(min_length=2, max_length=120)]
    precio: Decimal
    descripcion: Annotated[str, Field(max_length=1000)] = ""

    @field_validator("precio", mode="before")
    @classmethod
    def _precio_valido(cls, valor: Any) -> Decimal:
        return _precio(valor)

    @field_validator("descripcion", mode="before")
    @classmethod
    def _sin_nulo(cls, valor: Any) -> str:
        return valor or ""


class ProductoImportado(_ConSku):
    categoria: Annotated[str, Field(max_length=200)] = ""
    stock: Annotated[int, Field(ge=0, le=1_000_000)] = 0

    @field_validator("stock", mode="before")
    @classmethod
    def _stock_valido(cls, valor: Any) -> int:
        return 0 if valor in (None, "") else entero(valor)

    @field_validator("categoria", mode="before")
    @classmethod
    def _categoria_sin_nulo(cls, valor: Any) -> str:
        return valor or ""

    def ruta_de_categoria(self) -> list[str]:
        ruta = [p.strip() for p in self.categoria.split(">") if p.strip()]
        return [p[:80] for p in ruta[:3]] or [CATEGORIA_SIN_CLASIFICAR]


def plantillas() -> list[PlantillaDeImportacion]:
    return [
        PlantillaDeImportacion(
            id="productos",
            titulo="Productos",
            modo="catalogo",
            columnas=(
                ColumnaDeImportacion("sku", "Código del producto; no se repite en tu catálogo.", obligatoria=True),
                ColumnaDeImportacion("nombre", "Nombre del producto.", obligatoria=True),
                ColumnaDeImportacion("precio", "Precio en la moneda del negocio, por ejemplo 45000.", obligatoria=True),
                ColumnaDeImportacion("categoria", "Categoría; para una subcategoría use «Cabello > Ceras»."),
                ColumnaDeImportacion("descripcion", "Descripción (opcional)."),
                ColumnaDeImportacion("stock", "Unidades disponibles (opcional, 0 si no se indica)."),
            ),
            ejemplos=(
                {
                    "sku": "CERA-001",
                    "nombre": "Cera mate para el cabello",
                    "precio": "28000",
                    "categoria": "Cabello > Ceras",
                    "descripcion": "Fijación media, 100 g",
                    "stock": "12",
                },
                {"sku": "ACE-002", "nombre": "Aceite para barba", "precio": "35000", "categoria": "Barba", "stock": "8"},
            ),
            esquema=ProductoImportado,
            columna_clave="sku",
            clave=lambda fila: fila.sku,
            ayuda="Un producto por fila, con una sola presentación. Si el SKU ya existe en tu catálogo, esa fila se omite.",
        ),
    ]


async def _estado_de_skus(tenant_id: uuid.UUID, skus: list[str]) -> dict[str, bool]:
    """`sku → producto vivo` de los SKU que ya existen en el negocio."""
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            filas = await session.execute(
                select(ProductVariant.sku, Product.deleted_at)
                .join(Product, Product.id == ProductVariant.product_id)
                .where(
                    ProductVariant.tenant_id == tenant_id,
                    Product.tenant_id == tenant_id,
                    ProductVariant.sku.in_(skus),
                )
            )
            return {sku: borrado is None for sku, borrado in filas.all()}


def _a_seed(plantilla_id: str, filas: list[_ConSku]) -> dict[str, Any]:
    productos = []
    rutas: list[list[str]] = []
    for fila in filas:
        assert isinstance(fila, ProductoImportado)
        ruta = fila.ruta_de_categoria()
        variante: dict[str, Any] = {"sku": fila.sku, "price": float(fila.precio), "stock": fila.stock, "attributes": {}}
        if ruta not in rutas:
            rutas.append(ruta)
        productos.append(
            {"name": fila.nombre, "description": fila.descripcion, "category_path": ruta, "variants": [variante]}
        )
    return {"category_paths": rutas, "products": productos}


async def importar(
    tenant_id: uuid.UUID,
    plantilla_id: str,
    filas: list[BaseModel],
    contexto: ContextoDeImportacion,
    *,
    simular: bool,
) -> ResultadoDeImportacion:
    from pack_commerce.seed import seed_catalog

    estado = await _estado_de_skus(tenant_id, [f.sku for f in filas])  # type: ignore[attr-defined]
    resultado = ResultadoDeImportacion()
    nuevas: list[_ConSku] = []
    for i, fila in enumerate(filas):
        assert isinstance(fila, _ConSku)
        vivo = estado.get(fila.sku)
        if vivo is None:
            resultado.nuevas.append(i)
            nuevas.append(fila)
        elif vivo:
            resultado.existentes.append(i)
        else:
            resultado.errores[i] = ("sku", "Ese SKU pertenece a un producto que eliminaste; usa otro SKU.")
    if nuevas and not simular:
        creados: list[tuple[str, uuid.UUID]] = []
        await seed_catalog(tenant_id, _a_seed(plantilla_id, nuevas), creados=creados)
        resultado.creados = [RegistroSembrado(kind=k, ref=r) for k, r in creados]
    return resultado
