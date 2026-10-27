## El General y el Coordinador nacen con el negocio (2026-10-27)

- HU-AOS-53, SPEC-39. `onboard_tenant` reusa el backfill B2 (idempotente) al final del alta: crea los dos agentes
  reservados con su versión, derivados del AgentSpec v2 recién creado, y las instancias de las conexiones que ya
  existan. No cambia `aos_runtime`: el negocio sigue en `legacy` hasta su migración (P-34), así que no cambia cómo
  atiende. Un fallo se registra (`alta_agentes_base_fallo`) y el alta sigue; el backfill lo repara.
- Un negocio **nuevo** nace con `settings.agent_os.recortar_general = true`: cada especialista que cree el asistente
  le quita al General lo suyo y le deja solo la coordinación. Un negocio que ya existía conserva su bandera (la
  enciende el master) y repetir el alta no la pisa.
