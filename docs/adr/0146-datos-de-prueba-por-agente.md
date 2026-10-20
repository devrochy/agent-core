# ADR-0146 — Datos de prueba por agente: generados con el LLM según el negocio, con plantilla de respaldo y retiro lógico

- **Estado:** aceptado (HU-QA-10, SPEC-32, D-39). El dueño aprobó la spec y pidió avanzar el 2026-10-20.
- **Fecha:** 2026-10-20
- **Contexto:** SPEC-32 (`spec.md` y `design.md`) en `solvershub-docs`. Cierra D-SPEC-32-3.
  **Usa:** ADR-0083/0110 (seed de packs), ADR-0124 (hooks opcionales de pack con contrato en el core),
  ADR-0135 (datos de prueba de los testers y `QA_TESTERS_ENABLED`), ADR-0144 (créditos en
  `usage_ledger`), ADR-0014 (playbooks), ADR-0140 (`agent_record_origins`).

## Contexto

Para probar un agente en test solo existe «Preparar mis datos» (ADR-0135), que siembra un juego fijo e
igual para cualquier giro. El dueño pidió un botón en la ficha de cada agente que cargue datos
parecidos al negocio descrito, para arrancar la prueba del agente en minutos. Eligió (D-39): generar con
el LLM y caer a una plantilla si falla, solo en test, y dejar la importación de datos reales para una
ola 2.

## Decisión

1. **Contrato pack ↔ core.** Tres hooks opcionales nuevos en `BaseModule`, con tipos en
   `core/qa/contrato_datos.py` (como `ContenidoCargado`, ADR-0124; no toca `agentcore_contracts`):
   - `formas_de_datos_de_prueba()` → `FormaDeDatos`: `modo`, `habilidad` del agente que la usa,
     `prioridad`, `esquema` pydantic (valida al LLM y es el formato que ve) y `respaldo` (fragmento
     `presets/*.json` del pack, válido contra el esquema).
   - `sembrar_datos_de_prueba(tenant, modo, datos, contexto)` → registros **creados** (`kind`, `ref`).
     Lo que ya existía y se reutilizó no se devuelve: no es de la prueba.
   - `retirar_datos_de_prueba(tenant, registros)`: borrado lógico o desactivación (invariante 8).
   Los `seed.py` de commerce, calendar y cobranza aceptan un `creados` opcional; su uso por presets y CLI
   no cambia (salvo que commerce ya no reutiliza una categoría con `deleted_at`).
2. **Formas de la primera ola.** commerce: `catalogo` (habilidad `pack_commerce`, prioridad 30) y
   `planes` (sin habilidad: los planes son productos de «Planes», D-16). calendar: `agenda` (10).
   cobranza: `cartera` (10). Gana la menor prioridad entre las habilidades del agente (de su borrador
   o su última versión): la agenda sobre el comercio que usa solo para leads. El General, el
   Coordinador y los agentes sin forma muestran el motivo y no generan.
   - Una plantilla de agente puede nombrar su forma con `datos_de_prueba = "<pack>.<modo>"`
     (configuración, `core/presets/plantillas/`), y entonces gana sobre las habilidades. Así lo hace
     `suscripciones.json` con `pack_commerce.planes`: ningún pack nombra al otro (invariante 5 y la
     guarda «commerce no nombra al proveedor de suscripciones»).
   - `pack_subscriptions` no siembra nada propio: sus suscripciones necesitan clientes reales del core.
3. **Generación.** Una llamada `generate` sin tools, `thinking=False`, temperatura 0,4, 4000 tokens y
   90 s de tope, con el proveedor de la plataforma. Las instrucciones viven en el playbook del kernel
   `datos_de_prueba` (`load_core_playbook`: viaja con el binario, ningún negocio lo pide ni lo
   reemplaza, no entra a ningún turno). El negocio (descripción, voz, moneda, idioma), el agente y el
   JSON Schema van como **datos** en el mensaje. La salida es no confiable: primer objeto JSON,
   validado con el esquema (`extra="ignore"`: un teléfono o un SKU que invente el modelo se descarta;
   topes de cantidad, largo, montos y fechas). Si el proveedor falla o la salida no valida, se siembra
   el respaldo (`origen = 'plantilla'`, con el motivo `proveedor` o `formato`).
4. **Sin choques con el negocio.** El pack pone los identificadores con un sufijo del lote: SKUs
   `PRUEBA-<sufijo>-NN` y carteras «<nombre> · <sufijo>». Los clientes de cartera van sin teléfono.
5. **Lote y retiro.** Tabla `test_data_records` (0098): una fila por registro creado, con agente, lote,
   pack, modo, `kind`, `record_ref` y origen. RLS forzada por `tenant_id`; `app_runtime` con SELECT,
   INSERT y UPDATE (sin DELETE). Regenerar = retirar los lotes vivos del agente (cada pack lo suyo) y
   sembrar el nuevo; «Borrar» = solo retirar. Lo hecho a mano no está en la tabla y no se toca.
   - commerce desactiva las variantes y borra lógicamente las categorías creadas. **Los productos no
     tienen `deleted_at`**: quedan sin variantes activas (no se venden ni se recomiendan) y se ven
     como inactivos en el panel. *Revisable:* `deleted_at` en productos si molesta en la lista.
   - calendar y cobranza marcan `deleted_at` (cobranza de las hojas a la raíz: pagos, cuotas,
     préstamos, clientes, cartera). La caja de la cartera no tiene `deleted_at` y queda con la cartera
     retirada.
   - Se descartó `agent_record_origins` (0095): es append-only y registra lo que hizo el agente en una
     conversación, no lo sembrado.
6. **Concurrencia.** `pg_try_advisory_xact_lock` por negocio y agente en una sesión abierta durante
   el retiro y la siembra (la IA responde antes, sin retener una conexión): un doble clic recibe 409
   `en_curso`. La siembra del pack y el registro del lote van en transacciones separadas; si el
   registro fallara, lo sembrado quedaría sin lote (solo en test; se borra a mano).
   Una categoría del lote que ya tiene productos de otros no se retira.
7. **Consumo.** `usage_ledger.kind` admite `'generacion'` (0098 recrea `ck_usage_kind` NOT VALID y lo
   valida). Créditos de plataforma con el concepto `llm_turn` del tarifario vigente.
8. **Solo test.** `GET`, `POST` y `DELETE /api/v1/admin/agents/{id}/datos-de-prueba` responden 404 sin
   `QA_TESTERS_ENABLED`. Permiso: sección `agents` (escritura para generar y borrar). La descripción
   vacía o la fija de «Preparar mis datos» (`DESCRIPCION_QA`) da 422 `descripcion_requerida`: con ella
   todos los testers generarían lo mismo.

## Consecuencias

- Un pack nuevo ofrece datos de prueba implementando los tres hooks; el core no conoce sus tablas.
- El costo es una llamada por clic (< 0,005 USD con DeepSeek). Los tests usan un proveedor guionado.
- No cambia ningún prompt ni tool del turno: el gate de evals y sus grabaciones no se invalidan.
- La importación de datos existentes (HU-QA-12) puede reusar los esquemas y la tabla de lotes.

## Alternativas descartadas

- **Solo plantillas por giro:** cero costo, pero solo los giros que tengan plantilla y nada de la
  descripción real.
- **Columna `es_prueba` en cada tabla de pack:** migraciones en cuatro packs y en cada tabla futura.
- **Habilitarlo en producción:** el agente podría ofrecer a un cliente real algo inventado (D-SPEC-32-2).
