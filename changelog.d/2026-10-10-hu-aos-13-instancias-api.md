## Agent OS: escrituras de instancias por conexión y altas con agentes (2026-10-11)

- HU-AOS-13 (parte 2, #374). **API de instancias**: `POST /api/v1/admin/connections/{id}/agents`
  (agrega un especialista con versión publicada), `PATCH .../agents/{instance_id}` (habilitar o
  deshabilitar, prioridad, volverlo el agente por defecto) y `DELETE .../agents/{instance_id}`
  (borrado lógico). Sección `settings` con escritura; con `aos_runtime = legacy`, solo el master.
  - El Coordinador lo gestiona el sistema: no se agrega, no se deshabilita ni se quita por la
    API (409 `core_required` / `not_specialist`). Siempre hay un agente por defecto y está
    habilitado (409 `default_disabled` / `default_required`). 422 para una integración.
- **Altas posteriores:** un número dado de alta o reconectado (los cinco caminos, por
  `numero_para_conexion`) nace con Coordinador y General si el negocio ya tiene agentes, en la
  misma transacción. Un error ahí se registra y el alta sigue.
- **Promoción a producción:** la conexión del número real clona las instancias de la de prueba
  (`clonar_instancias`, que HU-AOS-14 reutiliza para clonar la release).
- El backfill y las altas ya no recrean el General en una conexión que tiene Coordinador: lo que
  el dueño quitó no vuelve.
- Sin cambio de conducta: con `aos_runtime = legacy` nada en runtime lee las instancias.
