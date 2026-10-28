"""Catálogo unificado de mensajes del negocio (pista D0, ADR-0178): `GET /api/v1/mensajes/catalogo`.

Solo lectura. Junta en una lista las plantillas de Meta, los avisos por evento, las plantillas con opciones y los
textos de los packs, y apunta a cada editor actual. Las reglas y las fuentes están en
`core/admin/catalogo_mensajes_service.py`; aquí solo HTTP y permisos.

Permisos: las cuatro fuentes ya viven en tres secciones distintas, así que la ruta no inventa una nueva ni amplía lo
que alguien ve hoy: cada tipo se incluye solo si quien consulta puede leer SU sección (`connections` → `meta` y
`opciones`; `settings` → `evento`; `plugins` → `texto_pack`, las mismas de las rutas de siempre; el master las tiene
todas). Sin ninguna de las tres: 403. Lo que se deja fuera se informa en `omitidas` (`sin_permiso`; `bandera_apagada`
para `opciones`, que con la bandera `opciones_directas.habilitada` apagada responde 404 en `/plantillas-opciones`).
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.admin import catalogo_mensajes_service as svc
from core.api.deps import Principal, get_principal
from core.api.errors import ApiError

router = APIRouter(prefix="/mensajes", tags=["mensajes"])

TipoDeMensaje = Literal["meta", "evento", "opciones", "texto_pack"]

# Sección que protege la fuente de cada tipo (la misma que exigen sus rutas actuales).
SECCION_POR_TIPO: dict[svc.TipoMensaje, str] = {
    "meta": "connections",
    "opciones": "connections",
    "evento": "settings",
    "texto_pack": "plugins",
}


class FuenteDeMensaje(BaseModel):
    """Dónde se edita: el panel arma el enlace con estos ids. Solo vienen los del tipo (el resto es nulo)."""

    editor: TipoDeMensaje
    connection_id: uuid.UUID | None = Field(default=None, description="`meta`: la conexión de la WABA que tiene la plantilla.")
    message_template_id: uuid.UUID | None = Field(default=None, description="`meta`: la fila de `message_templates`.")
    evento: str | None = Field(default=None, description="`evento`: clave del evento.")
    clave: str | None = Field(default=None, description="`opciones`: clave de la plantilla con opciones.")
    plugin_id: str | None = Field(default=None, description="`texto_pack`: el pack dueño del texto.")
    campo: str | None = Field(default=None, description="`texto_pack`: el campo de la configuración del pack.")


class MensajeDelCatalogo(BaseModel):
    id: str = Field(description="Estable y único en el catálogo (`<tipo>:<…>`).")
    tipo: TipoDeMensaje
    clave: str
    nombre: str
    idioma: str | None = Field(description="`meta`: idioma de la plantilla; `evento`: el del mapeo, si hay.")
    estado: str = Field(
        description="`meta`: el de Meta (`APPROVED`, `PENDING`…); `evento`: `sin_mapear`, `mapeado` o `plantilla_no_aprobada`; "
        "`opciones`: `draft`, `published` o `retired`; `texto_pack`: `por_defecto`, `personalizado` o `config_invalida`."
    )
    estado_detalle: str | None = Field(
        description="`meta`: categoría; `evento`: plantilla mapeada; `opciones`: estado de su plantilla en Meta si se publicó allí; "
        "`texto_pack`: nombre del pack."
    )
    canales_soportados: list[str] = Field(description="Canales por los que sale hoy (constante por tipo hasta la pista C1).")
    variables: list[str] = Field(
        description="Tal como las usa la fuente, sin normalizar: marcadores de Meta, rutas permitidas del evento, variables de la "
        "definición o llaves del texto."
    )
    dialecto_variables: Literal["meta_posicionales", "meta_nombradas", "llaves_con_ruta"]
    resumen: str | None = Field(description="Primeros ~160 caracteres del texto propio del negocio (nunca datos de un cliente).")
    actualizado_en: datetime | None = Field(
        description="`meta`: la última sincronización con Meta (el estado puede estar desfasado: no se sincroniza al listar); "
        "`opciones`: la última edición. Nulo en `evento` y `texto_pack`."
    )
    fuente: FuenteDeMensaje


class MensajeOmitido(BaseModel):
    tipo: TipoDeMensaje
    motivo: Literal["sin_permiso", "bandera_apagada"]


class CatalogoDeMensajesOut(BaseModel):
    mensajes: list[MensajeDelCatalogo]
    omitidas: list[MensajeOmitido] = Field(description="Tipos que no se listan y por qué.")
    truncado: list[TipoDeMensaje] = Field(description="Tipos que pasaron el tope por tipo: la lista está recortada.")


@router.get("/catalogo", response_model=CatalogoDeMensajesOut)
async def catalogo(principal: Annotated[Principal, Depends(get_principal)]) -> CatalogoDeMensajesOut:
    """Los mensajes del negocio activo en una sola lista, de solo lectura. No sincroniza con Meta (muestra lo último
    guardado) y no descifra nada fuera de la lectura de configuración de los packs. 403 si no se puede leer
    ninguna de las fuentes; 400 `no_tenant` si no hay un negocio seleccionado."""
    if principal.tenant_id is None:
        raise ApiError.bad_request("Selecciona un negocio.", code="no_tenant")
    permitidos = [tipo for tipo, seccion in SECCION_POR_TIPO.items() if principal.permissions.get(seccion) is not None]
    if not permitidos:
        raise ApiError.forbidden("Sin permiso para ver los mensajes del negocio.")
    resultado = await svc.catalogo_de_mensajes(principal.tenant_id, permitidos)
    return CatalogoDeMensajesOut(
        mensajes=[MensajeDelCatalogo.model_validate(asdict(e)) for e in resultado.entradas],
        omitidas=[MensajeOmitido(tipo=o.tipo, motivo=o.motivo) for o in resultado.omitidas],
        truncado=resultado.truncado,
    )
