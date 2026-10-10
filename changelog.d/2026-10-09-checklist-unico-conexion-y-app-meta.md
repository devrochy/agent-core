## Un solo checklist para "Pasar a producción" y para la App de Meta (2026-10-09)

- HU-CONN-06 sobre HU-CONN-07. "Verificar" y "Pasar a producción" de una conexión BYOK ya no tienen una lista
  propia de chequeos: usan el checklist por conexión de HU-CONN-07 (token, número registrado, WABA suscrita,
  webhook y plantillas mínimas, también las de los avisos) y le suman lo que solo pide la promoción: que la app sea
  propia, que el número sea real (no el de prueba de Meta), que esté registrado en la Cloud API y que la WABA esté
  suscrita a la app del negocio. La respuesta trae cada ítem con su etiqueta y su acción de reparación, como en
  "Producción" de la conexión (`PromotionCheck` se reemplaza por `ReadinessItem`). Como corren en el request,
  hay una sola revisión a la vez por negocio, 10 s de espera después y un tope de 60 s (504 si Meta no responde).
- HU-SH-05 sobre HU-CONN-07. Los requisitos de Embedded Signup ya no revisan por su cuenta las variables de la app,
  el HTTPS ni el webhook: la revisión de "Plataforma → App de Meta" suma el ítem `meta_app_configured`
  (`META_APP_ID`, `META_APP_SECRET`, `META_CONFIG_ID`) y el webhook falla si `PUBLIC_BASE_URL` no es HTTPS. Los
  requisitos solo quedan "listos" con los pasos manuales confirmados y la última revisión de la plataforma sin ítems
  en rojo y de hace menos de 30 días (`platform_check` reemplaza a `automatic`; `expired` y `expires_at` dicen si
  venció). Pasado el plazo vuelven a pendiente y la página pide revisar de nuevo.
