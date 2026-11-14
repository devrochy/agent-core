## Salen las columnas `tenants.aos_procedures` y `tenants.aos_routing` (HU-AOS-69, migración 0128, 2026-11-13)

- **Migración 0128:** borra el trigger `tenants_aos_banderas_guard` y su función, la función
  `aos_fijar_banderas` (firma de 0127), el CHECK `ck_tenants_aos_routing` y las dos columnas. Si queda
  algún negocio vivo sin todo encendido, avisa con un WARNING (no aborta). Las filas `agent_os.banderas`
  de `audit_log` se quedan como historia.
- **Downgrade:** repone las columnas con `true`/`'active'` para los negocios existentes (la conducta de
  hoy) y `false`/`'off'` por defecto para los nuevos, su CHECK y el texto de 0127 de la función y del
  guardia.
- **Despliegue:** después del PR que dejó de leer las columnas (ya desplegado), las tres apps (API,
  worker y beat) con la misma imagen.
