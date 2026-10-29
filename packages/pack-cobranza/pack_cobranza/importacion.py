"""Importar la cartera que el dueño ya tiene desde un CSV (HU-QA-12, ADR-0180).

Plantilla `clientes_y_prestamos`: una fila por cliente, con su cartera y, si tiene, su préstamo. Las
cuotas se calculan como en el alta de préstamo de la app de campo (valor, número de cuotas, interés,
días entre cuotas y fecha de desembolso): las vencidas sin pagar quedan atrasadas (mora) y las
pagadas generan su pago. **No hay columnas de teléfono ni de correo** y, si el archivo las trae, se
ignoran: ningún recordatorio puede llegar a un número real.

La clave es cartera + documento. Un cliente vivo con ese documento en esa cartera ya existe: la fila
se omite completa (no se le agrega otro préstamo). Una cartera con ese nombre se reutiliza; una nueva
arranca con la caja justa para que los préstamos importados no la dejen negativa.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
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
    fecha,
    numero,
)
from core.shared.tenant import tenant_scope
from pack_cobranza.models import Cartera, Cliente

_CAMPOS_DE_PRESTAMO = ("valor", "num_cuotas", "interes_porcentaje", "fecha_desembolso")


def _texto_o_vacio(valor: Any) -> str:
    return valor or ""


class ClienteImportado(FilaDeImportacion):
    cartera: Annotated[str, Field(min_length=2, max_length=60)]
    numero_documento: Annotated[str, Field(pattern=r"^[0-9]{5,15}$")]
    nombre: Annotated[str, Field(min_length=2, max_length=60)]
    apellido: Annotated[str, Field(max_length=60)] = ""
    negocio: Annotated[str, Field(max_length=80)] = ""
    riesgo: Literal["blanco", "azul", "rojo"] = "blanco"
    valor: Decimal | None = None
    num_cuotas: int | None = None
    interes_porcentaje: Decimal | None = None
    dias_entre_cuotas: int = 1
    fecha_desembolso: date | None = None
    cuotas_pagadas: int = 0

    @field_validator("numero_documento", mode="before")
    @classmethod
    def _documento(cls, valor: Any) -> str:
        return "".join(c for c in str(valor or "") if c not in ". ,")

    @field_validator("apellido", "negocio", mode="before")
    @classmethod
    def _sin_nulo(cls, valor: Any) -> str:
        return _texto_o_vacio(valor)

    @field_validator("riesgo", mode="before")
    @classmethod
    def _riesgo(cls, valor: Any) -> Any:
        return str(valor).strip().lower() if valor else "blanco"

    @field_validator("valor", mode="before")
    @classmethod
    def _valor(cls, valor: Any) -> Decimal | None:
        if valor in (None, ""):
            return None
        n = numero(valor)
        if n <= 0 or n > 10_000_000_000:
            raise ValueError("El valor del préstamo debe ser mayor que cero.")
        return n

    @field_validator("num_cuotas", mode="before")
    @classmethod
    def _cuotas(cls, valor: Any) -> int | None:
        if valor in (None, ""):
            return None
        n = entero(valor)
        if not 1 <= n <= 60:
            raise ValueError("El número de cuotas debe estar entre 1 y 60.")
        return n

    @field_validator("interes_porcentaje", mode="before")
    @classmethod
    def _interes(cls, valor: Any) -> Decimal | None:
        if valor in (None, ""):
            return None
        n = numero(valor)
        if not 0 <= n <= 100:
            raise ValueError("El interés debe estar entre 0 y 100.")
        return n

    @field_validator("dias_entre_cuotas", mode="before")
    @classmethod
    def _dias(cls, valor: Any) -> int:
        if valor in (None, ""):
            return 1
        n = entero(valor)
        if not 1 <= n <= 90:
            raise ValueError("Los días entre cuotas deben estar entre 1 y 90.")
        return n

    @field_validator("fecha_desembolso", mode="before")
    @classmethod
    def _fecha(cls, valor: Any) -> date | None:
        if valor in (None, ""):
            return None
        d = fecha(valor)
        if d > date.today():
            raise ValueError("La fecha de desembolso no puede ser futura.")
        return d

    @field_validator("cuotas_pagadas", mode="before")
    @classmethod
    def _pagadas(cls, valor: Any) -> int:
        if valor in (None, ""):
            return 0
        n = entero(valor)
        if n < 0:
            raise ValueError("Las cuotas pagadas no pueden ser negativas.")
        return n

    @model_validator(mode="after")
    def _prestamo_completo(self) -> ClienteImportado:
        campos = {c: getattr(self, c) for c in _CAMPOS_DE_PRESTAMO}
        if not any(v is not None for v in campos.values()):
            if self.cuotas_pagadas:
                raise ValueError("Hay cuotas pagadas pero no hay préstamo: completa valor, num_cuotas, interes_porcentaje y fecha_desembolso.")
            return self
        faltan = [c for c, v in campos.items() if v is None]
        if faltan:
            raise ValueError("Para el préstamo faltan: " + ", ".join(faltan) + ".")
        if self.num_cuotas is not None and self.cuotas_pagadas > self.num_cuotas:
            raise ValueError("Las cuotas pagadas no pueden superar el número de cuotas.")
        return self

    @property
    def tiene_prestamo(self) -> bool:
        return self.valor is not None


def plantillas() -> list[PlantillaDeImportacion]:
    return [
        PlantillaDeImportacion(
            id="clientes_y_prestamos",
            titulo="Clientes y préstamos",
            modo="cartera",
            columnas=(
                ColumnaDeImportacion("cartera", "Nombre de la cartera; si no existe se crea.", obligatoria=True),
                ColumnaDeImportacion("numero_documento", "Documento del cliente, solo números.", obligatoria=True),
                ColumnaDeImportacion("nombre", "Nombre del cliente.", obligatoria=True),
                ColumnaDeImportacion("apellido", "Apellido (opcional)."),
                ColumnaDeImportacion("negocio", "Negocio del cliente (opcional)."),
                ColumnaDeImportacion("riesgo", "blanco, azul o rojo (opcional, blanco si se omite)."),
                ColumnaDeImportacion("valor", "Valor prestado. Déjalo vacío si el cliente no tiene préstamo."),
                ColumnaDeImportacion("num_cuotas", "Número de cuotas, de 1 a 60."),
                ColumnaDeImportacion("interes_porcentaje", "Interés total del préstamo, por ejemplo 20."),
                ColumnaDeImportacion("dias_entre_cuotas", "Días entre una cuota y la siguiente (1 si se omite)."),
                ColumnaDeImportacion("fecha_desembolso", "Fecha en que se prestó: AAAA-MM-DD o DD/MM/AAAA."),
                ColumnaDeImportacion("cuotas_pagadas", "Cuántas cuotas ya pagó (0 si se omite)."),
            ),
            ejemplos=(
                {
                    "cartera": "Cartera Centro",
                    "numero_documento": "1000000001",
                    "nombre": "Juana",
                    "apellido": "Pérez",
                    "negocio": "Tienda La Esquina",
                    "riesgo": "rojo",
                    "valor": "500000",
                    "num_cuotas": "20",
                    "interes_porcentaje": "20",
                    "dias_entre_cuotas": "1",
                    "fecha_desembolso": "2026-10-01",
                    "cuotas_pagadas": "5",
                },
                {"cartera": "Cartera Centro", "numero_documento": "1000000002", "nombre": "Mario", "apellido": "Gómez"},
            ),
            esquema=ClienteImportado,
            columna_clave="numero_documento",
            clave=lambda fila: f"{fila.cartera}|{fila.numero_documento}",
            ayuda=(
                "Un cliente por fila, con su préstamo si lo tiene; las cuotas se calculan solas. "
                "No se importan teléfonos ni correos, para que ningún recordatorio llegue a un número real."
            ),
        )
    ]


async def _existentes(
    tenant_id: uuid.UUID, filas: list[ClienteImportado]
) -> tuple[dict[str, tuple[uuid.UUID, str]], set[tuple[uuid.UUID, str]]]:
    """Carteras vivas por nombre en minúsculas (con su id y su nombre real) y pares (cartera, documento)."""
    async with tenant_scope(tenant_id):
        async with get_session() as session:
            carteras = {
                c.nombre.casefold(): (c.id, c.nombre)
                for c in (
                    await session.execute(
                        select(Cartera).where(
                            Cartera.tenant_id == tenant_id,
                            Cartera.deleted_at.is_(None),
                        )
                    )
                ).scalars()
            }
            ids = [carteras[f.cartera.casefold()][0] for f in filas if f.cartera.casefold() in carteras]
            clientes: set[tuple[uuid.UUID, str]] = set()
            if ids:
                filas_c = await session.execute(
                    select(Cliente.cartera_id, Cliente.numero_documento).where(
                        Cliente.tenant_id == tenant_id,
                        Cliente.cartera_id.in_(set(ids)),
                        Cliente.deleted_at.is_(None),
                        Cliente.numero_documento.in_({f.numero_documento for f in filas}),
                    )
                )
                clientes = {(cid, doc) for cid, doc in filas_c.all() if doc}
    return carteras, clientes


def _a_seed(nuevas: list[ClienteImportado], carteras_vivas: dict[str, tuple[uuid.UUID, str]], moneda: str) -> dict[str, Any]:
    hoy = date.today()
    por_cartera: dict[str, list[ClienteImportado]] = {}
    for fila in nuevas:
        por_cartera.setdefault(fila.cartera, []).append(fila)
    salida = []
    for nombre, clientes in por_cartera.items():
        # Una cartera que ya existe se reutiliza con su nombre real (el seed la busca con igualdad exacta).
        existente = carteras_vivas.get(nombre.casefold())
        item: dict[str, Any] = {"nombre": existente[1] if existente else nombre, "moneda": moneda, "clientes": []}
        con_prestamo = [c for c in clientes if c.tiene_prestamo]
        if existente is None:
            primero = con_prestamo[0] if con_prestamo else None
            item["tipo_interes"] = float(primero.interes_porcentaje or 0) if primero else 0
            item["num_cuotas"] = primero.num_cuotas if primero and primero.num_cuotas else 1
            item["saldo_caja"] = float(sum((c.valor or 0 for c in con_prestamo), Decimal(0)))
        for c in clientes:
            cliente: dict[str, Any] = {
                "nombre": c.nombre,
                "apellido": c.apellido,
                "numero_documento": c.numero_documento,
                "negocio": c.negocio,
                "color_riesgo": c.riesgo,
            }
            if c.tiene_prestamo and c.fecha_desembolso is not None:
                cliente["prestamo"] = {
                    "valor": float(c.valor or 0),
                    "num_cuotas": c.num_cuotas,
                    "tipo_interes": float(c.interes_porcentaje or 0),
                    "dias_entre_cuotas": c.dias_entre_cuotas,
                    "hace_dias": (hoy - c.fecha_desembolso).days,
                    "cuotas_pagadas": c.cuotas_pagadas,
                }
            item["clientes"].append(cliente)
        salida.append(item)
    return {"carteras": salida}


async def importar(
    tenant_id: uuid.UUID,
    plantilla_id: str,
    filas: list[BaseModel],
    contexto: ContextoDeImportacion,
    *,
    simular: bool,
) -> ResultadoDeImportacion:
    from pack_cobranza.seed import sembrar_cartera

    clientes_in = [f for f in filas if isinstance(f, ClienteImportado)]
    carteras, existentes = await _existentes(tenant_id, clientes_in)
    resultado = ResultadoDeImportacion()
    nuevas: list[ClienteImportado] = []
    for i, fila in enumerate(clientes_in):
        cartera = carteras.get(fila.cartera.casefold())
        cartera_id = cartera[0] if cartera else None
        if cartera_id is not None and (cartera_id, fila.numero_documento) in existentes:
            resultado.existentes.append(i)
        else:
            resultado.nuevas.append(i)
            nuevas.append(fila)
    if nuevas and not simular:
        creados: list[tuple[str, uuid.UUID]] = []
        await sembrar_cartera(tenant_id, _a_seed(nuevas, carteras, contexto.moneda), creados=creados)
        resultado.creados = [RegistroSembrado(kind=k, ref=r) for k, r in creados]
    return resultado
