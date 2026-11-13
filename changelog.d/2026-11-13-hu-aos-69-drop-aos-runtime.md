## Sale la columna `tenants.aos_runtime` (HU-AOS-69, migración 0127, 2026-11-13)

- **Migración 0127:** quita los CHECK `ck_tenants_aos_runtime`, `ck_tenants_aos_order` y
  `ck_tenants_aos_routing_order`; `aos_fijar_banderas` pasa a 5 argumentos (sin `p_runtime`) y su
  auditoría deja de llevar `runtime`; el guardia `tenants_aos_banderas_guard` mira solo
  `aos_procedures` y `aos_routing`; y borra la columna. Aborta si queda algún negocio fuera de `'v3'`.
  El downgrade la repone en `'v3'` con sus CHECK y el texto de 0126.
- **Código:** `fijar_banderas` llama a la firma nueva; sale el código `orden` de `BanderasRechazadas`.
- **Despliegue:** migración y código juntos, las tres apps (API, worker y beat) con la misma imagen y
  después del PR que dejó de leer la columna.
