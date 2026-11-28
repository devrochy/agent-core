## Clientes: alta manual, importación CSV y filtro de datos del lead (2026-11-27)

- `POST /api/v1/admin/leads` crea un cliente y su lead desde el panel (nombre y teléfono o correo), reutiliza el
  cliente si el contacto ya existe y responde 409 con su id si ya tiene un lead con datos, en vez de duplicarlo.
- `POST /api/v1/admin/leads/import` acepta un CSV de hasta 1000 filas y 2 MB con encabezados en español o inglés;
  cada fila va en su propio savepoint, así una fila mala no tumba las buenas. Devuelve creados, actualizados y los
  errores con su número de línea.
- El provider del lead del core suma el filtro `lead_datos` (`con`/`sin`) con contadores, para que Clientes muestre
  «Con datos» y «Sin datos» en la misma lista que el embudo y no en una pestaña aparte.
- Pruebas puras del lector CSV e integración de alta/importación (se saltan sin `AGENT_CORE_INTEGRATION=1`).
