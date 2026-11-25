"""Datos del cliente por agente (HU-CRM-28, enmienda de ADR-0179).

`GET /api/v1/admin/agents/{id}/datos-cliente` lista las operaciones que ESE agente puede ejecutar,
con el nivel efectivo de cada dato (requerido, recomendado, no pedir), el que trae el módulo por
defecto y los datos obligatorios de sus superpoderes (solo lectura). `PUT` guarda en
`agents.data_policy` solo lo que difiere del módulo. Requiere la sección `settings`; cada cambio
deja una fila de auditoría con el antes y el después.

El aviso de tratamiento de datos sigue siendo del negocio (`/admin/datos-cliente/aviso`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.agents.editor import habilitados_del_negocio
from core.agents.habilidades_del_agente import packs_del_agente
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.datos_cliente import AccionDatos, Nivel, niveles_efectivos, registro_datos
from core.datos_cliente.ajustes import (
    METODOS_DE_ENTREGA,
    con_herramientas,
    con_metodos_entrega,
    con_politica,
    herramientas_de,
    metodos_entrega_de,
    politica_de,
)
from core.datos_cliente.servicio import base_de
from core.db.models import AgentRecord, AppSettings, AuditLog
from core.db.session import get_session

router = APIRouter(prefix="/admin/datos-cliente", tags=["admin · datos del cliente"])
router_agente = APIRouter(prefix="/admin/agents", tags=["admin · datos del cliente"])

NivelOut = Literal["requerido", "recomendado", "no_pedir"]
MetodoEntregaOut = Literal["domicilio", "recoger_en_tienda"]


class CampoPoliticaOut(BaseModel):
    clave: str
    etiqueta: str
    nivel: NivelOut
    por_defecto: NivelOut
    ajustable: bool
    ajustado: bool


class AccionPoliticaOut(BaseModel):
    id: str
    modulo: str
    etiqueta: str
    campos: list[CampoPoliticaOut]


class ProcedimientoDatoOut(BaseModel):
    clave: str
    etiqueta: str
    obligatorio: bool = True
    condicion: str | None = None
    tipo: str | None = None


class ProcedimientoDatosOut(BaseModel):
    id: str
    titulo: str
    datos: list[ProcedimientoDatoOut]


class AgenteDatosOut(BaseModel):
    agente_id: str
    niveles: list[NivelOut]
    acciones: list[AccionPoliticaOut]
    # Datos que exigen los superpoderes del agente: solo lectura, no se ajustan aquí.
    procedimientos: list[ProcedimientoDatosOut] = []
    herramientas_del_agente: bool = False
    metodos_entrega: list[MetodoEntregaOut] = Field(default_factory=lambda: list(METODOS_DE_ENTREGA))


class AgenteDatosUpdate(BaseModel):
    """`{accion_id: {clave: nivel}}`. Lo que no viene para una acción vuelve al valor del módulo.
    Sin `politica` (None) no se tocan los ajustes: sirve para cambiar solo la bandera."""

    politica: dict[str, dict[str, NivelOut]] | None = Field(default=None, max_length=50)
    herramientas_del_agente: bool | None = None
    metodos_entrega: list[MetodoEntregaOut] | None = Field(default=None, max_length=2)


def _exigir_negocio(principal: Principal) -> None:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")


_ETIQUETAS = {
    "name": "Nombre completo",
    "full_name": "Nombre completo",
    "phone": "Teléfono",
    "email": "Correo",
    "address": "Dirección",
    "municipality": "Municipio o ciudad",
    "municipio": "Municipio o ciudad",
    "delivery_method": "Método de entrega",
    "payment_method": "Método de pago",
    "id_document": "Documento",
}


def _etiqueta(clave: str) -> str:
    if clave in _ETIQUETAS:
        return _ETIQUETAS[clave]
    limpio = clave.replace("_", " ").strip()
    return limpio[0].upper() + limpio[1:] if limpio else clave


async def _agente(principal: Principal, agent_id: uuid.UUID) -> AgentRecord:
    _exigir_negocio(principal)
    async with get_session() as session:
        fila = (
            await session.execute(
                select(AgentRecord).where(
                    AgentRecord.tenant_id == principal.tenant_id,
                    AgentRecord.id == agent_id,
                    AgentRecord.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if fila is None:
        raise ApiError.not_found("Agente no encontrado.")
    if fila.kind != "specialist":
        raise ApiError(
            422,
            "agente_no_especialista",
            "Solo un agente especialista tiene datos del cliente.",
            fields={"agente_id": "Solo un agente especialista tiene datos del cliente."},
        )
    return fila


async def _contenido(principal: Principal, agent_id: uuid.UUID) -> dict:
    """El borrador del agente; si no tiene, la última versión publicada; si tampoco, vacío."""
    from core.agents import versiones

    try:
        borrador = await versiones.obtener_borrador(principal.tenant_id, agent_id)
        return borrador.content if isinstance(borrador.content, dict) else {}
    except Exception:
        versiones_lista = await versiones.listar_versiones(principal.tenant_id, agent_id)
        if versiones_lista and isinstance(versiones_lista[0].content, dict):
            return versiones_lista[0].content
        return {}


def _procedimientos_del_agente(contenido: dict, packs: set[str]) -> dict[str, object]:
    """Procedimientos declarados por el agente, resueltos con la versión más alta ofrecible."""
    from core.kernel.procedures.de_pack import cargar
    from core.releases.procedimientos import ofrecible, parsear_version

    por_id: dict[str, object] = {}
    for pack_id in sorted(packs):
        try:
            procedimientos = cargar(pack_id)
        except Exception:
            continue
        for proc in procedimientos:
            if not ofrecible(proc, packs):
                continue
            actual = por_id.get(proc.id)
            if actual is None or parsear_version(proc.version) > parsear_version(actual.version):
                por_id[proc.id] = proc
    refs = [
        str(p.get("id"))
        for p in contenido.get("procedures") or []
        if isinstance(p, dict) and p.get("id")
    ]
    return {pid: por_id[pid] for pid in refs if pid in por_id}


async def _acciones_del_agente(
    principal: Principal, agente: AgentRecord
) -> tuple[list[AccionDatos], set[str], dict]:
    """Acciones de datos que el agente puede ejecutar, sus packs y el borrador que las declara."""
    contenido = await _contenido(principal, agente.id)
    packs = await packs_del_agente(
        principal.tenant_id, agente.id, await habilitados_del_negocio(principal.tenant_id)
    )
    vinculadas = {
        str(b.get("tool"))
        for b in contenido.get("tools") or []
        if isinstance(b, dict) and b.get("tool") and b.get("enabled", True)
    }
    for proc in _procedimientos_del_agente(contenido, packs).values():
        for stage in proc.stages:  # type: ignore[attr-defined]
            vinculadas.update(stage.actions)
    datos = [a for a in registro_datos.de_packs(packs) if a.aplica_a(vinculadas)]
    return datos, packs, contenido


def _procedimientos(contenido: dict, packs: set[str]) -> list[ProcedimientoDatosOut]:
    """Datos obligatorios de los superpoderes del agente, en solo lectura."""
    salida: list[ProcedimientoDatosOut] = []
    for proc in _procedimientos_del_agente(contenido, packs).values():
        datos: list[ProcedimientoDatoOut] = []
        for nombre, slot in (proc.slots or {}).items():  # type: ignore[attr-defined]
            if slot.setBy is not None or not (slot.required or slot.requiredWhen is not None):
                continue
            condicion = None
            if slot.requiredWhen is not None:
                condicion = f"{slot.requiredWhen.slot} = {slot.requiredWhen.equals}"
            datos.append(
                ProcedimientoDatoOut(
                    clave=nombre,
                    etiqueta=_etiqueta(nombre),
                    obligatorio=bool(slot.required),
                    condicion=condicion,
                    tipo=slot.type,
                )
            )
        if datos:
            salida.append(ProcedimientoDatosOut(id=proc.id, titulo=proc.title, datos=datos))  # type: ignore[attr-defined]
    return salida


async def _leer(principal: Principal, agente: AgentRecord) -> AgenteDatosOut:
    acciones, packs, contenido = await _acciones_del_agente(principal, agente)
    ajustes = politica_de(agente.data_policy)
    salida: list[AccionPoliticaOut] = []
    for accion in acciones:
        base = await base_de(accion)
        defecto = niveles_efectivos(accion, None, base)
        efectivo = niveles_efectivos(accion, ajustes.get(accion.id), base)
        salida.append(
            AccionPoliticaOut(
                id=accion.id,
                modulo=accion.modulo,
                etiqueta=accion.etiqueta,
                campos=[
                    CampoPoliticaOut(
                        clave=c.clave,
                        etiqueta=c.etiqueta,
                        nivel=efectivo[c.clave].value,
                        por_defecto=defecto[c.clave].value,
                        ajustable=c.ajustable,
                        ajustado=efectivo[c.clave] is not defecto[c.clave],
                    )
                    for c in accion.campos
                ],
            )
        )
    return AgenteDatosOut(
        agente_id=str(agente.id),
        niveles=[n.value for n in Nivel],
        acciones=salida,
        procedimientos=_procedimientos(contenido, packs),
        herramientas_del_agente=herramientas_de(agente.data_policy),
        metodos_entrega=metodos_entrega_de(agente.data_policy),
    )


@router_agente.get("/{agent_id}/datos-cliente", response_model=AgenteDatosOut)
async def get_datos_cliente(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(require("settings"))],
) -> AgenteDatosOut:
    return await _leer(principal, await _agente(principal, agent_id))


@router_agente.put("/{agent_id}/datos-cliente", response_model=AgenteDatosOut)
async def save_datos_cliente(
    agent_id: uuid.UUID,
    payload: AgenteDatosUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> AgenteDatosOut:
    """Reemplaza los ajustes de las operaciones del agente. Rechaza acciones o datos que ese agente
    no tiene y los pisos técnicos; guarda solo lo que difiere del valor del módulo."""
    agente = await _agente(principal, agent_id)
    acciones_lista, _, _ = await _acciones_del_agente(principal, agente)
    acciones = {a.id: a for a in acciones_lista}
    nuevos: dict[str, dict[str, str]] = {}
    for accion_id, claves in (payload.politica or {}).items():
        accion = acciones.get(accion_id)
        if accion is None:
            raise ApiError(
                422,
                "accion_desconocida",
                "Esa operación no existe en este agente.",
                fields={"politica": "Esa operación no existe en este agente."},
            )
        defecto = niveles_efectivos(accion, None, await base_de(accion))
        elegidos: dict[str, str] = {}
        for clave, nivel in claves.items():
            campo = accion.campo(clave)
            if campo is None:
                raise ApiError(
                    422,
                    "dato_desconocido",
                    "Ese dato no existe en la operación.",
                    fields={"politica": "Ese dato no existe en la operación."},
                )
            if not campo.ajustable:
                raise ApiError(
                    422,
                    "dato_no_ajustable",
                    "Ese dato lo exige el sistema y no se puede cambiar.",
                    fields={"politica": "Ese dato lo exige el sistema y no se puede cambiar."},
                )
            if nivel != defecto[clave].value:
                elegidos[clave] = nivel
        if elegidos:
            nuevos[accion_id] = elegidos

    async with get_session() as session:
        fila = (
            await session.execute(
                select(AgentRecord)
                .where(
                    AgentRecord.tenant_id == principal.tenant_id,
                    AgentRecord.id == agent_id,
                    AgentRecord.deleted_at.is_(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if fila is None:
            raise ApiError.not_found("Agente no encontrado.")
        actual = politica_de(fila.data_policy)
        # Las operaciones que hoy no están habilitadas conservan su ajuste.
        if payload.politica is None:
            resultado = actual
        else:
            resultado = {k: v for k, v in actual.items() if k not in acciones}
            resultado.update(nuevos)
        nuevo_policy = con_politica(fila.data_policy, resultado)
        if payload.herramientas_del_agente is not None:
            nuevo_policy = con_herramientas(nuevo_policy, payload.herramientas_del_agente)
        if payload.metodos_entrega is not None:
            metodos = list(dict.fromkeys(payload.metodos_entrega))
            if not metodos:
                raise ApiError(
                    422,
                    "metodos_entrega_vacios",
                    "Elige al menos un método de entrega.",
                    fields={"metodos_entrega": "Elige al menos un método de entrega."},
                )
            nuevo_policy = con_metodos_entrega(nuevo_policy, metodos)
        if nuevo_policy != (fila.data_policy or {}):
            antes = {
                "politica": {k: v for k, v in actual.items() if k in acciones},
                "herramientas": herramientas_de(fila.data_policy),
                "metodos_entrega": metodos_entrega_de(fila.data_policy),
            }
            fila.data_policy = nuevo_policy
            session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="agent.datos_cliente",
                    input={
                        "agente_id": str(agent_id),
                        "antes": antes,
                        "despues": {
                            "politica": nuevos,
                            "herramientas": herramientas_de(nuevo_policy),
                            "metodos_entrega": metodos_entrega_de(nuevo_policy),
                        },
                    },
                )
            )
    return await _leer(principal, await _agente(principal, agent_id))


# ------------------------------------------------------------------ política global retirada (HU-CRM-28)

_POLITICA_RETIRADA = (
    "La política de datos del cliente es de cada agente (ADR-0198): usa /admin/agents/{id}/datos-cliente. "
    "El aviso de tratamiento de datos sigue en /admin/datos-cliente/aviso."
)
_RESPUESTA_RETIRADA = {
    410: {"description": "Retirado: la política es de cada agente, `error.code = politica_por_agente` (ADR-0198 §5)."}
}


@router.get("/politica", deprecated=True, status_code=410, responses=_RESPUESTA_RETIRADA)
async def get_politica_retirada(principal: Annotated[Principal, Depends(require("settings"))]) -> None:
    """Retirado (ADR-0198 §5): la política de datos del cliente es de cada agente (410 `politica_por_agente`)."""
    raise ApiError(410, "politica_por_agente", _POLITICA_RETIRADA)


@router.put("/politica", deprecated=True, status_code=410, responses=_RESPUESTA_RETIRADA)
async def save_politica_retirada(
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> None:
    """Retirado (ADR-0198 §5): la política de datos del cliente es de cada agente (410 `politica_por_agente`)."""
    raise ApiError(410, "politica_por_agente", _POLITICA_RETIRADA)



# ------------------------------------------------------------------ aviso de tratamiento de datos (HU-CRM-16)


class AvisoDatosOut(BaseModel):
    activo: bool
    texto: str  # el texto efectivo: el del negocio o, si no hay, el borrador
    texto_propio: str  # lo que escribió el negocio ("" = usa el borrador)
    es_borrador: bool  # el borrador NO está validado legalmente
    version: str


class AvisoDatosUpdate(BaseModel):
    activo: bool
    texto: str = Field(default="", max_length=1000)
    # Para ACTIVAR el aviso, quien lo hace confirma que el texto pasó por revisión legal (P-37).
    confirmo_revision_legal: bool = False


def _aviso_out(behavior: dict | None) -> AvisoDatosOut:
    from core.datos_cliente import aviso as aviso_mod
    from core.datos_cliente.ajustes import aviso_de

    aviso = aviso_mod.desde(behavior)
    return AvisoDatosOut(
        activo=aviso.activo,
        texto=aviso.texto,
        texto_propio=aviso_de(behavior)["texto"],
        es_borrador=aviso.es_borrador,
        version=aviso.version,
    )


async def _behavior_de(tenant_id) -> dict | None:
    async with get_session() as session:
        return (
            await session.execute(
                select(AppSettings.behavior_config).where(
                    AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()


@router.get("/aviso", response_model=AvisoDatosOut)
async def get_aviso(principal: Annotated[Principal, Depends(require("settings"))]) -> AvisoDatosOut:
    _exigir_negocio(principal)
    return _aviso_out(await _behavior_de(principal.tenant_id))


@router.put("/aviso", response_model=AvisoDatosOut)
async def save_aviso(
    payload: AvisoDatosUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> AvisoDatosOut:
    """Guarda el texto y el interruptor del aviso. Activarlo exige confirmar la revisión legal del texto.
    El texto completo queda en la auditoría junto con su versión."""
    from core.datos_cliente import aviso as aviso_mod
    from core.datos_cliente.ajustes import aviso_de, con_aviso

    _exigir_negocio(principal)
    if payload.activo and not payload.confirmo_revision_legal:
        raise ApiError(
            422,
            "revision_legal_requerida",
            "Para activar el aviso confirma que el texto fue revisado legalmente.",
            fields={"confirmo_revision_legal": "Confirma la revisión legal del texto."},
        )
    async with get_session() as session:
        app_settings = (
            await session.execute(
                select(AppSettings)
                .where(AppSettings.tenant_id == principal.tenant_id, AppSettings.deleted_at.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if app_settings is None:
            app_settings = AppSettings(tenant_id=principal.tenant_id)
            session.add(app_settings)
        antes = aviso_de(app_settings.behavior_config)
        nuevo = con_aviso(app_settings.behavior_config, payload.activo, payload.texto)
        if aviso_de(nuevo) != antes:
            app_settings.behavior_config = nuevo
            efectivo = aviso_mod.desde(nuevo)
            session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="settings.aviso_datos",
                    input={
                        "antes": antes,
                        "despues": aviso_de(nuevo),
                        "version": efectivo.version,
                        "texto_efectivo": efectivo.texto,
                        "es_borrador": efectivo.es_borrador,
                        "confirmo_revision_legal": payload.confirmo_revision_legal,
                    },
                )
            )
    return _aviso_out(await _behavior_de(principal.tenant_id))
