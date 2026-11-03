Corregido: la próxima ejecución de un cron (`proxima_ejecucion`) ya no depende de la fecha real; pasada la medianoche UTC saltaba a la hora siguiente y `test_cron_catalogo` fallaba en `develop`.
