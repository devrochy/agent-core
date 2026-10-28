"""Registro de acciones que piden datos del cliente (HU-CRM-13, ADR-0179 decisión 1).

Los packs se registran desde su módulo `datos.py` (cargado por su `on_activate`), igual que el
`lead_registry`: el core nunca importa un pack (invariante 5). La política efectiva de un negocio
es: ajuste del dueño > configuración del módulo (`base`) > valor por defecto de la acción.
"""

from __future__ import annotations

from core.datos_cliente.modelo import AccionDatos


class RegistroDeDatos:
    def __init__(self) -> None:
        self._acciones: dict[str, AccionDatos] = {}

    def registrar(self, accion: AccionDatos) -> None:
        claves = [c.clave for c in accion.campos]
        if len(claves) != len(set(claves)):
            raise ValueError(f"{accion.id}: campos repetidos")
        self._acciones[accion.id] = accion  # idempotente: un reimport reemplaza

    def obtener(self, accion_id: str) -> AccionDatos | None:
        return self._acciones.get(accion_id)

    def de_packs(self, habilitados: set[str] | None) -> list[AccionDatos]:
        """Acciones de los packs habilitados (`None` = todas), en orden estable."""
        return [
            a
            for a in sorted(self._acciones.values(), key=lambda x: (x.modulo, x.id))
            if habilitados is None or a.pack in habilitados
        ]


registro_datos = RegistroDeDatos()
