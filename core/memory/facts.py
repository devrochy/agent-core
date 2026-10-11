"""Hechos del cliente con procedencia (ADR-0114 §3-4 y su adenda, HU-AOS-36).

`memory_facts` es la FUENTE de la memoria estructurada del cliente y `customers.attributes` su
proyección para los lectores existentes (el perfil del prompt, `get_customer_profile` y la
búsqueda de email de pack-calendar). Este módulo es el ÚNICO que escribe cualquiera de las dos,
siempre en la misma transacción (un test estático lo vigila).

- **Proyección:** solo el namespace `perfil` se proyecta, con claves planas, y con el mismo
  algoritmo de antes (`{**attributes, **nuevos}`; `customers.name` si llega un `name` no vacío),
  así los lectores ven exactamente lo mismo. Los demás namespaces (los datos de las tareas,
  `ventas`, `cobranza`…) viven solo en `memory_facts`: los leerá el runtime v3 filtrando por
  `readScopes` y `data_class`, nunca a ciegas en cada prompt.
- **Permisos:** `concedidos` son los `writeNamespaces` del agente que escribe. `None` = quien
  escribe no es un agente v3 (tools de los packs, runtime legacy, consolidación, importación) y
  no se chequea; con un conjunto, escribir fuera de él es `NamespaceNoConcedido`.
- **data_class** la asigna el código por namespace y clave (`clasificar`); si quien escribe
  declara otra, gana la más estricta. Los hechos nunca se embeben en `memory_vectors`.
- **Olvido:** `purgar_de_cliente` borra (lógico) sus hechos, vacía su proyección y anota
  `customers.memory_purged_at`; `vencer` es la retención (`MEMORY_RETENTION_DAYS`).
- **Consolidación:** `consolidar_tareas` pasa a hechos los datos confirmados de las tareas
  cerradas (`completed`, o `cancelled` por `episode_expired`), con su tarea y su turno.

Orden de locks: primero la fila de `customers` (`FOR NO KEY UPDATE`, compatible con los INSERT
que la referencian), después `memory_facts`. Todos los caminos de este módulo lo respetan.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import Customer, MemoryFact
from core.shared.logger import get_logger

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

log = get_logger(__name__)

PERFIL = "perfil"            # namespace que se proyecta en `customers.attributes` (reservado)
NAMESPACE_TAREAS = "tareas"  # respaldo para tareas de un procedimiento sin habilidad usable
FUENTES = frozenset({"tool", "agent", "human", "import"})
CLASES = ("public", "internal", "pii", "sensitive")  # de menos a más estricta
_NAMESPACE = re.compile(r"^[a-z_]{2,30}$")            # = CHECK de memory_facts.namespace
_MAX_CLAVE = 80                                        # = CHECK de memory_facts.key

# Raíces (sin tildes, en minúscula) que marcan la clase de un dato. Genéricas, en español e
# inglés: nada propio de un pack. La migración 0080 congela una copia para el backfill.
# Ante la duda clasifica de más (una raíz corta puede marcar `pii` una clave inocua): es el lado
# seguro. Sensibles: salud, pago y credenciales, y las categorías especiales de la Ley 1581.
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
# Claves del perfil que solo escriben las tools, el sistema o una persona (no el LLM): ids externos
# y el consentimiento del intake.
CLAVES_DEL_SISTEMA = frozenset({"crm_lead_id", "optin", "source"})
# Límites de lo que escribe el LLM (las claves y valores entran al perfil del prompt).
_CLAVE_DEL_AGENTE = re.compile(r"^[\w .-]{1,80}$")
_MAX_VALOR_BYTES = 4096
_MAX_CLAVES_POR_ESCRITURA = 20

# Clase de un dato de tarea (`core/kernel/state.py`) → clase del hecho. `payment` no pasa a
# memoria: los datos de pago nunca se guardan como recuerdo del cliente.
_CLASE_DE_SLOT = {"public": "public", "pii": "pii"}
# Actores de un dato de tarea que lo hacen confiable sin confirmación (no el LLM).
_ACTORES_CONFIABLES = frozenset({"tool", "kernel", "human", "backoffice"})
_TERMINALES_CONSOLIDABLES = "(t.status = 'completed' OR (t.status = 'cancelled' AND t.status_reason = 'episode_expired'))"


class NamespaceNoConcedido(PermissionError):
    """El agente quiso escribir en un namespace que no tiene en `writeNamespaces`."""


class HechoInvalido(ValueError):
    """Namespace, clave o fuente que el esquema de `memory_facts` no admite."""


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in sin_tildes if not unicodedata.combining(c)).lower()


def _mas_estricta(a: str, b: str | None) -> str:
    if b is None or b not in CLASES:
        return a
    return a if CLASES.index(a) >= CLASES.index(b) else b


def clasificar(namespace: str, key: str, declarada: str | None = None) -> str:
    """`data_class` de un hecho: por namespace y clave; si se declara otra, la más estricta."""
    clave = _normalizar(f"{namespace}.{key}")
    if any(r in clave for r in RAICES_SENSITIVE):
        propia = "sensitive"
    elif any(r in clave for r in RAICES_PII):
        propia = "pii"
    else:
        propia = "internal"
    return _mas_estricta(propia, declarada)


def namespace_de_procedimiento(procedure_id: str) -> str:
    """Namespace de los hechos de una tarea: la habilidad del `procedure_id`, saneada.

    Nunca `perfil`: los datos de una tarea no se proyectan en el perfil que ve cada prompt.
    """
    habilidad = re.sub(r"[^a-z_]", "", procedure_id.split(".", 1)[0].lower())[:30]
    if not _NAMESPACE.match(habilidad) or habilidad == PERFIL:
        return NAMESPACE_TAREAS
    return habilidad


def _validar_escritura_del_agente(namespace: str, valores: Mapping[str, Any], data_class: str | None) -> None:
    """Lo que escribe el LLM: claves de una línea, valores acotados, nada sensible (pago, salud,
    credenciales) y ninguna clave del sistema. `HechoInvalido` con un mensaje genérico."""
    if len(valores) > _MAX_CLAVES_POR_ESCRITURA:
        raise HechoInvalido("demasiados datos en una sola escritura")
    for clave, valor in valores.items():
        if not isinstance(clave, str) or not _CLAVE_DEL_AGENTE.match(clave) or clave in CLAVES_DEL_SISTEMA:
            raise HechoInvalido("clave no admitida")
        if len(json.dumps(valor, ensure_ascii=False, default=str).encode("utf-8")) > _MAX_VALOR_BYTES:
            raise HechoInvalido("valor demasiado grande")
        if clasificar(namespace, clave, data_class) == "sensitive":
            raise HechoInvalido("dato sensible: no se guarda en la memoria del cliente")


def _ahora(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


async def _bloquear_cliente(session: AsyncSession, tenant_id: uuid.UUID, customer_id: uuid.UUID) -> Customer | None:
    return (
        await session.execute(
            select(Customer)
            .where(Customer.tenant_id == tenant_id, Customer.id == customer_id)
            .with_for_update(key_share=True)
        )
    ).scalar_one_or_none()


async def escribir_hechos(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    namespace: str,
    valores: Mapping[str, Any],
    source: str,
    concedidos: Collection[str] | None = None,
    data_class: str | None = None,
    confidence: float | Decimal = 1,
    written_by_instance_id: uuid.UUID | None = None,
    source_turn_id: uuid.UUID | None = None,
    source_task_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Escribe hechos y su proyección en la sesión dada (no hace commit). Devuelve las claves nuevas.

    Por clave: si el vigente tiene el mismo valor y clase no se crea fila; si no, el vigente
    queda `superseded_at` y entra el nuevo. Una clave que el esquema no admite (vacía o de más
    de 80 caracteres) se descarta con un log cuando escribe el runtime legacy (`concedidos=None`,
    para no cambiar el resultado de sus tools) y es `HechoInvalido` para un agente v3.
    Cliente inexistente en el negocio: no escribe nada (como el `upsert_attributes` de antes).
    """
    if source not in FUENTES:
        raise HechoInvalido(f"source inválida: {source!r}")
    if not _NAMESPACE.match(namespace):
        raise HechoInvalido(f"namespace inválido: {namespace!r}")
    if concedidos is not None and namespace not in concedidos:
        raise NamespaceNoConcedido(f"namespace no concedido: {namespace}")

    if source == "agent":
        _validar_escritura_del_agente(namespace, valores, data_class)
    validos: dict[str, Any] = {}
    for clave, valor in valores.items():
        if isinstance(clave, str) and 1 <= len(clave) <= _MAX_CLAVE:
            validos[clave] = valor
        elif concedidos is None:
            log.warning("memory_fact_key_skipped", namespace=namespace, key_length=len(str(clave)))
        else:
            raise HechoInvalido("clave inválida: entre 1 y 80 caracteres")
    if not validos:
        return []

    cliente = await _bloquear_cliente(session, tenant_id, customer_id)
    if cliente is None:
        log.warning("memory_fact_customer_not_found", customer_id=str(customer_id))
        return []

    ahora = _ahora(now)
    vigentes = {
        f.key: f
        for f in (
            await session.execute(
                select(MemoryFact).where(
                    MemoryFact.tenant_id == tenant_id,
                    MemoryFact.customer_id == customer_id,
                    MemoryFact.namespace == namespace,
                    MemoryFact.key.in_(list(validos)),
                    MemoryFact.superseded_at.is_(None),
                    MemoryFact.deleted_at.is_(None),
                )
            )
        ).scalars()
    }
    nuevos: list[MemoryFact] = []
    for clave, valor in validos.items():
        clase = clasificar(namespace, clave, data_class)
        actual = vigentes.get(clave)
        if actual is not None and actual.value == valor and actual.data_class == clase:
            continue
        if actual is not None:
            actual.superseded_at = ahora
        nuevos.append(
            MemoryFact(
                tenant_id=tenant_id,
                customer_id=customer_id,
                namespace=namespace,
                key=clave,
                value=valor,
                data_class=clase,
                confidence=Decimal(str(confidence)),
                source=source,
                written_by_instance_id=written_by_instance_id,
                source_turn_id=source_turn_id,
                source_task_id=source_task_id,
                valid_from=ahora,
                created_at=ahora,
            )
        )
    # El vigente se cierra ANTES de insertar el nuevo (índice único parcial del hecho vigente).
    await session.flush()
    session.add_all(nuevos)

    if namespace == PERFIL:
        # Proyección: el mismo algoritmo de `upsert_attributes` antes de HU-AOS-36.
        cliente.attributes = {**(cliente.attributes or {}), **validos}
        if validos.get("name"):
            cliente.name = str(validos["name"])
    await session.flush()
    return [f.key for f in nuevos]


