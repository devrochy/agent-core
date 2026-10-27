# ADR-0060 — Alineación del pack-cobranza al modelo de dominio de app-cobranza (Propietario/Gestor/Cartera)

- **Estado:** aceptado — «cobros al propietario» y el hilo con el propietario se retiraron de la API y del admin el 2026-10-26/27 (ver ADR-0064); sus tablas se conservan
- **Fecha:** 2026-09-24

## Contexto

El pack-cobranza se diseñó mirando la rama `main` de `app-cobranza`, que usa el
vocabulario viejo (`socio`, `cobrador`, `ruta`). La rama **`develop`** (142
commits por delante) hizo una migración *breaking* de vocabulario —documentada en
`app-cobranza-admin/docs/glosario.md`— y amplió el dominio a ~40 entidades
(cartera_config, caja, inyecciones, gastos, abonos, permisos, cobros al
propietario, dispositivos/offline). Si el pack se queda en el vocabulario viejo,
diverge del producto real y del panel.

## Decisión

El pack **se alinea al modelo de `develop`** con un **mapeo de compatibilidad
1:1** (mismo concepto, mismo nombre), aplicado a tablas, campos y tools. El
mapeo es:

| Modelo viejo (main) | Modelo nuevo (develop) → pack |
|---|---|
| `socios`, `socio_id` | `propietarios`, `propietario_id` |
| `socio_permisos` | `propietario_permisos` (23 keys) |
| `cobradores`, `cobrador_id` | `gestores`, `gestor_id` |
| `cobrador_permisos` | `gestor_permisos` (14 keys) |
| `rutas`, `ruta_id` | `carteras`, `cartera_id` |
| `ruta_config` | `cartera_config` (incl. flag `ruta_habilitada`) |
| `ruta_notas` | `cartera_notas` |
| `cobros_socio` | `cobros_propietario` |
| `conversaciones_socio` | `conversaciones_propietario` |
| "ruta" (feature) | se mantiene solo como *feature* (`ruta_habilitada`), nunca como entidad |

Reglas del mapeo:
1. **La entidad principal es `Cartera`** (no "ruta"). "Ruta" solo se admite como
   feature ("ruta habilitada", "trayecto diario").
2. Toda mención de "socio" → **propietario**; "cobrador" → **gestor**.
3. "Plan de pagos" = cuotas de un préstamo con sus pagos/abonos (no confundir con
   `cobros_propietario`).
4. El pack **no importa** `app-cobranza` (invariante nº5): replica el modelo, no
   el código.

Alcance v1 del pack: identidad+permisos, cartera+cartera_config, caja/ajustes,
inyecciones, gastos+evidencias, clientes/préstamos/cuotas/pagos/abonos/visitas,
promesas, reglas de negociación y liquidaciones. Quedan para v2 (Épicas 8/9/10):
dispositivos/offline, cobros al propietario y conversaciones propietario.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener el vocabulario viejo | Diverge del producto real y del panel de `develop`; deuda inmediata |
| Capa de traducción (viejo en el pack, nuevo en la API) | Dos vocabularios en el mismo sistema; confusión y bugs |
| Portar `app-cobranza` entero de una vez | Incluye Épicas 8/9/10 (dispositivos/offline/cobros) fuera del alcance v1 |

## Consecuencias

**Positivas:** el pack habla el mismo idioma que el producto y el panel; el mapeo
queda documentado y auditable; el alta y las tools usan los nombres reales.

**Negativas / lo que aceptamos pagar:** renombrar la migración y los modelos ya
creados (greenfield, sin datos, por lo que se reescriben en sitio); las tools del
asistente cambian de nombre interno (`cob_consultar_saldo` se mantiene, pero las
entidades referencian `cartera`).

**Qué haría que revisáramos esto:** que `app-cobranza` vuelva a renombrar el
dominio, o que el pack necesite compatibilidad con datos del modelo viejo.

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/models.py`, migraciones, skills, jobs,
  providers, module.
- ADR-0059 (vocabulario corregido), ADR-0056/0057/0058 (sin cambios).
- `docs/MULTITENANT.md` (si menciona el pack).
