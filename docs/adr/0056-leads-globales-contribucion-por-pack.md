# ADR-0056 — Leads globales con contribuciones por pack (`LeadProvider`)

- **Estado:** aceptado
- **Fecha:** 2026-09-24

## Contexto

ADR-0022 decidió que **todo `Customer` es un lead** y que la vista de leads se
calcula en vivo. Pero esa vista vive **dentro de `pack-commerce`**
(`_leads_list`), lo que ata el concepto "lead" a un pack. Con varios packs
(comercio, cobranza, intake público de ADR-0055) cada uno tiene señales propias
(pedidos vs. saldo/mora) y se quiere una vista **global** (todos los que
interactuaron) más columnas **por pack**, sin que el core importe packs
(invariante nº5) ni se duplique la vista por pack.

## Decisión

El **core** posee la vista de leads (`/admin/leads`, sección `leads`), computada
de `Customer` + `Conversation` (ADR-0022). Los packs registran un
**`LeadProvider`** en `core/admin/lead_registry.py`:

```python
lead_registry.register(
    "pack_cobranza",
    columns=[LeadColumn(key="saldo", label="Saldo"), LeadColumn(key="mora", label="Mora")],
    resolver=async def (customer_ids) -> dict[customer_id, {key: value}],
)
```

La vista global muestra las columnas del core **más** las de los packs
habilitados para el tenant (`AgentSpec.packs`). Cada pack conserva además su
propia vista (el subconjunto de su dominio) bajo su sección.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener la vista en `pack-commerce` | Ata el lead a un pack; no escala a N packs |
| Tabla única de leads con `source`/`pack` | Reintroduce el bug de ADR-0022 (fila que hay que crear/sincronizar) |
| El core importa los packs para armar la vista | Viola el invariante nº5 (los packs no se conocen entre sí) |
| Poblar una tabla de leads por eventos | Vuelve a "algo tiene que disparar la creación del lead" |

## Consecuencias

**Positivas:** un único concepto de lead global; columnas por pack sin acoplar;
comercio, cobranza y el intake público (ADR-0055) alimentan la misma vista.

**Negativas / lo que aceptamos pagar:** la vista global ejecuta N resolvers por
render (paginado); un provider lento degrada el panel → cachear y acotar; nueva
sección `leads` en RBAC y menú.

**Qué haría que revisáramos esto:** que un pack necesite joins pesados en SQL que
el resolver no exprese bien (→ permitir SQL contribuido) o que la vista global se
vuelva lenta con más de ~5 packs (→ vista materializada).

## Impacto en el código

- Nuevo: `core/admin/lead_registry.py`.
- Modificados: `core/admin/permissions.py` (sección `leads`), `core/admin/context.py`,
  `core/admin/views.py` (vista global), `core/admin/templates/base.html` (menú).
- `packages/pack-commerce/pack_commerce/module.py` (migrar `_leads_list` a un provider).
- `packages/pack-cobranza/pack_cobranza/providers.py` (registro del provider de cobranza).
