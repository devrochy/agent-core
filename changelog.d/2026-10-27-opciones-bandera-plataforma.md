## Opciones directas: ruta de plataforma (master) para encender la bandera y primera tool apta (SPEC-40, PR 10a, 2026-10-27)

- **Nada se enciende con este cambio:** la bandera `tenants.settings.opciones_directas.habilitada` sigue apagada en
  todos los negocios. Solo existe ahora quién la puede fijar. El interruptor en el panel es el PR 10b; el despliegue a
  test y la prueba real del QUICK_REPLY de Meta (puerta para encenderla) siguen pendientes del dueño.
- `GET|PUT /api/v1/admin/platform/tenants/{tenant_id}/opciones-directas` (solo el admin master; el tester y el dueño
  reciben 403): lee y fija `{habilitada: bool}` del negocio de la ruta, dentro de su `tenant_scope`, con
  `bandera.fijar` (único escritor; `jsonb_set` atómico) y auditoría `opciones_directas_bandera` con el actor
  `admin:<email>`. 404 si el negocio no existe o está borrado, 422 si el cuerpo trae algo distinto de
  `habilitada: true|false`. Idempotente. Esquemas `OpcionesDirectasBandera` y `OpcionesDirectasBanderaIn`; el negocio se
  elige con el listado de plataforma que ya existe (`GET /admin/platform/tenants`).
- `bandera.leer(tenant_id)`: lectura que distingue «apagada» de «negocio inexistente» para el GET.
- **Riesgo de `tenants.settings` evaluado: no es real con los caminos actuales.** Todo camino que reescribe el dict
  completo (suscripciones, presets de agentes, aplicación de preset, origen del alta) lee la fila con `FOR UPDATE` y
  el `UPDATE … jsonb_set` del master espera ese candado y se aplica encima; una prueba estática
  (`tests/test_settings_del_negocio_candado.py`) vigila que un escritor nuevo no se olvide del candado. No hace falta
  pasar la bandera a una columna.
- **Primera acción apta:** `list_client_appointments` (pack de agenda) lleva `apta_para_opcion=True`: solo lee las
  citas del cliente de la conversación. Una prueba (`tests/test_opciones_tools_aptas.py`) fija la lista de las
  marcadas, de modo que agregar otra exige revisión. Ninguna es `sensible` (no cambia estado).
