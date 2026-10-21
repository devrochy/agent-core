# ADR-0147 — Aliados (proveedores) opcionales por negocio en pack_commerce

- **Estado:** propuesto (HU-CRM-03, SPEC-33, D-40). Falta la aceptación del dueño.
- **Fecha:** 2026-10-21
- **Contexto:** SPEC-33 (`spec.md` y `design.md`) en `solvershub-docs`. Cierra D-SPEC-33-2 (opción A) y
  D-SPEC-33-3 (opción B). **Referencia:** agent-sale `migrations/0039`, `migrations/0041`,
  `src/shared/db/alliesDirectory.ts` y `src/domains/commerce/aplicarPromocion.ts:182` (`esElegible`).
  **Usa:** ADR-0079 (API admin de los packs), ADR-0125 (interruptor en `tenants.settings`).

## Contexto

El dueño opera en agent-sale tiendas que venden productos de varios proveedores («aliados»): agrupan el
catálogo, ordenan el import y limitan promociones. Los quiere en SolversHub, pero opcionales: la mayoría
de los negocios no tienen proveedores y no deben ver nada nuevo. Los aliados no son referidos ni llevan
comisiones.

## Decisión

1. **Modelo.** Tabla `pack_commerce_allies` (`id`, `tenant_id`, `name`, `contact_info` jsonb, `active`,
   `deleted_at`, `created_at`) con RLS forzada y `UNIQUE (tenant_id, lower(name)) WHERE deleted_at IS NULL`
   (el CSV resuelve por nombre). `ally_id` **nullable** en `pack_commerce_products` y
   `pack_commerce_promotions`, sin aliado sembrado: a diferencia de agent-sale, «sin aliado» es válido y
   los negocios de hoy no cambian (migración `crm_0012`, solo aditiva). FK lógica, como el resto del pack.
   `contact_info` tiene forma fija (`contacto`, `telefono`, `email`, `notas`) y rechaza otras claves.
2. **Bandera por negocio.** `tenants.settings["commerce_aliados"] = {habilitado, por, en}`, con el patrón
   del interruptor del alta (ADR-0125): UPDATE atómico de la clave y fila en `audit_log`. La escribe solo
   el master por `GET/PUT /api/v1/pack/commerce/platform/tenants/{id}/aliados` (Negocios › Acciones del
   panel). El endpoint vive en el pack para que el core no conozca el dominio. No se usa el mecanismo de
   HU-AOS-15 (función SECURITY DEFINER y trigger): existe porque las columnas `aos_*` están bloqueadas para
   `app_runtime`, y `settings` no lo está.
3. **Con la bandera apagada**, `/pack/commerce/allies` responde 404 `aliados_deshabilitados`, asignar un
   aliado da 404 y la columna `aliado` del CSV se ignora. Quitar un aliado (`ally_id: null`) siempre se
   puede. Apagar no borra nada: aliados y asignaciones vuelven al encenderla. El panel lo sabe por
   `GET /pack/commerce/modulos` (`{aliados: bool}`), sin inferirlo de un 404.
4. **API.** CRUD `GET/POST /allies`, `PATCH/DELETE /allies/{id}` (con el conteo de productos);
   `?ally_id=<id>|none` en `GET /products`; `ally_id` en `ProductSave`/`ProductOut` y
   `PromotionSave`/`PromotionOut`, donde **no mandarlo deja el aliado como está** y `null` lo quita (la APK
   edita productos sin conocer los aliados); columna `aliado` en el import CSV, con
   `ImportResult.unknown_allies` para los nombres que no existen (la fila se crea sin aliado).
5. **Promociones.** Una promoción con aliado aplica solo si **todos** los ítems del carrito son de ese
   aliado (la regla `esElegible` de agent-sale); un carrito mixto no aplica. La regla está en
   `_line_matches_promo` y depende de los datos, no de la bandera: sin aliados, `ally_id` es `None` y la
   salida de las tools no cambia.
6. **Borrar un aliado** es lógico. Sus productos quedan sin aliado y sus promociones se desactivan
   conservando el `ally_id`: quitárselo abriría la promoción a todo el catálogo.

## Consecuencias

- Los negocios sin aliados no ven ningún cambio, ni en el panel ni en el asistente (el gate de evals en
  replay no cambia).
- El asistente no menciona aliados. Si se quiere que los use en la conversación (p. ej. «¿qué tienen de
  la Distribuidora Norte?»), es otra HU.
- Las promociones v2 (HU-COM-11) siguen parqueadas: aquí solo se suma el aliado a las de hoy.
- Si el dueño prefiere que el dueño del negocio encienda los aliados (D-SPEC-33-2, opción B), el
  interruptor pasa a la configuración de Ventas sin tocar el modelo.
