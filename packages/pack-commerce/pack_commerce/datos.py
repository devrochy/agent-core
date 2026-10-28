"""Acciones de Ventas que piden datos del cliente (HU-CRM-13, ADR-0179 decisión 1).

Los valores por defecto reproducen `checkout_fields` de fábrica; la `base` lee la configuración
vigente del negocio, así que sin ajustes del dueño el comportamiento es el de siempre.
"""

from __future__ import annotations

from typing import Any

from core.datos_cliente import AccionDatos, CampoPolitica, Nivel, registro_datos
from core.datos_cliente.modelo import BaseDelNegocio
from core.memory.perfil import normalizar_clave

PLUGIN_ID = "pack_commerce"
R, REC, NO = Nivel.REQUERIDO, Nivel.RECOMENDADO, Nivel.NO_PEDIR

# Cómo se llama en `checkout_fields` la clave canónica que la política sube y no estaba en la lista.
A_LEGADO = {"name": "full_name", "municipio": "municipality"}

# Tipo de entrega o producto -> acción de la política.
ACCION_POR_ENTREGA = {
    "domicilio": "ventas.pedido_domicilio",
    "recoger_en_tienda": "ventas.pedido_retiro",
    "plan": "ventas.plan_legado",
}


def _base_de(lista: str) -> BaseDelNegocio:
    async def base() -> dict[str, Nivel]:
        from core.plugin_manager import plugin_manager

        config = await plugin_manager.load_config(PLUGIN_ID)
        campos: dict[str, Any] = getattr(config, "checkout_fields", None) or {}
        return {normalizar_clave(k): R for k in campos.get(lista, [])}

    return base


def _campos(**niveles: Nivel) -> tuple[CampoPolitica, ...]:
    etiquetas = {
        "name": "Nombre completo",
        "id_document": "Documento",
        "phone": "Teléfono",
        "email": "Correo",
        "address": "Dirección",
        "municipio": "Municipio o ciudad",
        "business_name": "Nombre del negocio",
        "vertical": "Tipo de negocio",
    }
    return tuple(CampoPolitica(k, etiquetas[k], n) for k, n in niveles.items())


registro_datos.registrar(
    AccionDatos(
        id="ventas.pedido_domicilio",
        modulo="ventas",
        pack=PLUGIN_ID,
        etiqueta="Pedido a domicilio",
        campos=_campos(name=R, id_document=R, phone=R, address=R, municipio=R, email=NO),
        base=_base_de("domicilio"),
    )
)
registro_datos.registrar(
    AccionDatos(
        id="ventas.pedido_retiro",
        modulo="ventas",
        pack=PLUGIN_ID,
        etiqueta="Pedido para recoger en tienda",
        campos=_campos(name=R, id_document=R, phone=R, email=NO, address=NO, municipio=NO),
        base=_base_de("recoger_en_tienda"),
    )
)
registro_datos.registrar(
    AccionDatos(
        id="ventas.plan_legado",
        modulo="ventas",
        pack=PLUGIN_ID,
        etiqueta="Contratar un plan (pedido de plan)",
        campos=_campos(name=R, id_document=R, phone=R, email=R, business_name=NO, vertical=NO),
        base=_base_de("plan"),
    )
)