async def purgar_de_cliente(
    session: AsyncSession, *, tenant_id: uuid.UUID, customer_id: uuid.UUID, now: datetime | None = None
) -> int:
    """Olvido del cliente (ADR-0053 §2, ADR-0114 §4): sus hechos y su proyección. Devuelve hechos borrados.

    `customers.attributes` queda `{}`: todas sus claves son proyección (nadie más las escribe).
    `customers.name` y `contact_phone` no se tocan: son la identidad del cliente en el canal,
    como sus mensajes y sus tareas, registro del negocio.
    """
    ahora = _ahora(now)
    if await _bloquear_cliente(session, tenant_id, customer_id) is None:
        return 0
    borrados = cast(
        "CursorResult[Any]",
        await session.execute(
            update(MemoryFact)
            .where(
                MemoryFact.tenant_id == tenant_id,
                MemoryFact.customer_id == customer_id,
                MemoryFact.deleted_at.is_(None),
            )
            .values(deleted_at=ahora)
        ),
    ).rowcount
    await session.execute(
        text(
            "UPDATE customers SET attributes = '{}'::jsonb, memory_purged_at = :ahora "
            "WHERE tenant_id = :t AND id = :c"
        ),
        {"ahora": ahora, "t": tenant_id, "c": customer_id},
    )
    session.expire_all()
    return int(borrados or 0)


