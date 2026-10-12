## CI: Postgres y Redis de los jobs con datos en memoria (2026-10-11)

- Los servicios `postgres` y `redis` de `ci.yml` y `evals-nightly.yml` montan `--tmpfs` en su carpeta
  de datos. En el runner self-hosted, GitHub borra el contenedor de servicio pero no su volumen
  anónimo: quedaban ~70 MB por job y el 2026-10-11 eran 1.628 volúmenes (69 GB) en el disco del
  runner, que es la misma máquina de test. Con `tmpfs` no se crea volumen y la base de cada job vive
  en RAM (más rápida; se pierde al terminar el job, que es lo que se quiere).
