"""Importar datos existentes por agente desde un CSV (HU-QA-12, ADR-0180).

Solo en test (`QA_TESTERS_ENABLED`; la API responde 404 sin la bandera). Por agente:

1. La plantilla sale de la forma de datos del agente (la misma de «Generar datos de prueba»):
   ventas → `productos`, agenda → `tipos_de_cita`, cobranza → `clientes_y_prestamos`,
   suscripciones → `planes`.
2. El archivo es dato no confiable: tope de tamaño y de filas, decodificación UTF-8 o Windows-1252,
   separador `,` o `;`, cabeceras normalizadas, validación fila por fila con el esquema de la
   plantilla y claves repetidas dentro del archivo. Nunca se importan teléfono ni correo.
3. La vista previa (`previsualizar`) no escribe nada; confirmar vuelve a validar el mismo archivo
   (sin estado en el servidor) y le pide al pack que cree solo las filas nuevas: una clave que ya
   existe se reporta como «ya existía» y no se toca (idempotente).
4. Cada importación es un lote de `test_data_records` con `origen = 'importacion'`. «Generar datos
   de prueba» y su borrado no lo tocan; `borrar` retira solo lo importado.
5. Una fila de `audit_log` por importación y por borrado (sin el contenido del archivo).
"""

from __future__ import annotations

import csv
import io
import uuid
from collections import Counter
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from core.db.models import AuditLog, TestDataRecord, utcnow
from core.db.session import get_session
from core.mcp.types import BaseModule
from core.qa.contrato_datos import FormaDeDatos, RegistroSembrado
from core.qa.contrato_importacion import (
    TOPE_BYTES,
    TOPE_ERRORES_MOSTRADOS,
    TOPE_FILAS,
    ContextoDeImportacion,
    PlantillaDeImportacion,
    ResultadoDeImportacion,
    normalizar_cabecera,
)
from core.qa.datos_generados import (
    MODULOS,
    Eleccion,
    ErrorDatosDePrueba,
    Modulo,
    _agente,
    _con_lock,
    _eleccion,
    _modulo_de_forma,
    _negocio,
    _retirar_registros,
)
from core.qa.datos_generados import (
    ORIGEN_IMPORTACION as ORIGEN,
)
from core.shared.logger import get_logger

log = get_logger(__name__)

ACCION_AUDITORIA = "qa_importar_datos"


@dataclass(frozen=True)
class Destino:
    """A dónde va el archivo de un agente: su plantilla, el pack que escribe y el módulo (sección)."""

    pack_id: str
    pack: BaseModule
    forma: FormaDeDatos
    plantilla: PlantillaDeImportacion
    modulo: Modulo


@dataclass
class Archivo:
    """El CSV ya leído: filas como diccionarios `columna → texto`, con su número de fila en el archivo."""

    filas: list[tuple[int, dict[str, str]]]
    ignoradas: list[str]
    faltantes: list[str]


# ------------------------------------------------------------------ destino
async def destino(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Destino | str:
    """El destino de la importación del agente, o el motivo por el que no tiene."""
    agente = await _agente(tenant_id, agent_id)
    eleccion = await _eleccion(tenant_id, agente)
    if not isinstance(eleccion, Eleccion):
        return eleccion
    plantilla = next(
        (p for p in eleccion.modulo.plantillas_de_importacion() if p.modo == eleccion.forma.modo),
        None,
    )
    if plantilla is None:
        return "Este agente todavía no tiene plantilla para importar datos."
    modulo_id = _modulo_de_forma(eleccion.pack_id, eleccion.forma.modo)
    modulo = MODULOS.get(modulo_id or "")
    if modulo is None:
        return "Este agente todavía no tiene plantilla para importar datos."
    return Destino(eleccion.pack_id, eleccion.modulo, eleccion.forma, plantilla, modulo)


async def _destino_o_error(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> Destino:
    encontrado = await destino(tenant_id, agent_id)
    if isinstance(encontrado, str):
        raise ErrorDatosDePrueba(409, "sin_plantilla", encontrado)
    return encontrado


def _info_plantilla(p: PlantillaDeImportacion) -> dict[str, Any]:
    return {
        "id": p.id,
        "titulo": p.titulo,
        "ayuda": p.ayuda,
        "columna_clave": p.columna_clave,
        "columnas": [
            {"nombre": c.nombre, "descripcion": c.descripcion, "obligatoria": c.obligatoria} for c in p.columnas
        ],
        "tope_filas": TOPE_FILAS,
        "tope_bytes": TOPE_BYTES,
    }


async def _registros_importados(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> list[TestDataRecord]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(TestDataRecord)
                    .where(
                        TestDataRecord.tenant_id == tenant_id,
                        TestDataRecord.agent_id == agent_id,
                        TestDataRecord.origen == ORIGEN,
                        TestDataRecord.deleted_at.is_(None),
                    )
                    .order_by(TestDataRecord.created_at)
                )
            ).scalars()
        )


