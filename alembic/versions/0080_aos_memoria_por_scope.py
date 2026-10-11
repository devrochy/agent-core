"""Agent OS · memoria por scope con procedencia y olvido (HU-AOS-36, ADR-0114 §3-4 y su adenda).

Lo que no entró en el expand de HU-AOS-02 (diseño §4.3 bloque 11), más lo que necesitan la
procedencia y el olvido:

- `memory_vectors`: `scope`, `connection_id`, `agent_id`, `kind`, `importance`, `expires_at`,
  `embedding_model`, `source_turn_id`, los CHECK `ck_mv_scope`, `ck_mv_kind`, `ck_mv_keys` y las
  FKs compuestas de `connection_id`, `agent_id` y `source_turn_id` (`NOT VALID`: se aplican a las
  filas nuevas; el `VALIDATE` va con el contract, como en 0070).
  El loop guarda cada intercambio sin `customer_id`; desde esta HU `store_memory` lo resuelve
  desde la conversación, así que esos INSERT cumplen `ck_mv_keys`. Las filas viejas se completan
  con el backfill de abajo.
- `memory_facts.source_task_id` (+ FK compuesta a `conversation_tasks` e índice): de qué tarea
  salió un hecho (procedencia) y marca de idempotencia de la consolidación.
- `customers.memory_purged_at`: hora de la última purga de memoria del cliente. La consolidación
  no pasa a memoria datos de tareas empezadas antes (si no, una tarea cerrada después del
  "olvidame" resucitaría lo olvidado). No se mapea en el modelo `Customer`: el runtime la lee y
  escribe con SQL, así ningún SELECT del consumer depende de esta migración.
- Índices concurrentes: `ix_memory_vectors_scope` (diseño §4.3) e `ix_conversation_tasks_closed`
  (la consolidación busca tareas cerradas por fecha, sin recorrer todas las del negocio).

Backfills (fuera de toda transacción con DDL, en lotes commiteados, re-ejecutables):
1. `memory_vectors`: `customer_id` desde la conversación (mismo negocio), `connection_id` solo si
   la conexión existe en `channel_connections` del mismo negocio, y `kind`: `fact` si la fila ya
   traía cliente (se recordaba y sigue recordándose), `exchange` si no (el loop la guardó sin
   cliente: nunca se recordó y sigue fuera del recall). Las filas sin cliente resoluble pasan a
   `scope = 'tenant'` borradas (lógico): no las alcanzaba ningún recall ni purga. Va por cursor de
   id. `kind` se agrega sin default, se completa
   y recién entonces pasa a `DEFAULT 'exchange'` y `NOT NULL` (sin escaneo bloqueante).
   Ventana del deploy: mientras corre el código anterior, sus intercambios sin cliente chocan con
   `ck_mv_keys` y no se guardan (el error ya se atrapaba y solo se registraba; nunca se recordaban),
   y lo que guarde con cliente después del `DEFAULT` queda `exchange` (fuera del recall).
2. `memory_facts` desde `customers.attributes` de los clientes vivos (namespace `perfil`,
   `source = 'import'`) y la `data_class` por las raíces de clave congeladas aquí (las mismas que
   `core/memory/facts.py`; un test lo comprueba). `valid_from`/`created_at` = la hora de la
   migración: la edad real de cada clave no se conoce, y con la del cliente la primera retención
   borraría el perfil de clientes viejos pero activos. Claves de más de 80 caracteres se saltan
   (el CHECK de `memory_facts.key` no las admite). `ON CONFLICT DO NOTHING` sobre el hecho vigente.
   Lo que el código anterior escriba en `attributes` durante el deploy queda sin hecho: se
   completa después con `agent-core tenant memoria-importar-perfil` (idempotente).

Después de migrar conviene `VACUUM (ANALYZE) memory_vectors`: el backfill reescribe sus filas.

Con RLS forzada, un UPDATE/INSERT sin `BYPASSRLS` no falla: toca 0 filas. Por eso se exige el
rol dueño (como 0063).

Locks: cada tabla se altera en su propia transacción corta con `lock_timeout` de 5 s
(`customers` sola); los backfills no toman locks de tabla.

Revision ID: 0080
Revises: 0076
Create Date: 2026-10-11
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0080"
down_revision = "0076"
branch_labels = None
depends_on = None

_PUEDE_SALTAR_RLS = "select rolsuper or rolbypassrls from pg_roles where rolname = current_user"
_LOTE = 5000
_LOTE_CLIENTES = 1000

# Raíces de clave → data_class, congeladas el 2026-10-11 (copia de `core/memory/facts.py`; un test
# comprueba que este CASE y `clasificar` coinciden). Sin tildes: la clave se normaliza antes.
RAICES_SENSITIVE = (
    "salud", "health", "diagnost", "medic", "enfermedad", "alergi", "embaraz", "discapacidad",
    "tarjeta", "card", "cvv", "banco", "bank", "iban", "cuenta", "account", "nequi", "daviplata",
    "pago", "payment", "password", "contrasena", "clave", "otp", "secret", "token",
    "religion", "sexual", "politic", "etnia", "sindic", "biometr", "huella",
)
RAICES_PII = (
    "name", "nombre", "apellido", "email", "correo", "mail", "phone", "telefono", "tel",
    "celular", "cel", "movil", "whatsapp", "address", "direccion", "ciudad", "city",
    "municip", "barrio", "postal", "document", "doc", "cedula", "dni", "nit", "rut", "pasaporte",
    "passport", "nacimiento", "birth", "edad", "genero",
)
# Tildes y eñe → letra base, ANTES de `lower` (con locale C, `lower` no baja `Á`). Igual que el
# `unicodedata.normalize('NFKD')` de `clasificar` para el alfabeto del español.
_CON_TILDE = "ÁÉÍÓÚÜÑÀÈÌÒÙáéíóúüñàèìòù"
_SIN_TILDE = "AEIOUUNAEIOUaeiouunaeiou"
CLAVE_NORMALIZADA = f"lower(translate(a.key, '{_CON_TILDE}', '{_SIN_TILDE}'))"


def _case_data_class() -> str:
    def _alguna(raices: tuple[str, ...]) -> str:
        return " OR ".join(f"position('{r}' in 'perfil.' || {CLAVE_NORMALIZADA}) > 0" for r in raices)

    return f"CASE WHEN {_alguna(RAICES_SENSITIVE)} THEN 'sensitive' WHEN {_alguna(RAICES_PII)} THEN 'pii' ELSE 'internal' END"


def _alter(tabla: str, columnas: list[str], constraints: dict[str, str]) -> str:
    """Un `DO` (una transacción) que agrega columnas y constraints a UNA tabla, idempotente."""
    sentencias = []
    if columnas:
        adds = ",\n  ".join(f"ADD COLUMN IF NOT EXISTS {c}" for c in columnas)
        sentencias.append(f"ALTER TABLE {tabla}\n  {adds};")
    for nombre, definicion in constraints.items():
        sentencias.append(
            f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{nombre}' "
            f"AND conrelid = 'public.{tabla}'::regclass) THEN\n"
            f"  ALTER TABLE {tabla} ADD CONSTRAINT {nombre} {definicion};\nEND IF;"
        )
    return "DO $$\nBEGIN\n" + "\n".join(sentencias) + "\nEND $$;"


_CAMBIOS: list[tuple[str, list[str], dict[str, str]]] = [
    (
        "memory_vectors",
        [
            "scope text NOT NULL DEFAULT 'customer'",
            "connection_id uuid",
            "agent_id uuid",  # NULL = compartida; valor = privada de ese agente
            # Sin default al agregarla: el backfill decide la clase de las filas viejas (y de las
            # que el código anterior siga insertando mientras corre). Después: DEFAULT y NOT NULL.
            "kind text",
            "importance smallint NOT NULL DEFAULT 1",
            "expires_at timestamptz",
            "embedding_model text",
            "source_turn_id uuid",
        ],
        {
            "ck_mv_scope": "CHECK (scope IN ('customer','connection','tenant')) NOT VALID",
            "ck_mv_kind": "CHECK (kind IN ('exchange','episode','fact','document')) NOT VALID",
            "ck_mv_keys": "CHECK ((scope = 'customer' AND customer_id IS NOT NULL) "
            "OR (scope = 'connection' AND connection_id IS NOT NULL) "
            "OR (scope = 'tenant')) NOT VALID",
            # Referencias del mismo negocio (columnas nuevas: ninguna fila vieja las tiene; el
            # backfill solo copia conexiones existentes del negocio). VALIDATE con el contract.
            "fk_mv_connection": "FOREIGN KEY (tenant_id, connection_id) "
            "REFERENCES channel_connections (tenant_id, id) NOT VALID",
            "fk_mv_agent": "FOREIGN KEY (tenant_id, agent_id) REFERENCES agents (tenant_id, id) NOT VALID",
            "fk_mv_source_turn": "FOREIGN KEY (tenant_id, source_turn_id) REFERENCES core_decisions (tenant_id, id) "
            "DEFERRABLE INITIALLY DEFERRED NOT VALID",
        },
    ),
    (
        # Tabla nueva (0069) y sin escrituras hasta esta HU: la FK nace validada.
        "memory_facts",
        ["source_task_id uuid"],
        {
            "fk_memory_facts_source_task": "FOREIGN KEY (tenant_id, source_task_id) "
            "REFERENCES conversation_tasks (tenant_id, id)",
        },
    ),
    ("customers", ["memory_purged_at timestamptz"], {}),
]

_IX_FACTS_TASK = (
    "CREATE INDEX IF NOT EXISTS ix_memory_facts_task ON memory_facts (tenant_id, source_task_id) "
    "WHERE source_task_id IS NOT NULL"
)

# nombre → definición después de `ON`
_INDICES = {
    "ix_memory_vectors_scope": "memory_vectors (tenant_id, scope, connection_id, customer_id) "
    "WHERE deleted_at IS NULL",
    "ix_conversation_tasks_closed": "conversation_tasks (tenant_id, completed_at) "
    "WHERE status IN ('completed','cancelled')",
}

# Un lote en orden de id desde `:desde` (cursor: no se recorre la tabla entera en cada vuelta).
# Las filas sin cliente resoluble (sin conversación, o de una conversación que no es del negocio)
# no pueden quedar `scope = 'customer'` (`ck_mv_keys`): pasan a `scope = 'tenant'` y se borran
# (lógico). Nunca se recordaban (el recall filtra por cliente) y ninguna purga las alcanzaba.
_BACKFILL_VECTORES = f"""
WITH lote AS (
  SELECT v.id, c.customer_id AS cliente, cc.id AS conexion
  FROM memory_vectors v
  LEFT JOIN conversations c ON c.tenant_id = v.tenant_id AND c.id = v.conversation_id
  LEFT JOIN channel_connections cc ON cc.tenant_id = c.tenant_id AND cc.id = c.connection_id
  WHERE v.id > :desde
    AND (v.kind IS NULL
         OR (v.customer_id IS NULL AND v.scope = 'customer')
         OR (v.connection_id IS NULL AND cc.id IS NOT NULL))
  ORDER BY v.id
  LIMIT {_LOTE}
), cambio AS (
  UPDATE memory_vectors mv
  SET customer_id   = coalesce(mv.customer_id, lote.cliente),
      connection_id = coalesce(mv.connection_id, lote.conexion),
      -- Con el cliente que traía la fila (el código anterior): lo guardado con cliente explícito
      -- se recordaba y sigue recordándose (`fact`); lo que el loop guardaba sin cliente nunca se
      -- recordó y queda `exchange`, fuera del recall.
      kind          = coalesce(mv.kind, CASE WHEN mv.customer_id IS NOT NULL THEN 'fact' ELSE 'exchange' END),
      scope         = CASE WHEN coalesce(mv.customer_id, lote.cliente) IS NULL AND mv.scope = 'customer'
                           THEN 'tenant' ELSE mv.scope END,
      deleted_at    = CASE WHEN coalesce(mv.customer_id, lote.cliente) IS NULL AND mv.scope = 'customer'
                           THEN coalesce(mv.deleted_at, now()) ELSE mv.deleted_at END
  FROM lote
  WHERE mv.id = lote.id
  RETURNING mv.id
)
SELECT max(id::text)::uuid FROM cambio
"""

# `kind` pasa a NOT NULL sin escanear bajo ACCESS EXCLUSIVE: CHECK NOT VALID → VALIDATE (no frena
# escrituras) → SET NOT NULL (usa el CHECK validado) → se suelta el CHECK auxiliar.
_KIND_NOT_NULL = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mv_kind_nn' "
    "AND conrelid = 'public.memory_vectors'::regclass) THEN "
    "ALTER TABLE memory_vectors ADD CONSTRAINT ck_mv_kind_nn CHECK (kind IS NOT NULL) NOT VALID; "
    "END IF; END $$;",
    "ALTER TABLE memory_vectors VALIDATE CONSTRAINT ck_mv_kind_nn",
    "ALTER TABLE memory_vectors ALTER COLUMN kind SET NOT NULL",
    "ALTER TABLE memory_vectors DROP CONSTRAINT ck_mv_kind_nn",
)

_CLIENTES_CON_ATRIBUTOS = """
SELECT id FROM customers
WHERE id > :desde AND deleted_at IS NULL
  AND jsonb_typeof(attributes) = 'object' AND attributes <> '{}'::jsonb
