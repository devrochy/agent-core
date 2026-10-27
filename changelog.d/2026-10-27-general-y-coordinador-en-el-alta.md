## El General y el Coordinador nacen con el negocio (2026-10-27)

- HU-AOS-53, SPEC-39. `onboard_tenant` reusa el backfill B2 (idempotente) al final del alta: crea los dos agentes
  reservados con su versión, derivados del AgentSpec v2 recién creado, y las instancias de las conexiones que ya
  existan. No cambia `aos_runtime`: el negocio sigue en `legacy` hasta su migración (P-34), así que no cambia cómo
  atiende. Un fallo se registra (`alta_agentes_base_fallo`) y el alta sigue; el backfill lo repara.
- Un negocio **nuevo** nace con `settings.agent_os.recortar_general = true`: cada especialista que cree el asistente
  le quita al General lo suyo y le deja solo la coordinación. Un negocio que ya existía conserva su bandera (la
  enciende el master) y repetir el alta no la pisa.
- **Un negocio nuevo nace en `aos_runtime = 'v3'`** cuando `AOS_CONNECTION_ROUTING` está encendida en el entorno
  (P-34): así su dueño puede crear agentes con el asistente sin pedirle nada al master. Usa `migrar_a_v3` (rol
  dueño, orden de banderas y auditoría, el mismo camino de «Preparar mis datos») sin construir ni promover
  releases: la release llega con cada conexión y, mientras no la tenga, el turno sigue por el motor de siempre.
  Con la global apagada o ante un bloqueo el negocio queda en `legacy` y el alta lo avisa en sus pasos.
- `recortar_general` se escribe con `coordinacion.fijar_bandera` (auditado como `agent_os_recorte_general`), y el
  nuevo comando `agent-core db recorte-general (--tenant | --todos) [--apagar] [--aplicar]` lo cambia en negocios
  existentes (dry-run por defecto; `--todos` solo recorre los que están en v3). Los negocios ya existentes no se
  tocan hasta que alguien lo corra.

`onboard_tenant(agentes_base=False)` salta esos pasos: lo usan los journeys del gate de pass^k, que montan su propio runtime y chocaban con `uq_agents_tenant_key` al crear un segundo Coordinador.
