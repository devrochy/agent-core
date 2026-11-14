## El motor de procedimientos y el ruteo ya no son banderas por negocio (HU-AOS-69, 2026-11-13)

Decisión del dueño (2026-11-13): el motor de procedimientos queda siempre encendido y el ruteo entre
especialistas siempre en `active`. Primera fase del retiro de `tenants.aos_procedures` y
`tenants.aos_routing`: el código deja de leerlas y escribirlas; las columnas, su CHECK, la función
`aos_fijar_banderas` y el guardia `tenants_aos_banderas_guard` salen en la migración siguiente.

- **Turno:** el motor corre en todo turno con negocio (sale su modo sombra, `procedures/sombra.py`) y
  el router enruta siempre; solo baja a sombra si la release lo pide (`routing.shadowMode` del
  KernelSpec, que se queda). `oferta.motor_activo()` ya no lee la base: las tools `sin_motor_activo`
  (`sub_cambiar_plan`, `cob_registrar_promesa_pago`) dejan de ofrecerse y las `solo_con_motor_activo`
  se ofrecen en todo negocio, como ya pasaba con la bandera encendida.
- **Cron de esperas vencidas:** recorre los negocios no borrados (`banderas.negocios_vivos`), también
  los suspendidos.
- **API (incompatible):** salen `GET`/`PUT /admin/platform/tenants/{id}/agent-os` y
  `POST .../agent-os/simular` con sus schemas `AgentOS*`, y `ConnectionsConsole.routing`. El catálogo
  de configuración pierde las opciones `aos_procedures` y `aos_routing`. `RoutePreview.mode` vale
  `active`, o `shadow` si la release pide sombra.
- **CLI:** sale `agent-core db ruteo`. `db reporte-ruteo` sigue con sus dos fuentes (golden contra la
  release activa y tráfico real) y ya no imprime banderas.
- **Centro de pruebas:** «Preparar mis datos» ya no enciende banderas (no hay nada que encender).
- **Evals:** sale `--runtime sin-motor|v3` de `evals/journeys.py` (ya no hay línea base sin el motor),
  `motor_de_procedimientos` y `requiere_motor` de los sets, y el input `runtime` de
  `evals-procedimientos.yml`. Los sets que corrían sin el motor ahora lo traen: sus casetes de replay
  se regraban.
- **Despliegue:** sin migración. Los negocios que tenían alguna bandera apagada pasan a la conducta
  con todo encendido al desplegar.
