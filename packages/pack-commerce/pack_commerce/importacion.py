"""Importar productos que el dueño ya tiene desde un CSV (HU-QA-12, ADR-0180; en producción, ADR-0194).

Una plantilla sobre el catálogo:
- `productos` (forma `catalogo`): SKU, nombre, precio, categoría, descripción y stock.

Importar planes por CSV ya no está en Comercio: los planes son del pack de Suscripciones (SPEC-43) y
su importación queda para una HU aparte.

Fotos (ADR-0194): una foto es un producto. La IA de visión devuelve `ProductoEnFoto` y
`_fila_de_foto` lo convierte en una fila de la precarga con un SKU propuesto (`FOTO-<hash>`, el dueño
lo puede cambiar) y la clave de la foto en la columna interna `imagen`, que al importar se valida
(tiene que ser una foto de productos de este negocio) y queda como foto del producto.

La clave es el SKU, que es único por negocio. Un SKU de un producto vivo ya existe y no se toca; el de
un producto eliminado a mano es un error de la fila (no se resucita lo que el negocio borró). Los
productos que retira una importación quedan con el SKU liberado (`retirar` en `datos_de_prueba.py`),
así que volver a importar el archivo después de borrar los crea otra vez. Reusa `seed_catalog`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
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
from core.shared.tenant import tenant_scope, tenant_slug
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
    # Clave de la foto subida (solo la llena el flujo de fotos; el CSV no trae esta columna).
    imagen: Annotated[str, Field(max_length=300)] = ""

    @field_validator("imagen", mode="before")
    @classmethod
    def _imagen_sin_nulo(cls, valor: Any) -> str:
        return valor or ""

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


class ProductoEnFoto(BaseModel):
    """Lo que la IA de visión ve en la foto de un producto. Dato no confiable: se valida aquí."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    nombre: Annotated[str, Field(min_length=2, max_length=120)]
    descripcion: Annotated[str, Field(max_length=1000)] = ""
    categoria: Annotated[str, Field(max_length=200)] = ""
    precio: Decimal | None = Field(default=None, description="Solo si el precio se ve en la foto.")

    @field_validator("descripcion", "categoria", mode="before")
    @classmethod
    def _texto(cls, valor: Any) -> str:
        return valor or ""

    @field_validator("precio", mode="before")
    @classmethod
    def _precio_visible(cls, valor: Any) -> Decimal | None:
        if valor in (None, ""):
            return None
        try:
            return _precio(valor)
        except ValueError:
            return None  # un precio ilegible no tumba la foto: el dueño lo escribe


def _fila_de_foto(datos: Any, image_key: str) -> dict[str, str]:
    assert isinstance(datos, ProductoEnFoto)
    huella = image_key.rsplit("/", 1)[-1].split(".", 1)[0][:8].upper()
    return {
        "sku": f"FOTO-{huella}",
        "nombre": datos.nombre,
        "precio": "" if datos.precio is None else format(datos.precio.normalize(), "f"),
        "categoria": datos.categoria,
        "descripcion": datos.descripcion,
        "stock": "",
        "imagen": image_key,
    }


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
            sinonimos={
                "sku": ("codigo", "cod", "referencia", "ref", "codigo_de_barras", "ean", "id_producto", "item"),
                "nombre": ("producto", "nombre_del_producto", "articulo", "titulo", "name", "product", "item_name"),
                "precio": ("valor", "precio_de_venta", "precio_venta", "pvp", "price", "costo_venta", "valor_unitario"),
                "categoria": ("linea", "familia", "grupo", "tipo", "category", "departamento"),
                "descripcion": ("detalle", "descripcion_del_producto", "observaciones", "description", "notas"),
                "stock": ("cantidad", "existencias", "inventario", "unidades", "disponible", "qty", "quantity"),
            },
            # El catálogo es lo único que el dueño importa en producción (ADR-0194 §2).
            en_produccion=True,
            esquema_foto=ProductoEnFoto,
            desde_foto=_fila_de_foto,
            columna_foto="imagen",
        ),
    ]


def _foto_del_negocio(key: str, slug: str | None) -> bool:
    """La foto tiene que estar en la carpeta de productos de este negocio (no basta el texto)."""
    from core import almacenamiento
    from pack_commerce.fotos import key_foto_valida

    if not slug or not key_foto_valida(key, slug):
        return False
    return almacenamiento.abrir(key, dentro_de=almacenamiento.carpeta_de("producto", slug)) is not None


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
        producto: dict[str, Any] = {
            "name": fila.nombre,
            "description": fila.descripcion,
            "category_path": ruta,
            "variants": [variante],
        }
        if fila.imagen:
            producto["image_key"] = fila.imagen
        productos.append(producto)
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
    slug = await tenant_slug(tenant_id) if any(getattr(f, "imagen", "") for f in filas) else None
    for i, fila in enumerate(filas):
        assert isinstance(fila, _ConSku)
        vivo = estado.get(fila.sku)
        if getattr(fila, "imagen", "") and not _foto_del_negocio(fila.imagen, slug):  # type: ignore[attr-defined]
            resultado.errores[i] = ("imagen", "La foto no es válida; vuelve a subirla.")
        elif vivo is None:
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
