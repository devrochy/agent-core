## Tests de integración sin capture_logs sobre loggers de módulo (2026-11-18)

- `test_pack_commerce_ignora_cobros_de_suscripcion.py` y `test_telegram_medios_db.py` sustituyen el logger del módulo por uno que anota en vez de usar `capture_logs`, con el mismo arreglo de core#850: un logger cacheado por `cache_logger_on_first_use` no lo ve `capture_logs` si otro test configuró antes una lista de procesadores nueva. En el primero, una captura vacía hacía pasar en falso la comprobación de «sin alarma».