async def vencer(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    antes_de: datetime,
    now: datetime | None = None,
    lote: int = 200,
) -> tuple[int, list[uuid.UUID]]:
    """Retención: borra (lógico) un lote de hechos creados antes de `antes_de` y quita de la
    proyección las claves de los que estaban vigentes. Devuelve (hechos, clientes tocados);
    quien llama repite hasta que no queden (cada lote en su transacción)."""
    ahora = _ahora(now)
    clientes = list(
        (
            await session.execute(
                text(
                    "SELECT id FROM customers WHERE tenant_id = :t AND id IN ("
                    "  SELECT DISTINCT customer_id FROM memory_facts"
                    "  WHERE tenant_id = :t AND deleted_at IS NULL AND created_at < :antes LIMIT :n"
                    ") ORDER BY id FOR NO KEY UPDATE"
                ),
                {"t": tenant_id, "antes": antes_de, "n": lote},
            )
        ).scalars()
    )
    if not clientes:
        return 0, []
    filas = (
        await session.execute(
            update(MemoryFact)
            .where(
                MemoryFact.tenant_id == tenant_id,
                MemoryFact.customer_id.in_(clientes),
                MemoryFact.deleted_at.is_(None),
                MemoryFact.created_at < antes_de,
            )
            .values(deleted_at=ahora)
            .returning(MemoryFact.customer_id, MemoryFact.namespace, MemoryFact.key, MemoryFact.superseded_at)
        )
    ).all()
    claves: dict[uuid.UUID, list[str]] = {}
    for customer_id, namespace, key, superseded_at in filas:
        if namespace == PERFIL and superseded_at is None:
            claves.setdefault(customer_id, []).append(key)
    for customer_id, quitar in claves.items():
        await session.execute(
            text("UPDATE customers SET attributes = attributes - CAST(:k AS text[]) WHERE tenant_id = :t AND id = :c"),
            {"k": quitar, "t": tenant_id, "c": customer_id},
        )
    return len(filas), clientes


