# ADR-0171 — Resumen por agente y KPIs de módulo

- **Estado:** propuesto (se acepta al mergear el PR de este ADR).
- **Fecha:** 2026-10-27
- **Contexto:** el Resumen del panel y de la app mostraba 4 cifras del negocio entero, sin enlace ni comparación. El dueño pidió ver los KPIs del agente general y de cada agente especialista, con los datos del módulo que gestiona, y poder entrar al detalle desde cada dato.
- **Relaciona:**
  - ADR-0140 (Mi agente y su data: la atribución por agente y lo que cuenta como actividad);
  - ADR-0136 (permisos por sección);
  - ADR-0056 y ADR-0153 (`lead_registry`: el patrón de datos de pack por agente sin que el core los conozca);
  - ADR-0155 (el enfoque de un agente sale de su plantilla);
  - HU-DS-07 (Resumen por rol): esta decisión no depende de HU-UI-07.

## Decisión

1. **Un endpoint, `GET /api/v1/admin/dashboard/agents`** (sección `dashboard`, sin tablas ni migración). Devuelve `general` (KPIs del negocio, lo que gobierna el Coordinador) y `agentes[]` (sin el Coordinador), cada uno con sus `kpis` y sus `modulos`.
2. **Cada KPI trae su comparación.** `valor` de los últimos 7 días, `previo` de los 7 días anteriores y `delta_pct`; las fotos de hoy (escalaciones pendientes, tareas abiertas) van sin `previo`. `valor` es `null` cuando no hay datos (las tasas bajo la muestra mínima de fiabilidad) y la UI muestra «—». Los KPIs mensuales de Suscripciones comparan con el mes anterior y lo dicen en su `hint`.
3. **Atribución idéntica a «Mi agente y su data».** Turnos, conversaciones y costo salen de `usage_ledger.agent_instance_id` y el General absorbe lo no atribuido, para que la cifra del Resumen coincida con el detalle al que enlaza. Los escalados por agente salen de `turn_outcomes` y solo existen con runtime v3 (en `legacy` se omiten).
4. **El módulo lo calcula el pack.** El core no conoce las tablas de los packs: cada pack registra un `ResumenProvider` en `core/admin/resumen_registry.py` (mismo patrón que `lead_registry`) con su `agent_key` (la clave de la plantilla: `ventas`, `agenda`, `cobranza`, `suscripciones`), la sección de permisos que exige y una función que devuelve los KPIs de un período. El agente cuya `key` coincide recibe el módulo; los módulos que ningún agente cubre cuelgan del General (cubre el runtime `legacy`, donde un solo agente atiende todo). Los datos son **del módulo completo**, no solo lo que creó el agente: `agent_record_origins` registra quién creó cada registro, no quién cobró, atendió ni facturó.
5. **Enlaces semánticos.** `destino = {ruta, params}` con rutas lógicas (`conversaciones`, `escalaciones`, `costos`, `agente`, `comercio.pedidos`, `calendario.citas`, `cobranza.pagos|carteras|promesas`, `suscripciones[.lista|.pagos]`, `leads`). El backend no conoce las URLs del panel ni de la app: cada cliente traduce la ruta a su navegación y, si no tiene pantalla, deja el KPI sin enlace.
6. **Permisos por KPI.** Cada KPI se omite (no sale en cero) si falta la sección que lo respalda: `conversations`, `escalations`, `costs`, `agents` y, para cada módulo, su sección (`pack_cobranza:resumen`, `pack_calendar:citas`, `pack_subscriptions:suscripciones`, `pack_commerce`). Quien solo tiene `dashboard` recibe listas vacías, no un 403.
7. **Un módulo que falla no tumba el Resumen.** Se captura por proveedor y sale como `modulo.error`; el resto de los KPIs sigue.

## Consecuencias

- Los packs que sumen un agente con módulo propio (p. ej. Inmobiliario) registran su `ResumenProvider`; no hay que tocar el core.
- Cada carga del Resumen ejecuta las consultas de los módulos habilitados. Si el costo molesta con datos reales, se cachea por negocio unos segundos; no antes de medir.
- pass^k es por negocio/conexión, no por agente: el KPI «Consistencia» está en el bloque general y enlaza a la pestaña de calidad.
- El contrato nuevo se refleja en `docs/openapi.json`; el panel y la app regeneran sus tipos.
