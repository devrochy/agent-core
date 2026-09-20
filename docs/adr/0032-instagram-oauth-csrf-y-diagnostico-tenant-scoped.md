# ADR-0032 — Instagram Login: state de OAuth sin secreto por defecto y diagnóstico de webhooks con verificación de tenant

- **Estado:** aceptado
- **Fecha:** 2026-09-19

## Contexto

La conexión de Instagram (ruta b, Instagram Login) firma el `state` de OAuth
con HMAC para CSRF. El auditor-multitenant (2026-09-24) encontró que
`_state_secret()` caía a `settings.app_secret_key` (default público
`"change-me"`) cuando `INSTAGRAM_APP_SECRET` no estaba configurado, y que
`instagram_ig_login_callback` no comparaba el `tenant_id` firmado en el
`state` contra el tenant real de la sesión del admin que completa el
callback. Combinados, en un despliegue mal configurado un admin autenticado
podía forjar un `state` válido para vincular una cuenta de Instagram a un
tenant ajeno. Además, `check_recent_instagram_webhook` (la prueba de humo)
no filtra por tenant -- el log de webhooks es preresolución de tenant por
diseño (ADR-0028) -- y el endpoint que la expone no verificaba que
`ig_user_id` perteneciera al tenant/conexión del llamador, generando un
oráculo de existencia de actividad de una cuenta ajena.

## Decisión

1. `_state_secret()` deja de tener fallback: si `INSTAGRAM_APP_SECRET` no
   está configurado, se corta con un error explícito en vez de firmar con
   un secreto compartido/débil.
2. `verify_state` acepta `expected_admin_id` y compara el `admin_id`
   firmado contra el admin que completa el callback; `instagram_ig_login_
   callback` además compara el `tenant_id` decodificado contra
   `get_current_tenant_id()` de la sesión activa, y rechaza en caso de
   discrepancia -- defensa en profundidad además de la firma HMAC.
3. `instagram_diagnostico_estado` resuelve primero la `IgAccount` por
   `tenant_id` + `connection_id` + `ig_user_id` (mismo patrón que
   `meta_connection_cuenta_ig_reverificar`) antes de delegar en
   `check_recent_instagram_webhook`, que sigue sin tocar `tenant_id`
   porque la tabla que consulta no lo tiene resuelto a esa altura.
4. `_maybe_refresh_one` (cron de refresco) agrega `tenant_id` explícito a
   sus dos queries de `IgAccount`, aunque ya corren dentro de
   `tenant_scope` -- defensa en profundidad, mismo criterio que
   `get_owned()` en el resto del código.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Solo agregar el chequeo de tenant en el callback, dejar el fallback del secreto | El fallback sigue siendo forjable por cualquiera que conozca `app_secret_key` por defecto; el chequeo de tenant es defensa en profundidad, no reemplaza una firma robusta. |
| Agregar `issued_at` a `meta_tokens` en vez de derivar la edad | Ya descartado en el diseño original del ticket (tabla auditada por 3 tickets previos, se prefiere derivar de `expires_at`); no es parte de este hallazgo. |
| Filtrar `check_recent_instagram_webhook` por tenant directamente | Rompería ADR-0028 (el log es preresolución de tenant); se resuelve en el llamador, no en la función. |

## Consecuencias

**Positivas:** un despliegue sin `INSTAGRAM_APP_SECRET` configurado falla
rápido y explícito en vez de aceptar conexiones inseguras; el callback y el
diagnóstico ya no dependen únicamente de que el `state`/`ig_user_id` sean
válidos sintácticamente, verifican pertenencia real al tenant activo.

**Negativas / lo que aceptamos pagar:** una consulta extra (`SELECT
IgAccount`) por cada diagnóstico de humo; aceptable dado el volumen (botón
de un panel admin, no un endpoint de alto tráfico).

**Qué haría que revisáramos esto:** si se agrega un tercer login flow de
Meta que reuse `sign_state`/`verify_state` con un `admin_id` opcional
distinto (ej. flujo sin sesión de admin), habría que revisar si
`expected_admin_id` sigue siendo el criterio correcto o hace falta otro.

## Impacto en el código

- `core/gateway/meta_instagram.py`: `_state_secret`, `sign_state`,
  `verify_state`.
- `core/admin/views.py`: `instagram_ig_login_callback`,
  `instagram_diagnostico_estado`.
- `core/tasks/instagram_token_refresh.py`: `_maybe_refresh_one`.