def _datos_consolidables(slots: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Datos de una tarea que pasan a memoria: con valor, que no sean de pago, y confirmados o
    fijados por una acción, el kernel o una persona (lo que el LLM extrajo sin confirmar no)."""
    salida: dict[str, dict[str, Any]] = {}
    for nombre, meta in slots.items():
        if not isinstance(meta, Mapping) or meta.get("value") is None:
            continue
        clase = _CLASE_DE_SLOT.get(str(meta.get("data_class")))
        if clase is None:
            continue
        if not (meta.get("confirmed") or meta.get("actor") in _ACTORES_CONFIABLES):
            continue
        salida[nombre] = {**meta, "data_class": clase}
    return salida


def _uuid_o_none(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor)) if valor else None
    except ValueError:
        return None


async def tareas_por_consolidar(
    session: AsyncSession, *, tenant_id: uuid.UUID, desde: datetime, limite: int = 500
) -> list[Any]:
    """Tareas cerradas desde `desde` sin hechos todavía (incluidos los borrados: lo olvidado no
    vuelve) y empezadas después de la última purga de memoria de su cliente."""
    return list(
        (
            await session.execute(
                text(
                    "SELECT t.id, t.procedure_id, t.slots, t.owner_instance_id, t.created_at, c.customer_id "
                    "FROM conversation_tasks t "
                    "JOIN conversations c ON c.tenant_id = t.tenant_id AND c.id = t.conversation_id "
                    "JOIN customers cu ON cu.tenant_id = c.tenant_id AND cu.id = c.customer_id "
                    f"WHERE t.tenant_id = :t AND {_TERMINALES_CONSOLIDABLES} "
                    "AND t.completed_at >= :desde "
                    "AND (cu.memory_purged_at IS NULL OR t.created_at > cu.memory_purged_at) "
                    "AND NOT EXISTS (SELECT 1 FROM memory_facts f "
                    "                WHERE f.tenant_id = t.tenant_id AND f.source_task_id = t.id) "
                    "ORDER BY t.completed_at LIMIT :n"
                ),
                {"t": tenant_id, "desde": desde, "n": limite},
            )
        ).all()
    )


async def consolidar_tarea(session: AsyncSession, *, tenant_id: uuid.UUID, tarea: Any, now: datetime | None = None) -> int:
    """Pasa a `memory_facts` los datos consolidables de UNA tarea (fila de `tareas_por_consolidar`).

    Cada dato lleva su procedencia: la tarea (`source_task_id`), su turno (`source_turn_id`),
    la instancia dueña y la fuente (`human` si lo fijó una persona; `tool` si no: el dato salió
    del motor de procedimientos, no de una escritura libre del agente).
    """
    datos = _datos_consolidables(tarea.slots or {})
    if not datos:
        return 0
    # Con el cliente bloqueado se vuelve a mirar la purga: una que se confirmó entre la lectura
    # de las tareas y esta transacción gana (lo olvidado no vuelve).
    if await _bloquear_cliente(session, tenant_id, tarea.customer_id) is None:
        return 0
    purgado = (
        await session.execute(
            text("SELECT memory_purged_at FROM customers WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": tarea.customer_id},
        )
    ).scalar_one_or_none()
    if purgado is not None and tarea.created_at <= purgado:
        return 0
    namespace = namespace_de_procedimiento(tarea.procedure_id)
    escritos = 0
    for nombre, meta in sorted(datos.items()):
        escritos += len(
            await escribir_hechos(
                session,
                tenant_id=tenant_id,
                customer_id=tarea.customer_id,
                namespace=namespace,
                valores={nombre: meta["value"]},
                source="human" if meta.get("actor") in {"human", "backoffice"} else "tool",
                data_class=meta["data_class"],
                written_by_instance_id=tarea.owner_instance_id,
                source_turn_id=_uuid_o_none(meta.get("turn_id")),
                source_task_id=tarea.id,
                now=now,
            )
        )
    return escritos


async def importar_perfil(session: AsyncSession, *, tenant_id: uuid.UUID, lote: int = 1000) -> int:
    """Copia como hechos `perfil` (`source = 'import'`) un lote de claves de `customers.attributes`
    que no tienen hecho vigente. Devuelve cuántas copió; quien llama repite hasta 0.

    Completa lo que el código anterior a HU-AOS-36 escribió en `attributes` durante el deploy
    (después del backfill de 0080). Idempotente: el índice del hecho vigente evita duplicados y
    la proyección no cambia (los valores ya están en `attributes`).
    """
    from sqlalchemy.dialects.postgresql import insert

    filas = (
        await session.execute(
            text(
                "SELECT c.id, a.key, a.value FROM customers c, jsonb_each(c.attributes) a "
                "WHERE c.tenant_id = :t AND c.deleted_at IS NULL AND jsonb_typeof(c.attributes) = 'object' "
                "AND length(a.key) BETWEEN 1 AND 80 AND NOT EXISTS ("
                "  SELECT 1 FROM memory_facts f WHERE f.tenant_id = c.tenant_id AND f.customer_id = c.id "
                "  AND f.namespace = 'perfil' AND f.key = a.key "
                "  AND f.superseded_at IS NULL AND f.deleted_at IS NULL) "
                "LIMIT :n"
            ),
            {"t": tenant_id, "n": lote},
        )
    ).all()
    if not filas:
        return 0
    await session.execute(
        insert(MemoryFact)
        .values(
            [
                {
                    "tenant_id": tenant_id,
                    "customer_id": customer_id,
                    "namespace": PERFIL,
                    "key": key,
                    "value": value,
                    "data_class": clasificar(PERFIL, key),
                    "source": "import",
                }
                for customer_id, key, value in filas
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "customer_id", "namespace", "key"],
            index_where=text("superseded_at IS NULL AND deleted_at IS NULL"),
        )
    )
    return len(filas)
