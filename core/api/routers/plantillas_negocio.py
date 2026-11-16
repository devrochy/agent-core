"""Plantillas del negocio en todas sus conexiones de WhatsApp (HU-CONN-21): la matriz plantilla × conexión y la copia.

Leer pide Conexiones en lectura; copiar, Conexiones en escritura, igual que crear una plantilla en cada proveedor
(`/admin/meta/connections/{id}/templates` y `/admin/connections/twilio/{id}/templates`). Todo dentro del negocio
activo y con RLS: una conexión de otro negocio no aparece y nunca es destino. La lógica vive en
`core.admin.plantillas_negocio_service`.

Una conexión se nombra con `{proveedor, id}`: el id de Meta es el de la cuenta (`meta_connections`) y el de Twilio
el de la conexión (`channel_connections`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from core.admin import plantillas_negocio_service as svc
from core.api.deps import Principal, require
from core.api.errors import ApiError

router = APIRouter(prefix="/admin/whatsapp-templates", tags=["connections"])


class ConexionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proveedor: Literal["meta", "twilio"]
    id: uuid.UUID


class ConexionOut(BaseModel):
    clave: str = Field(description="`proveedor:id`, la llave de las celdas.")
    proveedor: Literal["meta", "twilio"]
    id: uuid.UUID
    etiqueta: str
    activa: bool
    motivo_pausa: str | None
    sandbox: bool
    destinable: bool = Field(description="Activa y fuera del Sandbox: se puede copiar a ella.")


class CeldaOut(BaseModel):
    estado: str = Field(
        description="`aprobada`, `pendiente`, `sin_enviar`, `rechazada`, `pausada`, `falta` o `no_replicable`."
    )
    distinta: bool = Field(description="Mismo nombre e idioma con otro texto que el de la fila.")
    plantilla_id: uuid.UUID | None
    motivo: str = Field(description="Motivo del rechazo o de por qué no se puede copiar.")


class FilaOut(BaseModel):
    nombre: str
    idioma: str
    categoria: str
    cuerpo: str
    celdas: dict[str, CeldaOut] = Field(description="Por `clave` de conexión.")


class AvisoFaltanteOut(BaseModel):
    evento: str
    nombre: str
    idioma: str
    conexion: str = Field(description="`clave` de la conexión por la que el aviso no saldrá con plantilla.")
    estado: str


class MatrizOut(BaseModel):
    conexiones: list[ConexionOut]
    filas: list[FilaOut]
    avisos_faltantes: list[AvisoFaltanteOut]


class CopiarIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    origen: ConexionRef
    nombre: str = Field(min_length=1, max_length=512)
    idioma: str = Field(min_length=2, max_length=16)
    destinos: list[ConexionRef] = Field(min_length=1, max_length=20)


class CopiarTodasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origen: ConexionRef
    destino: ConexionRef


class ResultadoOut(BaseModel):
    conexion: str
    resultado: Literal["creada", "ya_existia", "distinta", "no_replicable", "error"]
    motivo: str
    nombre: str
    idioma: str


class ResumenOut(BaseModel):
    creadas: int
    ya_existian: int
    distintas: int
    no_replicables: int
    errores: int
    restantes: int = Field(
        description=f"Las que faltan por copiar (hasta {svc.LIMITE_COPIAR_TODAS} por pedido): repetir el pedido."
    )
    detalle: list[ResultadoOut]


_ERRORES = {
    404: {"description": "`not_found` o `plantilla_no_encontrada`."},
    409: {"description": "`origen_no_aprobada` o `no_es_destino` (Sandbox o conexión pausada)."},
    422: {"description": "`mismo_destino`."},
}


def _tenant(principal: Principal) -> uuid.UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


def _error(exc: svc.PlantillasNegocioError) -> ApiError:
    return ApiError(exc.http_status, exc.code, exc.message)


def _conexion_out(c: svc.Conexion) -> ConexionOut:
    return ConexionOut(
        clave=c.clave,
        proveedor=c.proveedor,  # type: ignore[arg-type]
        id=c.id,
        etiqueta=c.etiqueta,
        activa=c.activa,
        motivo_pausa=c.motivo_pausa,
        sandbox=c.sandbox,
        destinable=c.destinable,
    )


def _resultado_out(r: svc.ResultadoDestino) -> ResultadoOut:
    return ResultadoOut(**r.__dict__)


async def _auditar(principal: Principal, accion: str, datos: dict[str, str]) -> None:
    from core.shared.audit import record_audit

    await record_audit(f"admin:{principal.admin.email}", accion, input_data=datos)


@router.get("", response_model=MatrizOut)
async def matriz(
    principal: Annotated[Principal, Depends(require("connections"))],
) -> MatrizOut:
    """Cada plantilla del negocio una vez (nombre e idioma) con su estado en cada conexión de WhatsApp, y los avisos
    por evento cuya plantilla falta o no está aprobada en una conexión activa."""
    m = await svc.matriz(_tenant(principal))
    return MatrizOut(
        conexiones=[_conexion_out(c) for c in m.conexiones],
        filas=[
            FilaOut(
                nombre=f.nombre,
                idioma=f.idioma,
                categoria=f.categoria,
                cuerpo=f.cuerpo,
                celdas={k: CeldaOut(**c.__dict__) for k, c in f.celdas.items()},
            )
            for f in m.filas
        ],
        avisos_faltantes=[AvisoFaltanteOut(**a.__dict__) for a in m.avisos_faltantes],
    )


@router.post("/copy", response_model=list[ResultadoOut], responses=_ERRORES)
async def copiar(
    payload: CopiarIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> list[ResultadoOut]:
    """Copia una plantilla aprobada a otras conexiones (crea y envía a aprobación). Resultado por destino; repetirla
    no duplica."""
    try:
        resultados = await svc.copiar(
            _tenant(principal),
            origen=(payload.origen.proveedor, payload.origen.id),
            nombre=payload.nombre,
            idioma=payload.idioma,
            destinos=[(d.proveedor, d.id) for d in payload.destinos],
        )
    except svc.PlantillasNegocioError as exc:
        raise _error(exc) from None
    await _auditar(
        principal,
        "whatsapp_templates.copied",
        {
            "origen": f"{payload.origen.proveedor}:{payload.origen.id}",
            "plantilla": f"{payload.nombre}/{payload.idioma}",
            "resultados": ",".join(f"{r.conexion}={r.resultado}" for r in resultados)[:500],
        },
    )
    return [_resultado_out(r) for r in resultados]


@router.post("/copy-all", response_model=ResumenOut, responses=_ERRORES)
async def copiar_todas(
    payload: CopiarTodasIn,
    principal: Annotated[Principal, Depends(require("connections", write=True))],
) -> ResumenOut:
    """Copia a otra conexión las plantillas aprobadas del origen que le falten. Si quedan `restantes`, repetir."""
    try:
        resumen = await svc.copiar_todas(
            _tenant(principal),
            origen=(payload.origen.proveedor, payload.origen.id),
            destino=(payload.destino.proveedor, payload.destino.id),
        )
    except svc.PlantillasNegocioError as exc:
        raise _error(exc) from None
    await _auditar(
        principal,
        "whatsapp_templates.copied_all",
        {
            "origen": f"{payload.origen.proveedor}:{payload.origen.id}",
            "destino": f"{payload.destino.proveedor}:{payload.destino.id}",
            "creadas": str(resumen.creadas),
            "errores": str(resumen.errores),
        },
    )
    return ResumenOut(
        creadas=resumen.creadas,
        ya_existian=resumen.ya_existian,
        distintas=resumen.distintas,
        no_replicables=resumen.no_replicables,
        errores=resumen.errores,
        restantes=resumen.restantes,
        detalle=[_resultado_out(r) for r in resumen.detalle],
    )
