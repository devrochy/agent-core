"""Mapeo de columnas de un CSV a la plantilla de importación del agente (ADR-0194 §2).

El dueño sube su hoja tal como la tiene: con sus cabeceras («Código», «Valor», «Existencias»). El core
propone a qué columna de la plantilla va cada cabecera y devuelve las filas ya normalizadas para que el
cliente las muestre, las edite y las reenvíe en JSON (`importar_datos.importar_filas`). No guarda nada.

1. **Heurística:** la cabecera normalizada es el nombre de la columna o uno de sus sinónimos
   (`PlantillaDeImportacion.sinonimos`, los pone el pack).
2. **IA, solo si hace falta:** si quedan cabeceras sin reconocer **y** columnas de la plantilla sin
   asignar, una sola llamada con las cabeceras, unas filas de muestra y las columnas libres. Las
   instrucciones están en el playbook del kernel `mapear_columnas`; el dominio llega como datos.
   La salida es dato no confiable: solo se aceptan cabeceras del archivo y columnas libres de la
   plantilla, cada una una vez; lo demás se ignora. Si la IA falla, esas cabeceras quedan sin asignar.
3. La llamada se mide en `usage_ledger` (`kind='generacion'`, `result.proposito='mapeo_columnas'`) con la
   llave del negocio si la configuró (helper de `core.ia.estructurado`).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from core.qa.contrato_importacion import PlantillaDeImportacion, normalizar_cabecera
from core.qa.importar_datos import (
    Tabla,
    _destino_o_error,
    _evaluar,
    _info_plantilla,
    _reporte,
    archivo_de_filas,
    leer_tabla,
)
from core.shared.logger import get_logger

log = get_logger(__name__)

PLAYBOOK = "mapear_columnas"
FILAS_DE_MUESTRA = 5
MAX_CELDA_MUESTRA = 80
TIMEOUT_LLM_S = 45
MAX_TOKENS = 800
TEMPERATURA = 0.0


class _Asignacion(BaseModel):
    cabecera: str
    columna: str | None = None


class _RespuestaIA(BaseModel):
    asignaciones: list[_Asignacion] = Field(default_factory=list, max_length=200)


@dataclass(frozen=True)
class Asignacion:
    cabecera: str  # tal como viene en el archivo
    columna: str | None  # columna de la plantilla, o None (se ignora)
    origen: str  # heuristica | ia | ninguno


def _indice_de_nombres(plantilla: PlantillaDeImportacion) -> dict[str, str]:
    """Cabecera normalizada → columna de la plantilla (nombre propio primero, luego sinónimos)."""
    indice: dict[str, str] = {}
    for columna in plantilla.nombres:
        for sinonimo in plantilla.sinonimos.get(columna, ()):
            indice.setdefault(normalizar_cabecera(sinonimo), columna)
    for columna in plantilla.nombres:
        indice[normalizar_cabecera(columna)] = columna
    return indice


def mapeo_heuristico(cabeceras: list[str], plantilla: PlantillaDeImportacion) -> list[Asignacion]:
    """A cada cabecera, la columna que nombra (o un sinónimo). Una columna se asigna una sola vez: gana
    la primera cabecera del archivo que la nombra."""
    indice = _indice_de_nombres(plantilla)
    usadas: set[str] = set()
    salida: list[Asignacion] = []
    for cabecera in cabeceras:
        columna = indice.get(normalizar_cabecera(cabecera))
        if columna is not None and columna not in usadas:
            usadas.add(columna)
            salida.append(Asignacion(cabecera, columna, "heuristica"))
        else:
            salida.append(Asignacion(cabecera, None, "ninguno"))
    return salida


def _entrada_ia(
    plantilla: PlantillaDeImportacion, tabla: Tabla, pendientes: list[int], libres: list[str]
) -> str:
    muestra = [
        {tabla.cabeceras[i]: (registro[i] if i < len(registro) else "")[:MAX_CELDA_MUESTRA] for i in pendientes}
        for _, registro in tabla.registros[:FILAS_DE_MUESTRA]
    ]
    descripcion = {c.nombre: c.descripcion for c in plantilla.columnas}
    return json.dumps(
        {
            "plantilla": plantilla.titulo,
            "columnas_libres": [
                {"nombre": c, "descripcion": descripcion.get(c, ""), "obligatoria": c in plantilla.obligatorias}
                for c in libres
            ],
            "cabeceras_sin_reconocer": [tabla.cabeceras[i] for i in pendientes],
            "muestra": muestra,
        },
        ensure_ascii=False,
    )


def _proveedor() -> Any:
    """Proveedor de texto; `None` = el del negocio (`resolver_llm`). Punto de inyección de los tests."""
    return None


async def _con_ia(
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    plantilla: PlantillaDeImportacion,
    tabla: Tabla,
    mapeo: list[Asignacion],
) -> tuple[list[Asignacion], str | None]:
    """Completa el mapeo con la IA. Devuelve el mapeo y el motivo si la IA no ayudó (`proveedor`/`formato`)."""
    from core.ia.estructurado import ErrorIA, generar_estructurado

    usadas = {a.columna for a in mapeo if a.columna}
    libres = [c for c in plantilla.nombres if c not in usadas]
    pendientes = [i for i, a in enumerate(mapeo) if a.columna is None and a.cabecera]
    if not libres or not pendientes:
        return mapeo, None
    proveedor = _proveedor()
    try:
        respuesta = await generar_estructurado(
            tenant_id,
            playbook=PLAYBOOK,
            esquema=_RespuestaIA,
            entrada=_entrada_ia(plantilla, tabla, pendientes, libres),
            kind="generacion",
            result={"proposito": "mapeo_columnas", "agent_id": str(agent_id), "plantilla": plantilla.id},
            proveedor=(lambda: proveedor) if proveedor is not None else None,
            max_tokens=MAX_TOKENS,
            temperatura=TEMPERATURA,
            timeout_s=TIMEOUT_LLM_S,
        )
    except ErrorIA as exc:
        log.warning("mapeo_columnas_ia_fallo", motivo=exc.motivo, error=exc.causa, plantilla=plantilla.id)
        return mapeo, exc.motivo
    # La salida es dato no confiable: solo cabeceras pendientes y columnas libres, cada una una vez.
    por_cabecera = {mapeo[i].cabecera: i for i in pendientes}
    libres_restantes = set(libres)
    nuevo = list(mapeo)
    for asignacion in respuesta.datos.asignaciones:
        i = por_cabecera.pop(asignacion.cabecera, None)
        if i is None or asignacion.columna not in libres_restantes:
            continue
        libres_restantes.discard(asignacion.columna)
        nuevo[i] = Asignacion(mapeo[i].cabecera, asignacion.columna, "ia")
    return nuevo, None


def aplicar(tabla: Tabla, mapeo: list[Asignacion]) -> list[tuple[int, dict[str, str | None]]]:
    """Las filas del archivo como `columna → texto`, con su número de fila."""
    filas: list[tuple[int, dict[str, str | None]]] = []
    for numero, registro in tabla.registros:
        valores: dict[str, str | None] = {}
        for i, asignacion in enumerate(mapeo):
            if asignacion.columna is not None:
                valores[asignacion.columna] = registro[i] if i < len(registro) else ""
        filas.append((numero, valores))
    return filas


async def mapear(tenant_id: uuid.UUID, agent_id: uuid.UUID, contenido: bytes) -> dict[str, Any]:
    """Propuesta de mapeo, filas normalizadas y su vista previa. No escribe nada (salvo el ledger)."""
    from core.qa.datos_generados import ErrorDatosDePrueba

    d = await _destino_o_error(tenant_id, agent_id)
    tabla = leer_tabla(contenido)
    if not tabla.registros:
        raise ErrorDatosDePrueba(422, "archivo_vacio", "El archivo no tiene filas con datos.")
    mapeo, motivo_ia = await _con_ia(
        tenant_id, agent_id, d.plantilla, tabla, mapeo_heuristico(tabla.cabeceras, d.plantilla)
    )
    filas = aplicar(tabla, mapeo)
    archivo = archivo_de_filas(filas, d.plantilla)
    reporte = _reporte(await _evaluar(tenant_id, d, archivo, simular=True))
    asignadas = {a.columna for a in mapeo if a.columna}
    return {
        "plantilla": _info_plantilla(d.plantilla),
        "mapeo": [{"cabecera": a.cabecera, "columna": a.columna, "origen": a.origen} for a in mapeo],
        "columnas_faltantes": [c for c in d.plantilla.obligatorias if c not in asignadas],
        "ia": {
            "usada": any(a.origen == "ia" for a in mapeo),
            "motivo_fallo": motivo_ia,
        },
        "filas": [{"fila": numero, "valores": valores} for numero, valores in filas],
        "vista_previa": reporte,
    }


__all__ = ["Asignacion", "aplicar", "mapear", "mapeo_heuristico"]
