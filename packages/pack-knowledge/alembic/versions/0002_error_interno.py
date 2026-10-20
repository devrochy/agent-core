"""pack_knowledge: código de error `interno` (HU-AOS-49 parte 2).

Una excepción inesperada en el worker deja el documento `fallido` con `error_codigo = interno` (y la
clase de la excepción en `error_detalle`), en vez de quedar en `procesando` para siempre. Solo
cambia el CHECK: la tabla es nueva y no hay filas que validar a la fuerza.

Revision ID: pack_knowledge_0002
Revises: pack_knowledge_0001
Create Date: 2026-10-20
"""

from __future__ import annotations

from alembic import op

revision = "pack_knowledge_0002"
down_revision = "pack_knowledge_0001"
branch_labels = None
depends_on = None

_TABLA = "pack_knowledge_documents"
_CHECK = "ck_pack_knowledge_documents_error"
_CODIGOS = ("sin_texto", "muy_grande", "formato", "url_inaccesible", "limite_plan", "proveedor")


def _crear(codigos: tuple[str, ...]) -> None:
    lista = ", ".join(f"'{c}'" for c in codigos)
    op.create_check_constraint(_CHECK, _TABLA, f"error_codigo is null or error_codigo in ({lista})")


def upgrade() -> None:
    op.drop_constraint(_CHECK, _TABLA, type_="check")
    _crear((*_CODIGOS, "interno"))


def downgrade() -> None:
    op.execute(f"update {_TABLA} set error_codigo = 'formato' where error_codigo = 'interno'")
    op.drop_constraint(_CHECK, _TABLA, type_="check")
    _crear(_CODIGOS)
