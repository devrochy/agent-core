"""Catálogo de permisos del gestor de cartera (app de campo, v2).

El **propietario es el tenant** (ADR-0062): sus permisos los resuelve el RBAC del
core (owner → write, colaborador → read), así que no hay catálogo propio de
propietario. El gestor (usuario del pack) sí tiene su matriz de 14 permisos en
`pack_cobranza_gestor_permisos` (pivot permiso + habilitado).
"""

from __future__ import annotations

GESTOR_PERMISOS: tuple[str, ...] = (
    "registrar_prestamo",
    "registrar_pago",
    "registrar_abono",
    "registrar_gasto",
    "registrar_no_pago",
    "anotar_notas_cartera",
    "actualizar_cliente",
    "eliminar_prestamo",
    "eliminar_pago",
    "eliminar_abono",
    "eliminar_gasto",
    "registrar_inyeccion",
    "ver_cartera",
    "generar_reporte",
)