ORDER BY id LIMIT :n
"""

_BACKFILL_HECHOS = f"""
INSERT INTO memory_facts (tenant_id, customer_id, namespace, key, value, data_class, source,
                          confidence, valid_from, created_at)
SELECT c.tenant_id, c.id, 'perfil', a.key, a.value, {_case_data_class()}, 'import', 1.00,
       now(), now()
FROM customers c, jsonb_each(c.attributes) a
WHERE c.id = ANY(:ids) AND length(a.key) BETWEEN 1 AND 80
ON CONFLICT (tenant_id, customer_id, namespace, key)
  WHERE superseded_at IS NULL AND deleted_at IS NULL
DO NOTHING
"""


def _indice_concurrente(nombre: str, definicion: str) -> None:
    valido = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
            {"nombre": nombre},
        )
        .scalar()
    )
    if valido:
        definicion_real = (
            op.get_bind()
            .exec_driver_sql(
                "SELECT pg_get_indexdef(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = %(nombre)s",
                {"nombre": nombre},
            )
            .scalar()
        )
        columnas = definicion[definicion.index("(") : definicion.index(")") + 1]
        if columnas not in str(definicion_real):
            raise RuntimeError(f"Ya existe un índice {nombre} distinto del esperado: {definicion_real}")
        return
    if valido is False:  # quedó INVALID de un CONCURRENTLY interrumpido
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
    op.execute(f"CREATE INDEX CONCURRENTLY {nombre} ON {definicion}")


def _backfill_vectores() -> None:
    bind = op.get_bind()
    desde: uuid.UUID | None = uuid.UUID(int=0)
    while desde is not None:
        desde = bind.execute(sa.text(_BACKFILL_VECTORES), {"desde": desde}).scalar()


def _backfill_hechos() -> None:
    bind = op.get_bind()
    desde = uuid.UUID(int=0)
    while True:
        ids = list(
            bind.execute(sa.text(_CLIENTES_CON_ATRIBUTOS), {"desde": desde, "n": _LOTE_CLIENTES}).scalars()
        )
        if not ids:
            return
        bind.execute(sa.text(_BACKFILL_HECHOS).bindparams(sa.bindparam("ids", type_=sa.ARRAY(sa.Uuid))), {"ids": ids})
        desde = ids[-1]


def upgrade() -> None:
    bind = op.get_bind()
    if not bind.execute(sa.text(_PUEDE_SALTAR_RLS)).scalar():
        raise RuntimeError(
            "0080: el rol de migraciones no es superusuario ni tiene BYPASSRLS. Los backfills de "
            "`memory_vectors` y `memory_facts` tocarían 0 filas por la RLS forzada. Corre la "
            "migración con el dueño de la base."
        )
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '5s'")
        for tabla, columnas, constraints in _CAMBIOS:
            op.execute(_alter(tabla, columnas, constraints))
        op.execute(_IX_FACTS_TASK)  # tabla nueva y chica: sin CONCURRENTLY
        op.execute("RESET lock_timeout")
        _backfill_vectores()
        op.execute("SET lock_timeout = '5s'")
        op.execute("ALTER TABLE memory_vectors ALTER COLUMN kind SET DEFAULT 'exchange'")
        op.execute("RESET lock_timeout")
        _backfill_vectores()  # lo que el código anterior insertó mientras tanto
        op.execute("SET lock_timeout = '5s'")
        for sentencia in _KIND_NOT_NULL:
            op.execute(sentencia)
        op.execute("RESET lock_timeout")
        _backfill_hechos()
        for nombre, definicion in _INDICES.items():
            _indice_concurrente(nombre, definicion)


def downgrade() -> None:
    # Los hechos importados del backfill quedan (son copia de `customers.attributes`, que no se
    # tocó); los valores de `customer_id`/`connection_id` completados en `memory_vectors` también.
    with op.get_context().autocommit_block():
        for nombre in reversed(list(_INDICES)):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {nombre}")
        op.execute("SET lock_timeout = '5s'")
        op.execute("DROP INDEX IF EXISTS ix_memory_facts_task")
        for tabla, columnas, constraints in reversed(_CAMBIOS):
            partes = [f"DROP CONSTRAINT IF EXISTS {n}" for n in reversed(list(constraints))]
            partes += [f"DROP COLUMN IF EXISTS {c.split()[0]}" for c in reversed(columnas)]
            op.execute(f"ALTER TABLE {tabla} " + ", ".join(partes))
        op.execute("RESET lock_timeout")
