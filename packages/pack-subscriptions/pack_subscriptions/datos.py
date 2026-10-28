"""Acción de Suscripciones que pide datos del cliente (HU-CRM-13, ADR-0179 decisión 1).

La `base` lee `campos_de_inscripcion` de la configuración del negocio, así que sin ajustes del
dueño se piden los mismos datos de siempre.
"""

from __future__ import annotations

from core.datos_cliente import AccionDatos, CampoPolitica, Nivel, registro_datos
from core.memory.perfil import normalizar_clave

PLUGIN_ID = "pack_subscriptions"
ACCION_INSCRIBIRSE = "suscripciones.inscribirse"
R, NO = Nivel.REQUERIDO, Nivel.NO_PEDIR

# Cómo se llama en `campos_de_inscripcion` la clave canónica que la política sube y no estaba.
A_LEGADO = {"name": "nombre", "phone": "telefono", "id_document": "documento"}


async def _base() -> dict[str, Nivel]:
    from core.plugin_manager import plugin_manager

    config = await plugin_manager.load_config(PLUGIN_ID)
    return {normalizar_clave(k): R for k in (getattr(config, "campos_de_inscripcion", None) or [])}


registro_datos.registrar(
    AccionDatos(
        id=ACCION_INSCRIBIRSE,
        modulo="suscripciones",
        pack=PLUGIN_ID,
        etiqueta="Inscribirse a un plan",
        campos=(
            CampoPolitica("name", "Nombre completo", R),
            CampoPolitica("email", "Correo", R),
            CampoPolitica("phone", "Teléfono", R),
            CampoPolitica("id_document", "Documento", NO),
            CampoPolitica("negocio", "Nombre del negocio", NO),
            CampoPolitica("vertical", "Tipo de negocio", NO),
        ),
        base=_base,
    )
)