def _lote(registros: list[TestDataRecord]) -> dict[str, Any] | None:
    if not registros:
        return None
    return {
        "lotes": len({r.lote_id for r in registros}),
        "creado_en": registros[-1].created_at,
        "conteos": dict(Counter(r.kind for r in registros)),
    }


async def estado(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> dict[str, Any]:
    encontrado = await destino(tenant_id, agent_id)
    importado = _lote(await _registros_importados(tenant_id, agent_id))
    if isinstance(encontrado, str):
        return {"disponible": False, "motivo": encontrado, "plantilla": None, "importado": importado}
    return {
        "disponible": True,
        "motivo": None,
        "plantilla": _info_plantilla(encontrado.plantilla),
        "importado": importado,
    }


# ------------------------------------------------------------------ plantilla descargable
def plantilla_csv(plantilla: PlantillaDeImportacion) -> bytes:
    """Cabeceras y filas de ejemplo; UTF-8 con BOM para que Excel respete las tildes."""
    salida = io.StringIO()
    escritor = csv.writer(salida, lineterminator="\r\n")
    escritor.writerow(plantilla.nombres)
    for ejemplo in plantilla.ejemplos:
        escritor.writerow([ejemplo.get(nombre, "") for nombre in plantilla.nombres])
    return salida.getvalue().encode("utf-8-sig")


async def plantilla_del_agente(tenant_id: uuid.UUID, agent_id: uuid.UUID) -> tuple[str, bytes]:
    d = await _destino_o_error(tenant_id, agent_id)
    return f"plantilla-{d.plantilla.id}.csv", plantilla_csv(d.plantilla)


# ------------------------------------------------------------------ lectura del archivo
def _decodificar(contenido: bytes) -> str:
    try:
        return contenido.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contenido.decode("cp1252", errors="replace")  # «CSV (delimitado por comas)» de Excel


def leer_archivo(contenido: bytes, plantilla: PlantillaDeImportacion) -> Archivo:
    if len(contenido) > TOPE_BYTES:
        raise ErrorDatosDePrueba(
            413, "archivo_muy_grande", f"El archivo pesa más de {TOPE_BYTES // 1_000_000} MB. Divídelo en partes."
        )
    texto = _decodificar(contenido).replace("\x00", "")
    primera = next((linea for linea in texto.splitlines() if linea.strip()), "")
    if not primera:
        raise ErrorDatosDePrueba(422, "archivo_vacio", "El archivo está vacío.")
    separador = max((";", ",", "\t"), key=primera.count)
    lector = csv.reader(io.StringIO(texto), delimiter=separador)
    try:
        cabeceras = [normalizar_cabecera(c) for c in next(lector)]
    except (StopIteration, csv.Error) as exc:
        raise ErrorDatosDePrueba(422, "archivo_vacio", "El archivo está vacío.") from exc
    conocidas = set(plantilla.nombres)
    ignoradas = [c for c in cabeceras if c and c not in conocidas]
    faltantes = [n for n in plantilla.obligatorias if n not in cabeceras]
    filas: list[tuple[int, dict[str, str]]] = []
    try:
        for numero, registro in enumerate(lector, start=2):
            if not any(celda.strip() for celda in registro):
                continue
            filas.append((numero, {c: v.strip() for c, v in zip(cabeceras, registro, strict=False) if c in conocidas}))
            if len(filas) > TOPE_FILAS:
                raise ErrorDatosDePrueba(
                    422,
                    "demasiadas_filas",
                    f"El archivo tiene más de {TOPE_FILAS} filas. Divídelo en archivos más pequeños.",
                )
    except csv.Error as exc:
        raise ErrorDatosDePrueba(422, "archivo_invalido", "No se pudo leer el archivo como CSV.") from exc
    if not filas and not faltantes:
        raise ErrorDatosDePrueba(422, "archivo_vacio", "El archivo no tiene filas con datos.")
    return Archivo(filas=filas, ignoradas=ignoradas, faltantes=faltantes)


# ------------------------------------------------------------------ validación por fila
_MENSAJES = {
    "missing": "Falta este valor.",
    "string_too_short": "Es muy corto o está vacío.",
    "string_too_long": "Es muy largo.",
    "int_parsing": "Debe ser un número entero.",
    "decimal_parsing": "Debe ser un número.",
    "greater_than": "Debe ser mayor que cero.",
    "greater_than_equal": "Es menor que el mínimo permitido.",
    "less_than_equal": "Es mayor que el máximo permitido.",
    "string_pattern_mismatch": "Tiene un formato no válido.",
}


def _mensaje(error: Any) -> str:
    if error["type"] == "value_error":
        return str(error["msg"]).removeprefix("Value error, ")
    if error["type"] == "literal_error":
        permitidos = ", ".join(str(v) for v in error.get("ctx", {}).get("expected", "").split(" or "))
        return f"Valor no válido; use {permitidos.replace(chr(39), '')}."
    return _MENSAJES.get(error["type"], str(error["msg"]))


@dataclass
class Fila:
    """Una fila del archivo ya validada (o con sus errores)."""

    numero: int
    datos: BaseModel | None
    errores: list[tuple[str | None, str]]


def validar(archivo: Archivo, plantilla: PlantillaDeImportacion) -> list[Fila]:
    """Valida cada fila con el esquema de la plantilla y marca las claves repetidas del archivo."""
    filas: list[Fila] = []
    vistas: dict[str, int] = {}
    for numero, valores in archivo.filas:
        crudo = {k: (v if v != "" else None) for k, v in valores.items()}
        try:
            datos = plantilla.esquema.model_validate(crudo)
        except ValidationError as exc:
            filas.append(
                Fila(
                    numero,
                    None,
                    [(str(e["loc"][0]) if e["loc"] else None, _mensaje(e)) for e in exc.errors()],
                )
            )
            continue
        clave = plantilla.clave(datos).strip().casefold()
        if clave in vistas:
            filas.append(
                Fila(
                    numero,
                    None,
                    [(plantilla.columna_clave, f"Repetido: ya aparece en la fila {vistas[clave]} del archivo.")],
                )
            )
            continue
        vistas[clave] = numero
        filas.append(Fila(numero, datos, []))
    return filas


async def _contexto(tenant_id: uuid.UUID) -> ContextoDeImportacion:
    negocio = await _negocio(tenant_id)
    return ContextoDeImportacion(moneda=negocio.moneda, zona_horaria=negocio.zona_horaria)


@dataclass
class Evaluacion:
    """Lo que dio el archivo: sus filas, el estado de cada una y lo que creó (o crearía) el pack."""

    archivo: Archivo
    filas: list[Fila]
    nuevas: int
    existentes: int
    creados: list[RegistroSembrado]

    @property
    def errores(self) -> list[dict[str, Any]]:
        salida = [
            {"fila": f.numero, "columna": columna, "mensaje": mensaje} for f in self.filas for columna, mensaje in f.errores
        ]
        salida.sort(key=lambda e: e["fila"])
        return salida

    @property
    def filas_con_errores(self) -> int:
        return sum(1 for f in self.filas if f.errores)


async def _evaluar(tenant_id: uuid.UUID, d: Destino, contenido: bytes, *, simular: bool) -> Evaluacion:
    """Lee y valida el archivo y le pide al pack lo que haría (o hace) con las filas válidas. Los errores
    que el pack encuentra (p. ej. un SKU de un producto eliminado) se suman a los de cada fila."""
    archivo = leer_archivo(contenido, d.plantilla)
    filas = validar(archivo, d.plantilla)
    validas = [f for f in filas if f.datos is not None]
    resultado = ResultadoDeImportacion()
    if validas and not archivo.faltantes:
        resultado = await d.pack.importar_filas(
            tenant_id,
            d.plantilla.id,
            [f.datos for f in validas if f.datos is not None],
            await _contexto(tenant_id),
            simular=simular,
        )
        for i, error in resultado.errores.items():
            validas[i].errores.append(error)
    return Evaluacion(
        archivo=archivo,
        filas=filas,
        nuevas=len(resultado.nuevas),
        existentes=len(resultado.existentes),
        creados=resultado.creados,
    )


def _reporte(e: Evaluacion) -> dict[str, Any]:
    errores = e.errores
    return {
        "total_filas": len(e.filas),
        "nuevas": e.nuevas,
        "existentes": e.existentes,
        "con_errores": e.filas_con_errores,
        "errores": errores[:TOPE_ERRORES_MOSTRADOS],
        "errores_total": len(errores),
        "columnas_ignoradas": e.archivo.ignoradas,
        "columnas_faltantes": e.archivo.faltantes,
    }


async def previsualizar(tenant_id: uuid.UUID, agent_id: uuid.UUID, contenido: bytes) -> dict[str, Any]:
    """Vista previa: errores por fila y cuántas filas se crearían o ya existen. No escribe nada."""
    d = await _destino_o_error(tenant_id, agent_id)
    return _reporte(await _evaluar(tenant_id, d, contenido, simular=True))


# ------------------------------------------------------------------ importar y borrar
async def _auditar(tenant_id: uuid.UUID, actor: str, detalle: dict[str, Any]) -> None:
    async with get_session() as session:
        session.add(AuditLog(tenant_id=tenant_id, actor=actor[:120], action=ACCION_AUDITORIA, input=detalle))


async def importar(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    contenido: bytes,
    *,
    solo_validas: bool,
    actor: str,
) -> dict[str, Any]:
    d = await _destino_o_error(tenant_id, agent_id)

    async def trabajo() -> dict[str, Any]:
        previa = await _evaluar(tenant_id, d, contenido, simular=True)
        reporte = _reporte(previa)
        if previa.archivo.faltantes:
            raise ErrorDatosDePrueba(
                422,
                "columnas_faltantes",
                "Faltan columnas obligatorias: " + ", ".join(previa.archivo.faltantes) + ".",
            )
        if reporte["errores_total"] and not solo_validas:
            raise ErrorDatosDePrueba(
                422,
                "archivo_con_errores",
                "El archivo tiene filas con errores. Corrígelas o importa solo las válidas.",
            )
        hecha = await _evaluar(tenant_id, d, contenido, simular=False)
        lote_id = uuid.uuid4()
        ahora = utcnow()
        creados = hecha.creados
        async with get_session() as session:
            session.add_all(
                TestDataRecord(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    modulo=d.modulo.id,
                    lote_id=lote_id,
                    pack_id=d.pack_id,
                    modo=d.forma.modo,
                    kind=r.kind,
                    record_ref=r.ref,
                    origen=ORIGEN,
                    created_at=ahora,
                )
                for r in creados
            )
        conteos = dict(Counter(r.kind for r in creados))
        salida = {
            "lote_id": lote_id,
            "plantilla": d.plantilla.id,
            "creado_en": ahora,
            "creadas": hecha.nuevas,
            "existentes": hecha.existentes,
            "con_errores": reporte["con_errores"],
            "conteos": conteos,
        }
        await _auditar(
            tenant_id,
            actor,
            {
                "accion": "importar",
                "agent_id": str(agent_id),
                "plantilla": d.plantilla.id,
                "lote_id": str(lote_id),
                "filas": reporte["total_filas"],
                "creadas": salida["creadas"],
                "existentes": salida["existentes"],
                "con_errores": reporte["con_errores"],
                "conteos": conteos,
            },
        )
        log.info(
            "datos_importados",
            tenant_id=str(tenant_id),
            agent_id=str(agent_id),
            plantilla=d.plantilla.id,
            filas=reporte["total_filas"],
            creadas=salida["creadas"],
            existentes=salida["existentes"],
            con_errores=reporte["con_errores"],
        )
        return salida

    return await _con_lock(
        tenant_id, f"importar:{agent_id}", trabajo, "Ya se están importando datos para este agente."
    )


async def borrar(tenant_id: uuid.UUID, agent_id: uuid.UUID, *, actor: str) -> dict[str, Any]:
    await _agente(tenant_id, agent_id)

    async def trabajo() -> dict[str, Any]:
        registros = await _registros_importados(tenant_id, agent_id)
        retirados = await _retirar_registros(tenant_id, registros)
        await _auditar(
            tenant_id,
            actor,
            {
                "accion": "borrar",
                "agent_id": str(agent_id),
                "lotes": sorted({str(r.lote_id) for r in registros}),
                "retirados": retirados,
            },
        )
        log.info("datos_importados_borrados", tenant_id=str(tenant_id), agent_id=str(agent_id), retirados=retirados)
        return {"retirados": retirados}

    return await _con_lock(
        tenant_id, f"importar:{agent_id}", trabajo, "Ya se están importando datos para este agente."
    )


__all__ = ["ErrorDatosDePrueba", "borrar", "estado", "importar", "plantilla_del_agente", "previsualizar"]
