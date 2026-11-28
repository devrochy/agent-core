"""Importación de leads desde un CSV (panel › Clientes › Importar).

La lista acepta un archivo con encabezados en español o inglés (`nombre`, `telefono`, `email`, `interes`,
`estado`…). El parseo es puro y acotado; cada fila se guarda en su propio savepoint para que una fila mala no
tumble las demás. Nunca ejecuta nada del archivo ni toca otros negocios: el `tenant_id` lo pone la ruta.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from core.leads import alta
from core.leads import seguimiento as _seg
from core.shared.telefono import normalizar_telefono

MAX_FILAS = 1000
MAX_BYTES = 2_000_000
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CsvInvalido(ValueError):
    """El archivo no se puede leer como CSV de leads."""


@dataclass(frozen=True)
class FilaDeImportacion:
    fila: int
    nombre: str
    telefono: str | None
    email: str | None
    interes: str
    estado: str | None
    valor: Decimal | None
    # Texto crudo: la zona horaria del negocio se conoce al importar, no al leer el CSV.
    seguimiento_en: str | None
    seguimiento_nota: str | None
    motivo_perdida: str | None


@dataclass(frozen=True)
class ErrorDeFila:
    fila: int
    mensaje: str


@dataclass
class ResumenDeImportacion:
    total: int = 0
    creados: int = 0
    actualizados: int = 0
    errores: list[ErrorDeFila] = field(default_factory=list)

    @property
    def omitidos(self) -> int:
        return len(self.errores)


# Encabezados aceptados por campo. Se comparan sin tildes, en minúsculas y con `_` en vez de espacios.
_CAMPOS: dict[str, frozenset[str]] = {
    "nombre": frozenset({"nombre", "nombres", "name", "cliente", "razon_social"}),
    "telefono": frozenset({"telefono", "tel", "phone", "whatsapp", "celular", "movil", "mobile"}),
    "email": frozenset({"email", "correo", "mail", "e_mail"}),
    "interes": frozenset({"interes", "interest", "negocio", "producto", "servicio"}),
    "estado": frozenset({"estado", "etapa", "status", "stage"}),
    "valor": frozenset({"valor", "valor_estimado", "value", "monto", "precio"}),
    "seguimiento_en": frozenset({"seguimiento", "seguimiento_en", "proxima_accion", "next_action"}),
    "seguimiento_nota": frozenset({"nota_seguimiento", "nota_proxima_accion", "next_action_note"}),
    "motivo_perdida": frozenset({"motivo", "motivo_perdida", "loss_reason"}),
}


def _normalizar_cabecera(valor: str) -> str:
    texto = unicodedata.normalize("NFKD", valor or "").encode("ascii", "ignore").decode()
    texto = re.sub(r"[^a-zA-Z0-9]+", "_", texto).strip("_").lower()
    return texto


def _mapear(fieldnames: list[str] | None) -> dict[str, str]:
    salida: dict[str, str] = {}
    for original in fieldnames or []:
        normal = _normalizar_cabecera(original)
        for campo, alias in _CAMPOS.items():
            if campo not in salida and normal in alias:
                salida[campo] = original
    return salida


def _texto(fila: dict[str, Any], mapa: dict[str, str], campo: str) -> str:
    original = mapa.get(campo)
    if original is None:
        return ""
    return " ".join(str(fila.get(original) or "").split())


def _decimal(valor: str) -> Decimal | None:
    texto = valor.replace("$", "").replace(" ", "").strip()
    if not texto:
        return None
    def _miles(separador: str) -> bool:
        partes = texto.split(separador)
        return len(partes) > 1 and all(len(parte) == 3 for parte in partes[1:])

    # Formato colombiano (1.250.000,50) o anglosajón (1,250,000.50): el último separador manda.
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif "," in texto:
        texto = texto.replace(",", "") if _miles(",") else texto.replace(",", ".")
    elif "." in texto and _miles("."):
        texto = texto.replace(".", "")
    try:
        return Decimal(texto)
    except InvalidOperation as exc:
        raise ValueError(f"«{valor}» no es un valor estimado válido.") from exc


def _fecha(valor: str, zona: Any) -> datetime | None:
    if not valor:
        return None
    try:
        return _seg.parsear_fecha(valor, zona)
    except ValueError:
        # Un archivo exportado de Excel suele traer dd/mm/aaaa.
        for formato in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return _seg.parsear_fecha(datetime.strptime(valor, formato).isoformat(), zona)
            except ValueError:
                continue
    raise ValueError(f"«{valor}» no es una fecha de seguimiento válida.")


def leer_csv(contenido: bytes, *, max_filas: int = MAX_FILAS) -> tuple[list[FilaDeImportacion], list[ErrorDeFila]]:
    """Lee y valida el CSV. Devuelve las filas buenas y los errores con su número de línea (la 1 es el encabezado)."""
    if not contenido.strip():
        raise CsvInvalido("El archivo está vacío.")
    if len(contenido) > MAX_BYTES:
        raise CsvInvalido(f"El archivo supera {MAX_BYTES // 1_000_000} MB.")
    try:
        texto = contenido.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvInvalido("Guarda el archivo en UTF-8 (en Excel: «Guardar como» y elige CSV UTF-8).") from exc

    try:
        dialecto = csv.Sniffer().sniff(texto[:4096], delimiters=",;")
    except csv.Error:
        dialecto = csv.excel
    lector = csv.DictReader(io.StringIO(texto), dialect=dialecto)
    mapa = _mapear(lector.fieldnames)
    if "nombre" not in mapa:
        raise CsvInvalido("Falta la columna «nombre».")
    if "telefono" not in mapa and "email" not in mapa:
        raise CsvInvalido("Agrega una columna «telefono» o «email».")

    filas: list[FilaDeImportacion] = []
    errores: list[ErrorDeFila] = []
    for numero, fila in enumerate(lector, start=2):
        if len(filas) >= max_filas:
            errores.append(ErrorDeFila(numero, f"El archivo pasa de {max_filas} filas; se importan las primeras."))
            break
        try:
            nombre = _texto(fila, mapa, "nombre")
            if not nombre:
                raise ValueError("Falta el nombre.")
            crudo_telefono = _texto(fila, mapa, "telefono")
            telefono = normalizar_telefono(crudo_telefono) if crudo_telefono else None
            if crudo_telefono and not telefono:
                raise ValueError("El teléfono no tiene dígitos.")
            email = _texto(fila, mapa, "email").lower() or None
            if email and not _EMAIL.match(email):
                raise ValueError("El correo no es válido.")
            if not telefono and not email:
                raise ValueError("Falta el teléfono o el correo.")
            estado = _texto(fila, mapa, "estado").lower() or None
            motivo = _texto(fila, mapa, "motivo_perdida") or None
            if estado == "perdido" and not motivo:
                raise ValueError("Un lead perdido necesita «motivo».")
            valor = _decimal(_texto(fila, mapa, "valor"))
            seguimiento_nota = _texto(fila, mapa, "seguimiento_nota") or None
            if seguimiento_nota and len(seguimiento_nota) > 200:
                raise ValueError("La nota de seguimiento admite hasta 200 caracteres.")
            filas.append(
                FilaDeImportacion(
                    fila=numero,
                    nombre=nombre,
                    telefono=telefono,
                    email=email,
                    interes=_texto(fila, mapa, "interes"),
                    estado=estado,
                    valor=valor,
                    seguimiento_en=_texto(fila, mapa, "seguimiento_en") or None,
                    seguimiento_nota=seguimiento_nota,
                    motivo_perdida=motivo,
                )
            )
        except ValueError as exc:
            errores.append(ErrorDeFila(numero, str(exc)))
    return filas, errores


async def importar_csv(
    session: Any, tenant_id: uuid.UUID, contenido: bytes, *, source: str = "import"
) -> ResumenDeImportacion:
    """Importa el CSV en la sesión del llamador. Cada fila va en su propio savepoint."""
    filas, errores = leer_csv(contenido)
    zona = await _seg.zona_del_negocio(session, tenant_id)
    resumen = ResumenDeImportacion(total=len(filas) + len(errores), errores=list(errores))
    for fila in filas:
        try:
            fecha = _fecha(fila.seguimiento_en or "", zona)
            async with session.begin_nested():
                _, _, lead_creado, _ = await alta.crear_desde_panel(
                    session,
                    tenant_id,
                    nombre=fila.nombre,
                    telefono=fila.telefono,
                    email=fila.email,
                    interes=fila.interes,
                    estado=fila.estado,
                    valor=fila.valor,
                    seguimiento_en=fecha,
                    seguimiento_nota=fila.seguimiento_nota,
                    motivo_perdida=fila.motivo_perdida,
                    source=source,
                )
            if lead_creado:
                resumen.creados += 1
            else:
                resumen.actualizados += 1
        except Exception as exc:  # una fila mala no tumba las demás
            resumen.errores.append(ErrorDeFila(fila.fila, str(exc) or "No se pudo importar."))
    return resumen
