## El runtime deja de ser bandera: nadie lee `tenants.aos_runtime` (HU-AOS-69, 2026-11-13)

Todo negocio corre en el Agent OS desde ADR-0191. Este cambio quita las lecturas de la columna; la
columna se borra en el PR siguiente (migración 0127), cuando este código ya esté desplegado.

- **API (quita campos de respuesta):** sale `runtime` de `GET /admin/dashboard/agents` (`ResumenPorAgente`) y de la consola de
  conexiones (`ConnectionsConsole`); sale `aos_runtime` del catálogo de configuración (`Catalogo`).
  En el panel master, `AgentOSBanderas`/`AgentOSEstado`/`AgentOSResultado` pierden `runtime` y
  `aos_connection_routing`, y `AgentOSObjetivo` pierde `runtime` y `construir` (si un panel viejo los
  manda, se ignoran). El panel y la APK dejan de leerlos en sus propios PRs, que van primero.
- **APK instalada:** `AgentsOverview.runtime` se queda, deprecado y siempre `"v3"`: lo lee el JS
  de la APK anterior a apk#250 y los binarios que ya no reciben OTA dependen de él.
- **Configuración:** sale la opción del master «Motor de conversación» (`aos_runtime`) y la
  dependencia de las demás banderas del Agent OS sobre ella. El asistente siempre omite las preguntas
  que solo cambian la voz (la voz sale de la versión publicada del agente).
- **Kernel:** el turno, el motor de procedimientos, el router, la consola, el canary (`requiere_v3`
  ya no existe), el contract (`negocio en legacy`), los presets v2 (`banderas_apagadas` ya no
  existe), «Preparar mis datos», los escenarios de QA, los journeys y el gate dejan de mirar el
  runtime. `negocios_v3` pasa a `negocios_activos`.
- Sin migración en este PR: `aos_fijar_banderas` sigue recibiendo `'v3'` fijo hasta 0127.
