## Test de puertos determinista (2026-10-10)

- `test_choose_port_ignora_un_previo_que_ya_no_es_nuestro` ya no depende de que el puerto 49600 esté libre
  en la máquina: fallaba al azar en el runner compartido (PRs #401 y #410).
