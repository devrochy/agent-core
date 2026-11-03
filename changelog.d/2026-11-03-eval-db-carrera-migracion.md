## Pruebas de integración: la base de eval ya no se migra en carrera (2026-11-03)

- `tests/integration/_eval_db.py`: con xdist, varios workers veían la base sin migrar y lanzaban `db migrate` a la vez (`pg_type_typname_nsp_index`, `expected to match one row`), dejando `integracion` en rojo al azar. Ahora un candado de Postgres hace que migre uno solo y los demás la encuentren lista.
