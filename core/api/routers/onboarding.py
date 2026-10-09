"""Primeros pasos del negocio: `GET /api/v1/admin/onboarding` (HU-SH-06, ADR-0124).

El checklist del Resumen calculado del estado real (`core/primeros_pasos`): datos del negocio,
WhatsApp conectado y verificado, catálogo o servicios, probar el asistente, equipo invitado, el
tipo de negocio que espera un plan más alto y las habilidades pendientes de configurar. Cada paso
dice si el usuario puede actuar sobre él (sección del RBAC con escritura): el panel no muestra
una acción que daría 403.

Al evaluar se registra la primera vez de cada paso cumplido (`onboarding_hitos`, el embudo de
activación). Es un efecto secundario idempotente y no falla la lectura.

Requiere la sección `dashboard` (la misma del Resumen).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.primeros_pasos.hitos import evaluar
from core.primeros_pasos.reglas import Estado, Motivo, Paso

router = APIRouter(prefix="/admin/onboarding", tags=["admin · primeros pasos"])


class PasoOut(BaseModel):
    clave: str = Field(
        description=(
            "datos_negocio | whatsapp | catalogo | probar_asistente | equipo | activar_preset | "
            "configurar_<habilidad>"
        )
    )
    estado: Estado
    motivo: Motivo | None = Field(default=None, description="Por qué no está completo (clave para el panel).")
    cantidad: int | None = Field(default=None, description="Elementos cargados (paso `catalogo`).")
    pack: str | None = Field(default=None, description="Habilidad del paso (`catalogo` y `configurar_*`).")
    tipo: Literal["catalogo", "servicios", "cartera"] | None = None
    titulo: str | None = Field(default=None, description="Nombre visible de la habilidad o del tipo de negocio.")
    explicacion: str | None = Field(default=None, description="Guía de configuración de la habilidad (su catálogo).")
    plan_requerido: str | None = Field(default=None, description="Plan que necesita el tipo de negocio.")
    puede_actuar: bool = Field(description="El usuario tiene escritura en la sección del paso.")
    completado_en: datetime | None = Field(default=None, description="Primera vez que se cumplió (hito).")
    segundos_desde_alta: int | None = Field(default=None, description="Del alta a la primera vez que se cumplió.")


class PrimerosPasosResponse(BaseModel):
    alta_en: datetime
    origen: Literal["aprovisionamiento", "manual"]
    completos: int
    total: int
    pasos: list[PasoOut]


def _puede_ver(principal: Principal, seccion: str | None) -> bool:
    """¿Puede leer la sección del paso? Si no, el paso no le muestra cuánto hay cargado."""
    if seccion is None:
        return False
    return principal.admin.role == "master" or principal.permissions.get(seccion) in ("read", "write")


def _puede_actuar(principal: Principal, seccion: str | None) -> bool:
    if seccion is None:
        return False
    if principal.admin.role == "master":
        return True
    return principal.permissions.get(seccion) == "write"


@router.get("", response_model=PrimerosPasosResponse)
async def get_primeros_pasos(
    principal: Annotated[Principal, Depends(require("dashboard"))],
) -> PrimerosPasosResponse:
    if principal.tenant_id is None:
        raise ApiError.bad_request("No hay negocio activo.", "no_tenant")
    resultado = await evaluar(principal.tenant_id)
    if resultado is None:
        raise ApiError.not_found("Negocio no encontrado.")
    evaluacion, hitos = resultado

    def _out(paso: Paso) -> PasoOut:
        hito = hitos.get(paso.clave)
        return PasoOut(
            clave=paso.clave,
            estado=paso.estado,
            motivo=paso.motivo,
            cantidad=paso.cantidad if _puede_ver(principal, paso.seccion) else None,
            pack=paso.pack,
            tipo=paso.tipo,  # type: ignore[arg-type]
            titulo=paso.titulo,
            explicacion=paso.explicacion,
            plan_requerido=paso.plan_requerido,
            puede_actuar=_puede_actuar(principal, paso.seccion),
            completado_en=hito.completado_en if hito else None,
            segundos_desde_alta=hito.segundos_desde_alta if hito else None,
        )

    pasos = [_out(p) for p in evaluacion.pasos]
    return PrimerosPasosResponse(
        alta_en=evaluacion.alta_en,
        origen=evaluacion.origen,
        completos=sum(1 for p in pasos if p.estado == "completo"),
        total=len(pasos),
        pasos=pasos,
    )
