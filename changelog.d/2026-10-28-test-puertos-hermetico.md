## Test de puertos libres hermético (2026-10-28)

- `test_find_free_port_evita_puertos_de_contenedores_parados` ya no depende de que el puerto 49302 esté libre en el host: parchea `port_available`. Fallaba de forma intermitente en el CI al correr `rapido` a la vez que los contenedores de integración.
