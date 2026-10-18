"""Tabla de SISTEMA (sin RLS): `admin_permissions.access` admite `none` (denegado), ADR-0047.

`admin_permissions` es tabla de sistema (sin `tenant_id not null` ni RLS: se consulta al
autenticar, antes de conocer el negocio; excepción documentada en ADR-0047). Esta migración
no crea tablas: amplía el CHECK de `access` a `read | write | none`.

Por qué: el rol del `tenant_user` da acceso por defecto (`colaborador` → read, `owner` →
write). Borrar la fila explícita devolvía ese default y «revocar» no cambiaba nada (defecto
P0 del ciclo de pruebas 2026-10-13: «revocó reportes cobranza y aún así veo la sección»).
Con `none` el dueño deniega una sección —o un pack completo— a una cuenta de su negocio.

`none` solo existe con alcance de negocio (`tenant_id not null`): la rama de `admin_tenants()`
(0088) que muestra todos los negocios a quien tiene un permiso GLOBAL no debe dispararse por
una denegación. Sin grants nuevos: `app_runtime` ya opera la tabla (0035).

Revision ID: 0090
Revises: 0089
Create Date: 2026-10-17
"""

from __future__ import annotations

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

_TABLE = "admin_permissions"


def upgrade() -> None:
    op.drop_constraint("ck_admin_permissions_access", _TABLE, type_="check")
    op.create_check_constraint(
        "ck_admin_permissions_access",
        _TABLE,
        "access in ('read', 'write') or (access = 'none' and tenant_id is not null)",
    )


def downgrade() -> None:
    # Las denegaciones no existen en el esquema anterior: se descartan antes de reponer el CHECK.
    op.execute(f"delete from {_TABLE} where access = 'none'")
    op.drop_constraint("ck_admin_permissions_access", _TABLE, type_="check")
    op.create_check_constraint("ck_admin_permissions_access", _TABLE, "access in ('read', 'write')")
