"""Opciones directas: `option_templates`, `option_template_versions`, `option_sends` y `option_activations` (ADR-0172, SPEC-40).

Cuatro tablas de NEGOCIO: `tenant_id` + RLS habilitada y forzada + policy de aislamiento (invariante 1),
índices compuestos que empiezan por `tenant_id` y FKs COMPUESTAS `(tenant_id, …)` entre ellas y hacia
`conversations` (una referencia entre negocios es imposible a nivel de esquema, como en 0067/0069).

- `option_templates`: la plantilla del negocio. Clave única por negocio entre las vivas (índice parcial
  `where deleted_at is null`), borrador editable en `draft_definition` y número de la versión vigente. ÚNICA
  con borrado lógico.
- `option_template_versions`: una fila POR VERSIÓN PUBLICADA (el borrador no vive aquí), por eso toda fila es
  inmutable: `prevent_mutation('message_template_id', 'retired_at')` (0067) rechaza DELETE, TRUNCATE y todo
  UPDATE salvo esas dos columnas. `message_template_id` se fija DESPUÉS de congelar (publicación en Meta,
  `<clave>_v<n>`) y `retired_at` marca el retiro; el resto (`definition`, `definition_hash`, `version_n`…) no
  cambia nunca. FK simple a `message_templates` con `ON DELETE SET NULL` (esa tabla se borra en cascada con
  la WABA y no tiene `UNIQUE (tenant_id, id)`); el negocio de la plantilla se valida en código al publicar.
- `option_sends`: un envío concreto (conversación, versión, vía, `expires_at`). `context` guarda SOLO las
  rutas y valores que el servidor resolvió para renderizar el cuerpo; el mismo texto ya está en `messages`.
- `option_activations`: un toque. `UNIQUE (tenant_id, dedupe_key)` y como máximo UNA activación pendiente
  (`pending_confirm`/`running`) por envío y opción (índice parcial): así el `optc:`/`optn:` de una opción no
  es ambiguo aunque el envío no sea exclusivo. `inbound_wamid` único por negocio: el reintento del webhook
  de Meta no ejecuta dos veces. No guarda argumentos ni resultados, solo `args_hash` y un `error_code`.

Excepción documentada a la invariante 8: `versions`, `sends` y `activations` no llevan `deleted_at` (son
historia de lo publicado y de lo ejecutado, de solo avance); los borra únicamente `purge_tenant`, que no
necesita cambios (descubre las tablas por su columna `tenant_id` y apaga los triggers con
`session_replication_role = replica`, 0072). Por la misma razón `app_runtime` no recibe DELETE en ninguna.

Aditiva y reversible: tablas nuevas y vacías, sin reescritura ni bloqueo largo; el downgrade las elimina en
orden inverso (se pierden las plantillas y la historia de toques, no hay datos previos que preservar).

Revision ID: 0112
Revises: 0111
Create Date: 2026-10-27
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

RLS = "nullif(current_setting('app.tenant_id', true), '')::uuid"
_UUID = UUID(as_uuid=True)
_TS = sa.DateTime(timezone=True)
_TABLAS = ("option_templates", "option_template_versions", "option_sends", "option_activations")
_AHORA = sa.text("now()")


def _rls(table: str) -> None:
    op.execute(f"alter table {table} enable row level security")
    op.execute(f"alter table {table} force row level security")
    op.execute(
        f"create policy tenant_isolation on {table} "
        f"using (tenant_id = {RLS}) with check (tenant_id = {RLS})"
    )
    # Sin DELETE: el borrado lógico es un UPDATE y la historia no se borra (solo `purge_tenant`). Los default
    # privileges de 0003 le dan DELETE al rol de la app, así que se revoca antes de conceder (como 0069).
    op.execute(f"revoke all on {table} from app_runtime")
    op.execute(f"grant select, insert, update on {table} to app_runtime")


def upgrade() -> None:
    op.create_table(
        "option_templates",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("draft_definition", JSONB(), nullable=True),
        sa.Column("current_version_n", sa.Integer(), nullable=True),
        sa.Column("deleted_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.UniqueConstraint("tenant_id", "id", name="uq_option_templates_tenant_id_id"),
        sa.CheckConstraint("status in ('draft','published','retired')", name="ck_option_templates_status"),
        sa.CheckConstraint("key ~ '^[a-z0-9_]{1,40}$'", name="ck_option_templates_key"),
        sa.CheckConstraint("current_version_n is null or current_version_n >= 1", name="ck_option_templates_version"),
    )
    op.create_index(
        "uq_option_templates_tenant_key",
        "option_templates",
        ["tenant_id", "key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "option_template_versions",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("option_template_id", _UUID, nullable=False),
        sa.Column("version_n", sa.Integer(), nullable=False),
        sa.Column("definition", JSONB(), nullable=False),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("message_template_id", _UUID, sa.ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True),
        sa.Column("published_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("retired_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.UniqueConstraint("tenant_id", "id", name="uq_option_template_versions_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "option_template_id", "version_n", name="uq_option_template_versions_n"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "option_template_id"],
            ["option_templates.tenant_id", "option_templates.id"],
            name="fk_option_versions_template",
        ),
        sa.CheckConstraint("version_n >= 1", name="ck_option_versions_n"),
        sa.CheckConstraint("definition_hash ~ '^[0-9a-f]{64}$'", name="ck_option_versions_hash"),
    )
    op.create_index(
        "ix_option_versions_tenant_message_template", "option_template_versions", ["tenant_id", "message_template_id"]
    )

    op.create_table(
        "option_sends",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("option_template_id", _UUID, nullable=False),
        sa.Column("version_id", _UUID, nullable=False),
        sa.Column("conversation_id", _UUID, nullable=False),
        sa.Column("via", sa.Text(), nullable=False),
        sa.Column("context", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.UniqueConstraint("tenant_id", "id", name="uq_option_sends_tenant_id_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["option_template_versions.tenant_id", "option_template_versions.id"],
            name="fk_option_sends_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_option_sends_conversation",
        ),
        sa.CheckConstraint("via in ('interactive','template','text')", name="ck_option_sends_via"),
    )
    op.create_index(
        "ix_option_sends_tenant_conversation", "option_sends", ["tenant_id", "conversation_id", sa.text("created_at DESC")]
    )
    op.create_index("ix_option_sends_tenant_version", "option_sends", ["tenant_id", "version_id"])

    op.create_table(
        "option_activations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, nullable=False),
        sa.Column("send_id", _UUID, nullable=False),
        sa.Column("option_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending_confirm"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("inbound_wamid", sa.Text(), nullable=True),
        sa.Column("args_hash", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("claimed_at", _TS, nullable=True),
        sa.Column("finished_at", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=_AHORA),
        sa.Column("updated_at", _TS, nullable=False, server_default=_AHORA),
        sa.UniqueConstraint("tenant_id", "id", name="uq_option_activations_tenant_id_id"),
        sa.UniqueConstraint("tenant_id", "dedupe_key", name="uq_option_activations_tenant_dedupe"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "send_id"],
            ["option_sends.tenant_id", "option_sends.id"],
            name="fk_option_activations_send",
        ),
        sa.CheckConstraint(
            "state in ('pending_confirm','running','done','failed','cancelled','expired')",
            name="ck_option_activations_state",
        ),
        sa.CheckConstraint("option_id ~ '^[a-z0-9_]{1,12}$'", name="ck_option_activations_option_id"),
    )
    op.create_index(
        "uq_option_activations_pendiente",
        "option_activations",
        ["tenant_id", "send_id", "option_id"],
        unique=True,
        postgresql_where=sa.text("state in ('pending_confirm','running')"),
    )
    op.create_index(
        "uq_option_activations_wamid",
        "option_activations",
        ["tenant_id", "inbound_wamid"],
        unique=True,
        postgresql_where=sa.text("inbound_wamid IS NOT NULL"),
    )
    op.create_index("ix_option_activations_tenant_send", "option_activations", ["tenant_id", "send_id"])

    for tabla in _TABLAS:
        _rls(tabla)

    # Versiones publicadas = inmutables (función genérica de 0067): solo `message_template_id` y `retired_at`.
    op.execute(
        "CREATE TRIGGER option_template_versions_inmutable BEFORE UPDATE OR DELETE ON option_template_versions "
        "FOR EACH ROW EXECUTE FUNCTION prevent_mutation('message_template_id', 'retired_at')"
    )
    op.execute(
        "CREATE TRIGGER option_template_versions_sin_vaciar BEFORE TRUNCATE ON option_template_versions "
        "FOR EACH STATEMENT EXECUTE FUNCTION prevent_mutation()"
    )


def downgrade() -> None:
    op.execute("drop trigger if exists option_template_versions_sin_vaciar on option_template_versions")
    op.execute("drop trigger if exists option_template_versions_inmutable on option_template_versions")
    for tabla in reversed(_TABLAS):
        op.execute(f"drop policy if exists tenant_isolation on {tabla}")
        op.execute(f"alter table {tabla} no force row level security")
        op.execute(f"alter table {tabla} disable row level security")
    op.drop_table("option_activations")
    op.drop_table("option_sends")
    op.drop_table("option_template_versions")
    op.drop_table("option_templates")
