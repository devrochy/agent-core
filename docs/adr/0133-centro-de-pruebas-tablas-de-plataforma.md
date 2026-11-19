# ADR-0133 — Centro de pruebas: tablas de plataforma, ejecución por tester y capturas en TENANTS_ROOT

- **Estado:** aceptado (dueño, 2026-10-13)
- **Fecha:** 2026-10-13
- **Contexto:** SPEC-30 de solvershub-docs (D-33; HU-QA-01 y HU-QA-02). **Usa:** ADR-0012 (tablas de sistema),
  ADR-0047 (RBAC por sección), ADR-0098 (correo de sistema).

## Contexto

- Las pruebas manuales de cada release vivían en un artifact externo, sin capturas, sin informe por persona y
  solo editable por gente de la organización en claude.ai.
- El dueño pidió un centro de pruebas en el panel. Cada tester ejecuta paso a paso, adjunta capturas, ve su
  avance y envía un informe; el dueño consolida por ciclo (D-SPEC-30-4 y 5).
- El catálogo de pruebas se versiona con git en `solvershub-docs/11-pruebas/catalogo.yaml`, junto a la HU que lo
  pide (D-SPEC-30-1).
- Las pruebas son de la plataforma, no de un negocio (D-SPEC-30-3).

## Decisión

1. **Nueve tablas de SISTEMA sin RLS** (migración 0087):
   - `qa_catalog_versions` y `qa_items`: catálogo importado;
   - `qa_cycles`: ciclos, con un índice parcial que deja un solo ciclo abierto;
   - `qa_runs`: una ejecución por tester y ciclo;
   - `qa_step_results` y `qa_item_results`: resultados por paso y por prueba;
   - `qa_attachments`: capturas;
   - `qa_config_status`: configuración de test y de producción, que no se reinicia por ciclo;
   - `qa_events`: historial; `app_runtime` solo puede insertar y leer.

   No llevan `tenant_id` porque no pertenecen a ningún negocio. Ninguna query de negocio las toca. Los grants
   son mínimos por tabla, con `revoke all` previo, porque los default privileges de la 0003 dan todo a
   `app_runtime`.
2. **Sección RBAC `qa`, global** (`GLOBAL_ONLY_SECTIONS`). No se hereda del rol en un negocio: la tiene el
   master, o un permiso explícito. El rol tester se decide en su propio ADR (HU-QA-04). Importar el catálogo es
   solo del master.
3. **Resultados por slug.** Se guardan por `item_slug`, el `id` estable del YAML, no por la fila del catálogo.
   Al importar una versión nueva, el ciclo abierto pasa a usarla sin perder lo marcado, y los ciclos cerrados
   conservan su versión y su foto (`qa_cycles.resumen`).
4. **Importación idempotente por contenido.** `contenido_sha256` es único: importar el mismo archivo dos veces no
   duplica nada. Entra por `POST /api/v1/admin/qa/catalog/import` o por `agent-core qa importar`. El parser
   (`core/qa/catalogo.py`) aplica las mismas reglas que el CI de docs, salvo la existencia de las HU.
5. **Escritura solo sobre la ejecución propia.** Se escribe sobre la propia ejecución, en el ciclo abierto y
   mientras esté `en_curso`. Enviada, queda congelada con su informe en `qa_runs.informe`, que no cambia aunque se
   importe otro catálogo. El master lee todas las ejecuciones; el resto, solo las suyas. El historial de una
   prueba sigue la misma regla: a quien no es master solo le muestra su ejecución y los eventos de ciclo y de
   configuración.
   - **Cerrar el ciclo es solo del master**, porque congela las ejecuciones en curso de todos. El rol tester lo
     suma en su ADR.
   - **La configuración de test y de producción** (`qa_config_status`) la marca cualquiera con escritura en `qa`:
     es un checklist compartido del entorno.
   - **La vista de ciclos** muestra el progreso de cada tester con su nombre, pero sin sus notas. Es la vista
     consolidada del equipo.
6. **Capturas en el volumen `TENANTS_ROOT`**, bajo `.plataforma/pruebas/<run_id>/`. Es el mismo volumen
   persistente y respaldado que guarda las evidencias de cobranza; el punto inicial no choca con ningún slug de
   negocio.
   - Formatos jpeg, png, webp o pdf, con el tipo confirmado por los primeros bytes; hasta 5 MB; hasta 10 por paso.
   - El nombre físico es `uuid4`; no se guardan blobs en Postgres.
   - Se sirven solo con sesión, con `nosniff` y `CSP: sandbox`.
7. **Aviso por correo de sistema.** Al enviar un informe se avisa a los masters activos con el correo de sistema
   (ADR-0098), con el enlace `PANEL_BASE_URL/pruebas/informes/<id>`. Si el correo falla, el informe ya quedó
   guardado.

## Consecuencias

- El auditor multitenant tiene que confirmar en cada revisión que ninguna query de negocio lee `qa_*`.
- Las capturas crecen con cada ciclo. Su limpieza queda como punto de producción del catálogo
  (`prod-limpieza-de-capturas-de-pruebas`).
- El panel consume la API con tipos generados del OpenAPI (HU-QA-03).
