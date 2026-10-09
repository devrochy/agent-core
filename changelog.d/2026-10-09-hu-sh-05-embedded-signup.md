## Embedded Signup real en el panel nuevo (2026-10-09)

- HU-SH-05 (ADR-0128, implementa ADR-0080). El alta "Producción: conectar con Facebook" funciona de punta a punta
  con Embedded Signup v4: el panel abre el popup con el `config_id` de la plataforma y el backend canjea el `code`
  y encadena suscribir la WABA, registrar el número con el PIN del negocio, sincronizar plantillas y verificar
  salud. La verificación contra Meta real queda pendiente del dueño (app de la plataforma aprobada y configurada).
- Estado y cancelación del wizard: `GET /api/v1/admin/meta/wizard/session` (en qué paso quedó, sin efectos),
  `POST /wizard/cancel` y `POST /wizard/resume`. El canje exige el intento de un solo uso que da
  `POST /wizard/session` (atado al admin, vence a los 15 min). `/wizard/exchange`, `/wizard/number` y `/wizard/pin`
  devuelven el estado completo (`WizardSessionOut`) en vez de `{connectionId, nextStep}`. Los IDs del popup solo
  aceptan dígitos.
- El token del canje se guarda solo después de verificar que ve la WABA y el número; reautorizar con otra WABA u
  otro número da 409. El wizard ya no toma una conexión BYOK (409 `byok_connection_exists`) ni reactiva una pausada
  (409 `connection_paused`).
- El PIN de verificación en dos pasos lo escribe el negocio (antes se generaba uno que nadie veía); sin PIN la
  conexión queda `pending` hasta registrarlo. En producción también es obligatorio desde "Números".
- Un alta vencida (24 h) reutiliza la conexión borrador; `restart` empieza de nuevo; dos pedidos simultáneos ya no
  dan 500; la cadena tiene un tope de 45 s por request. Sin `META_APP_ID`/`META_APP_SECRET`/`META_CONFIG_ID` el
  wizard dice qué variable falta.
- La app de la plataforma: el alta de un negocio ya no re-registra su suscripción de webhook (solo el master); el
  cron de 15 min la lee con las credenciales del entorno (solo lectura) y alerta una vez si está inactiva. Una
  conexión de Embedded Signup solo queda `connected` con el token y esa suscripción verificados.
- Mensaje de prueba desde el hub de la conexión: `GET|POST /api/v1/admin/meta/connections/{id}/test-message`
  (plantilla aprobada sin variables, uno cada 30 s y 10 al día, dueño o master en producción, en la auditoría).
- "Plataforma → Embedded Signup", requisitos de Meta de la app de la plataforma (solo master):
  `GET /api/v1/admin/platform/meta-app/embedded-signup` y `POST .../step`. Migración `0066`
  (`platform_meta_app_requirements`, tabla de sistema; cuelga de 0063 y hay que re-encadenarla tras 0064/0065).
- Seguridad: errores de transporte del canje como `GraphError` y más claves (`client_secret`, `code`, `params`…) en el
  scrubber de Sentry; los logs de `debug_token` solo llevan la forma.
