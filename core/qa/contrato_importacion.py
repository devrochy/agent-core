"""Contrato entre el core y los packs para importar datos existentes por agente (HU-QA-12, ADR-0180).

El dueño sube un CSV con sus datos (productos, tipos de cita, cartera, planes) para probar el agente
con datos reales. El core se ocupa del archivo (decodificar, separador, cabeceras, tope, validación por
fila con su reporte, claves repetidas, lote, lock, auditoría); el pack declara sus plantillas
(`PlantillaDeImportacion`: columnas, ejemplo y esquema por fila) y escribe en sus tablas
(`importar_filas`). Lo que el pack crea se devuelve como `RegistroSembrado` para retirarlo después con
`retirar_datos_de_prueba`, igual que lo generado (HU-QA-10).

Vive en el core y no en `agentcore_contracts`, como `contrato_datos.py` (ADR-0124).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.qa.contrato_datos import RegistroSembrado

TOPE_FILAS = 500
TOPE_BYTES = 1_000_000
TOPE_ERRORES_MOSTRADOS = 50


@dataclass(frozen=True)
class ColumnaDeImportacion:
    """Una columna de la plantilla. `nombre` es el de la cabecera del CSV (minúsculas, sin tildes)."""

    nombre: str
    descripcion: str
    obligatoria: bool = False


@dataclass(frozen=True)
class PlantillaDeImportacion:
    """Una plantilla de importación de un pack.

    - `id`: estable dentro del pack (`productos`, `tipos_de_cita`, `clientes_y_prestamos`, `planes`).
    - `modo`: la `FormaDeDatos.modo` del mismo pack a la que pertenece; el agente que usa esa forma
      importa con esta plantilla.
    - `columnas`: orden de la plantilla descargable; las que no estén aquí se ignoran con aviso.
    - `ejemplos`: filas de ejemplo de la plantilla (valores ya como texto del CSV).
    - `esquema`: valida una fila (`extra="ignore"`); sus validadores lanzan `ValueError` con texto
      para el dueño, en español. Los nombres de campo son los de las columnas.
    - `columna_clave` / `clave`: clave natural de la fila (SKU, nombre, documento). Dos filas del mismo
      archivo con la misma clave son un error de la segunda.
    """

    id: str
    titulo: str
    modo: str
    columnas: tuple[ColumnaDeImportacion, ...]
    ejemplos: tuple[dict[str, str], ...]
    esquema: type[BaseModel]
    columna_clave: str
    clave: Callable[[Any], str]
    ayuda: str = ""

    @property
    def nombres(self) -> tuple[str, ...]:
        return tuple(c.nombre for c in self.columnas)

    @property
    def obligatorias(self) -> tuple[str, ...]:
        return tuple(c.nombre for c in self.columnas if c.obligatoria)


@dataclass(frozen=True)
class ContextoDeImportacion:
    moneda: str
    zona_horaria: str


@dataclass
class ResultadoDeImportacion:
    """Lo que hizo (o haría, con `simular`) el pack con las filas válidas.

    - `creados`: registros que se crean, para el lote (con `simular` van vacíos).
    - `nuevas`: índices (de la lista recibida) que se crean o se crearían.
    - `existentes`: índices cuya clave ya existe en el negocio: no se tocan.
    - `errores`: índice → (columna, mensaje) de filas que el pack no puede crear (p. ej. un SKU que
      pertenece a un producto eliminado).
    """

    creados: list[RegistroSembrado] = field(default_factory=list)
    nuevas: list[int] = field(default_factory=list)
    existentes: list[int] = field(default_factory=list)
    errores: dict[int, tuple[str | None, str]] = field(default_factory=dict)


class FilaDeImportacion(BaseModel):
    """Base de los esquemas por fila: espacios recortados y columnas desconocidas ignoradas."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ------------------------------------------------------------------ lectura de valores del CSV
def normalizar_cabecera(texto: str) -> str:
    """`«Número de Documento»` → `numero_de_documento`."""
    sin_tildes = "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.strip().lower()).strip("_")


_MILES_COMA = re.compile(r"^\d{1,3}(,\d{3})+$")
_MILES_PUNTO = re.compile(r"^\d{1,3}(\.\d{3})+$")


def numero(valor: Any) -> Decimal:
    """Número escrito como en Excel: `1500`, `1.500.000`, `1,500.50`, `1500,5`, `$ 20.000`.

    Lanza `ValueError` si no es un número.
    """
    if isinstance(valor, Decimal):
        return valor
    if isinstance(valor, int | float):
        return Decimal(str(valor))
    texto = re.sub(r"[\s$]", "", str(valor or ""))
    if not texto:
        raise ValueError("Debe ser un número.")
    if "," in texto and "." in texto:
        decimal, miles = ("," , ".") if texto.rfind(",") > texto.rfind(".") else (".", ",")
        texto = texto.replace(miles, "").replace(decimal, ".")
    elif "," in texto:
        texto = texto.replace(",", "") if _MILES_COMA.match(texto) else texto.replace(",", ".")
    elif "." in texto and _MILES_PUNTO.match(texto):
        texto = texto.replace(".", "")
    try:
        n = Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError("Debe ser un número.") from exc
    # `NaN`, `Infinity` y `1E+900000000` son Decimal válidos: no son números de una hoja de cálculo.
    if not n.is_finite() or abs(n.adjusted()) > 15:
        raise ValueError("Debe ser un número.")
    return n


def entero(valor: Any) -> int:
    n = numero(valor)
    if n != n.to_integral_value():
        raise ValueError("Debe ser un número entero.")
    return int(n)


def fecha(valor: Any) -> date:
    """`2026-10-28` o `28/10/2026`."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = str(valor or "").strip()
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValueError("La fecha debe ser AAAA-MM-DD o DD/MM/AAAA.")


__all__ = [
    "TOPE_BYTES",
    "TOPE_ERRORES_MOSTRADOS",
    "TOPE_FILAS",
    "ColumnaDeImportacion",
    "ContextoDeImportacion",
    "FilaDeImportacion",
    "PlantillaDeImportacion",
    "ResultadoDeImportacion",
    "entero",
    "fecha",
    "normalizar_cabecera",
    "numero",
]
