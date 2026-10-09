## Checklist de producción real: por conexión y de la plataforma (HU-CONN-07, 2026-10-09)

- **Por conexión.** `GET /api/v1/admin/meta/connections/{id}/production` devuelve el último resultado
  guardado (con fecha) del checklist de producción de esa conexión: token, número registrado en la Cloud
  API, WABA suscrita a la app, webhook (suscripción de la app de esa conexión al objeto de su canal y
  override por número) y plantillas mínimas aprobadas (al menos una, y todas las asignadas a avisos en
  `message_templates_map`). Se evalúa con las credenciales de ESA conexión (resolver de HU-CONN-01).
  `POST .../production/run` (202) solo registra la corrida y encola el job.
- **Solo lo necesario.** `check_app_subscriptions_complete` exige solo `whatsapp_business_account`;
  `page` e `instagram` se informan como no obligatorios en vez de dejar el check siempre en rojo.
- **Plataforma aparte, solo master.** `GET /api/v1/admin/platform/meta-app` y `POST .../meta-app/run`:
  webhook de la plataforma, suscripciones de la app, App Secret distinto al de desarrollo, claves de
  cifrado, worker y DLQ, RLS forzada, tests de aislamiento (informa el commit, sin pytest) y secretos en
  plantillas. Un check que revienta queda "sin evaluar" sin tumbar la corrida.
- **Sin bloqueo.** Las dos corridas son jobs de Celery (`core.meta_connection_production_check` y
  `core.platform_readiness_check`); el encolado va en un hilo. El resultado queda en
  `meta_production_checks` (por negocio, RLS forzada) y `platform_readiness_runs` (tabla de sistema),
  migración `0064`. Una sola corrida activa por conexión y una para la plataforma; una activa de más de
  10 minutos se muestra fallida y deja pedir otra.
- **Obsoletas** (se mantienen para la app móvil): `GET /admin/meta/checklist/production` y
  `POST /admin/meta/checklist/production/run`. La segunda ya no corre los checks de la plataforma ni nada
  en el request: encola la revisión de cada conexión y devuelve lo guardado.
- **Tras la revisión de seguridad:** espera mínima de 60 s entre corridas terminadas (cada una llama a
  Meta); el worker solo toma corridas en cola y vigentes, con tope de 240 s; el servicio exige master
  para la plataforma y un test impide usar `platform_readiness_runs` fuera de su servicio; el GET al
  override de webhook de un número solo va a hosts públicos por HTTPS (anti-SSRF, `ScopedHttp`); los
  detalles guardados ya no llevan el texto de excepciones de red ni el cuerpo crudo de una respuesta no
  JSON de Meta; y "Requisitos de Meta" ya no muestra a un negocio los datos de la app de la plataforma
  (nombre, suscripciones, `config_id`): eso queda en "Plataforma → App de Meta".
