"""Política de datos del cliente por módulo (HU-CRM-13, ADR-0179 decisión 1).

`GET /api/v1/admin/datos-cliente/politica` lista las acciones de los módulos habilitados del
negocio con el nivel efectivo de cada dato (requerido, recomendado, no pedir) y el que trae el
módulo por defecto. `PUT` guarda solo lo que difiere del módulo. Requiere la sección `settings`;
cada cambio deja una fila de auditoría con el antes y el después.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.agents.spec import enabled_packs, load_active_spec
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.datos_cliente import AccionDatos, Nivel, niveles_efectivos, registro_datos
from core.datos_cliente.ajustes import con_herramientas, con_politica, herramientas_de, politica_de
from core.datos_cliente.servicio import base_de
from core.db.models import AppSettings, AuditLog
from core.db.session import get_session

router = APIRouter(prefix="/admin/datos-cliente", tags=["admin · datos del cliente"])

NivelOut = Literal["requerido", "recomendado", "no_pedir"]


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


class PoliticaDatosOut(BaseModel):
    niveles: list[NivelOut]
    acciones: list[AccionPoliticaOut]
    # Si el asistente puede consultar los datos del cliente con sus herramientas (apagado por defecto).
    herramientas_del_agente: bool = False


class PoliticaDatosUpdate(BaseModel):
    """`{accion_id: {clave: nivel}}`. Lo que no viene para una acción vuelve al valor del módulo.
    Sin `politica` (None) no se tocan los ajustes: sirve para cambiar solo la bandera."""

    politica: dict[str, dict[str, NivelOut]] | None = Field(default=None, max_length=50)
    # None = no cambia. True/False enciende o apaga las herramientas de datos del cliente del asistente.
    herramientas_del_agente: bool | None = None


def _exigir_negocio(principal: Principal) -> None:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")


async def _habilitadas() -> list[AccionDatos]:
    try:
        habilitados = enabled_packs(await load_active_spec())
    except Exception:
        habilitados = set()
    return registro_datos.de_packs(habilitados)


async def _leer(behavior: dict | None, acciones: list[AccionDatos]) -> PoliticaDatosOut:
    ajustes = politica_de(behavior)
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
    return PoliticaDatosOut(
        niveles=[n.value for n in Nivel], acciones=salida, herramientas_del_agente=herramientas_de(behavior)
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


@router.get("/politica", response_model=PoliticaDatosOut)
async def get_politica(
    principal: Annotated[Principal, Depends(require("settings"))],
) -> PoliticaDatosOut:
    _exigir_negocio(principal)
    return await _leer(await _behavior_de(principal.tenant_id), await _habilitadas())


@router.put("/politica", response_model=PoliticaDatosOut)
async def save_politica(
    payload: PoliticaDatosUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> PoliticaDatosOut:
    """Reemplaza los ajustes de las acciones habilitadas. Rechaza acciones o datos que el negocio
    no tiene y los pisos técnicos; guarda solo lo que difiere del valor del módulo."""
    _exigir_negocio(principal)
    acciones = {a.id: a for a in await _habilitadas()}
    nuevos: dict[str, dict[str, str]] = {}
    for accion_id, claves in (payload.politica or {}).items():
        accion = acciones.get(accion_id)
        if accion is None:
            raise ApiError(422, "accion_desconocida", "Esa acción no existe en este negocio.", fields={"politica": "Esa acción no existe en este negocio."})
        defecto = niveles_efectivos(accion, None, await base_de(accion))
        elegidos: dict[str, str] = {}
        for clave, nivel in claves.items():
            campo = accion.campo(clave)
            if campo is None:
                raise ApiError(422, "dato_desconocido", "Ese dato no existe en la acción.", fields={"politica": "Ese dato no existe en la acción."})
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
        actual = politica_de(app_settings.behavior_config)
        # Las acciones de módulos que hoy no están habilitados conservan su ajuste.
        if payload.politica is None:
            resultado = actual
        else:
            resultado = {k: v for k, v in actual.items() if k not in acciones}
            resultado.update(nuevos)
        if resultado != actual:
            antes = {k: v for k, v in actual.items() if k in acciones}
            app_settings.behavior_config = con_politica(app_settings.behavior_config, resultado)
            session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="settings.datos_cliente",
                    input={"antes": antes, "despues": nuevos},
                )
            )
        if payload.herramientas_del_agente is not None and payload.herramientas_del_agente != herramientas_de(
            app_settings.behavior_config
        ):
            app_settings.behavior_config = con_herramientas(app_settings.behavior_config, payload.herramientas_del_agente)
            session.add(
                AuditLog(
                    tenant_id=principal.tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="settings.datos_cliente_herramientas",
                    input={"despues": payload.herramientas_del_agente},
                )
            )
    return await _leer(await _behavior_de(principal.tenant_id), list(acciones.values()))


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
