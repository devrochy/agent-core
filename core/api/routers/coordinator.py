"""Opciones propias del Coordinador (ADR-0211, HU-AOS-98).

El Coordinador no es un `AgentSpecV3` editable (ADR-0205 §5): sus opciones viven en `AppSettings` y la release las
congela. Cada campo escribe en el dato que ya lo gobierna; la escritura es PARCIAL (solo lo que viene en el PUT) y
bajo `FOR UPDATE`, para que dos pantallas no se pisen.

- `GET /api/v1/admin/coordinator` — voz e identidad, horario (el del negocio entero), nivel de IA y reglas.
- `PUT /api/v1/admin/coordinator` — cambia solo lo enviado. 422 por campo; 409 `plan_nivel_ia_not_included`.

Voz, identidad, reglas y nivel entran con la siguiente release de la conexión; el horario rige en caliente. Las
reglas y el nivel solo valen cuando el Coordinador atiende (sin especialistas). Las tools, los secretos, las
habilidades y los packs del Coordinador siguen cerrados: 409 `coordinador_inmutable` en las rutas del agente.
Permiso: `settings` (lectura y escritura), como el resto de la configuración del negocio.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from core.admin.settings_service import validate_business_ranges
from core.api.deps import Principal, require
from core.api.errors import ApiError
from core.coordinador import recepcion_config as rc
from core.db.models import AppSettings, AuditLog
from core.db.session import get_session
from core.kernel.horario_de_servicio import modo_del_horario
from core.llm import niveles
from core.llm.prompt_builder import DEFAULT_TIMEZONE

router = APIRouter(prefix="/admin/coordinator", tags=["admin · coordinador"])

_HORA = r"^([01]\d|2[0-3]):[0-5]\d$"


class BusinessRange(BaseModel):
    day: int = Field(ge=0, le=6)
    from_: str = Field(alias="from", pattern=_HORA)
    to: str = Field(pattern=_HORA)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _desde_antes_que_hasta(self) -> BusinessRange:
        if self.from_ >= self.to:
            raise ValueError("«from» tiene que ser anterior a «to».")
        return self


class CoordinatorOptions(BaseModel):
    assistant_name: str
    business_description: str
    brand_voice: str
    business_tz: str
    business_ranges: list[BusinessRange]
    business_hours_mode: Literal["bloquea", "informativo"]
    out_of_hours_message: str
    nivel_ia: Literal["estandar", "avanzado"]
    guardrails: list[dict[str, Any]]
    # Lo guardado que ya no pasa el contrato (dato malo): el panel pide corregirlo; `guardrails: []` lo repara.
    guardrails_problems: list[str] = []


class CoordinatorOptionsUpdate(BaseModel):
    """Todos opcionales: solo cambia lo que viene. `null` equivale a no enviarlo; para vaciar se envía `[]` o `""`
    (`business_ranges: []` borra el horario del negocio)."""

    assistant_name: str | None = Field(default=None, min_length=1, max_length=120)
    business_description: str | None = Field(default=None, max_length=4000)
    brand_voice: str | None = Field(default=None, max_length=4000)
    business_tz: str | None = Field(default=None, max_length=64)
    business_ranges: list[BusinessRange] | None = Field(default=None, max_length=50)
    business_hours_mode: Literal["bloquea", "informativo"] | None = None
    out_of_hours_message: str | None = Field(default=None, max_length=1000)
    nivel_ia: Literal["estandar", "avanzado"] | None = None
    guardrails: list[dict[str, Any]] | None = Field(default=None, max_length=rc.MAX_REGLAS)

    @field_validator("business_tz")
    @classmethod
    def _zona_valida(cls, valor: str | None) -> str | None:
        if valor is None or not valor.strip():
            return None
        try:
            ZoneInfo(valor.strip())
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Zona horaria no válida (por ejemplo America/Bogota).") from None
        return valor.strip()


def _exigir_negocio(principal: Principal) -> UUID:
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    return principal.tenant_id


def _problemas(behavior: dict[str, Any]) -> list[str]:
    try:
        rc.validar_reglas(rc.reglas_guardadas(behavior))
    except rc.ReglasInvalidas as exc:
        return [f"{r}: {m}" for r, m in exc.problemas][:10]
    return []


def _salida(fila: AppSettings | None) -> CoordinatorOptions:
    escalation = dict((fila.escalation_config if fila else None) or {})
    horario = escalation.get("business_hours") or {}
    behavior = dict((fila.behavior_config if fila else None) or {})
    return CoordinatorOptions(
        assistant_name=(fila.assistant_name if fila else "") or "",
        business_description=(fila.business_description if fila else "") or "",
        brand_voice=(fila.brand_voice if fila else "") or "",
        business_tz=str(horario.get("tz") or (fila.timezone if fila else "") or DEFAULT_TIMEZONE),
        business_ranges=validate_business_ranges(horario.get("ranges") or []),  # type: ignore[arg-type]
        business_hours_mode=modo_del_horario(escalation),  # type: ignore[arg-type]
        out_of_hours_message=str(escalation.get("out_of_hours_message") or ""),
        nivel_ia=rc.nivel_guardado(behavior),
        guardrails=[r for r in rc.reglas_guardadas(behavior) if isinstance(r, dict)],
        guardrails_problems=_problemas(behavior),
    )


async def _fila(session: Any, tenant_id: UUID, *, bloquear: bool) -> AppSettings | None:
    consulta = select(AppSettings).where(AppSettings.tenant_id == tenant_id, AppSettings.deleted_at.is_(None))
    if bloquear:
        consulta = consulta.with_for_update()
    return (await session.execute(consulta)).scalar_one_or_none()


@router.get("", response_model=CoordinatorOptions)
async def leer_opciones(principal: Annotated[Principal, Depends(require("settings"))]) -> CoordinatorOptions:
    tenant_id = _exigir_negocio(principal)
    async with get_session() as session:
        return _salida(await _fila(session, tenant_id, bloquear=False))


@router.put("", response_model=CoordinatorOptions)
async def guardar_opciones(
    payload: CoordinatorOptionsUpdate,
    principal: Annotated[Principal, Depends(require("settings", write=True))],
) -> CoordinatorOptions:
    tenant_id = _exigir_negocio(principal)
    reglas: list[dict[str, Any]] | None = None
    if payload.guardrails is not None:
        try:
            reglas = rc.validar_reglas(payload.guardrails)
        except rc.ReglasInvalidas as exc:
            raise ApiError(
                422, "validation_error", "Revisa las reglas marcadas.", fields=dict(exc.problemas)
            ) from None

    async with get_session() as session:
        fila = await _fila(session, tenant_id, bloquear=True)
        if fila is None:
            fila = AppSettings(tenant_id=tenant_id)
            session.add(fila)
        antes = _salida(fila).model_dump(mode="json", by_alias=True)

        if payload.nivel_ia is not None and payload.nivel_ia != rc.nivel_guardado(fila.behavior_config):
            if payload.nivel_ia != niveles.ESTANDAR:
                plan = await niveles.plan_del_negocio(tenant_id, session=session)
                if plan.techo is not None and not niveles.plan_incluye(payload.nivel_ia, {"nivel_ia": plan.techo}):
                    raise ApiError(
                        409,
                        niveles.NivelNoIncluido.code,
                        str(niveles.NivelNoIncluido(payload.nivel_ia, plan.plan_id, plan.plan_name)),
                    )

        if payload.assistant_name is not None:
            fila.assistant_name = payload.assistant_name.strip()
        if payload.business_description is not None:
            fila.business_description = payload.business_description
        if payload.brand_voice is not None:
            fila.brand_voice = payload.brand_voice

        escalation = dict(fila.escalation_config or {})
        horario = dict(escalation.get("business_hours") or {})
        if payload.business_ranges is not None or payload.business_tz is not None:
            rangos = (
                validate_business_ranges([r.model_dump(by_alias=True) for r in payload.business_ranges])
                if payload.business_ranges is not None
                else list(horario.get("ranges") or [])
            )
            tz = payload.business_tz or horario.get("tz") or fila.timezone or DEFAULT_TIMEZONE
            escalation["business_hours"] = {"tz": tz, "ranges": rangos} if rangos else None
        if payload.out_of_hours_message is not None:
            escalation["out_of_hours_message"] = payload.out_of_hours_message.strip()
        if payload.business_hours_mode is not None:
            escalation["business_hours_mode"] = payload.business_hours_mode
        if escalation != dict(fila.escalation_config or {}):
            fila.escalation_config = escalation

        behavior = dict(fila.behavior_config or {})
        if reglas is not None:
            behavior[rc.CLAVE_REGLAS] = reglas
        if payload.nivel_ia is not None:
            behavior[rc.CLAVE_NIVEL] = payload.nivel_ia
        if behavior != dict(fila.behavior_config or {}):
            fila.behavior_config = behavior

        despues = _salida(fila).model_dump(mode="json", by_alias=True)
        if despues != antes:
            session.add(
                AuditLog(
                    tenant_id=tenant_id,
                    actor=f"admin:{principal.admin.email}",
                    action="settings.coordinator",
                    # Solo los nombres de lo que cambió: la voz y las reglas son texto libre del dueño.
                    input={"campos": sorted(k for k in despues if antes[k] != despues[k])},
                )
            )
        await session.flush()
        return _salida(fila)
