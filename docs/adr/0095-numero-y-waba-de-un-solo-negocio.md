# ADR-0095 — Un número o una WABA de Meta pertenecen a un solo negocio

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Enmienda:** ADR-0012, "Webhooks: `phone_number_id` → `channel_connection` → tenant": el número se busca también en
  `phone_numbers` (conexiones de Meta vivas), va **antes** que la WABA, y un activo que reclaman dos negocios no
  resuelve a ninguno. Sigue el mismo mecanismo (`SECURITY DEFINER`, bootstrap sin RLS).
- **Contexto:** hallazgo alto preexistente de la revisión de HU-CONN-01 (agent-core#238); relacionado con ADR-0039
  (funciones `SECURITY DEFINER` que no exponen datos), ADR-0094 (credenciales por conexión) y ADR-0030 (wizard de
  Embedded Signup).

## Contexto

El webhook de WhatsApp decide a qué negocio va cada evento por la WABA (`entry.id`) y el número
(`metadata.phone_number_id`). Un negocio podía registrar la WABA o el número de otro y recibir sus mensajes:

- `resolve_tenant_for_event` miraba **primero la WABA** y `resolve_tenant_for_waba` hacía `LIMIT 1` sin orden,
  contando también WABAs de conexiones borradas;
- `resolve_tenant_for_phone` solo veía `channel_connections`, no `phone_numbers`, y comparaba con el `external_id`
  de cualquier tipo de conexión;
- las altas guardaban la WABA y el número sin verificarlos: la manual y BYOK, sin comprobar que el número es de esa
  WABA; el Embedded Signup, con los IDs que manda el navegador y **antes** de canjear el `code` (un `code`
  inventado dejaba las filas escritas).

## Decisión

Un número o una WABA de Meta pertenecen a un solo negocio: el ruteo nunca elige entre dos, y ninguna alta guarda un
activo sin verificarlo contra Graph ni si otro negocio ya lo tiene.

1. **Ruteo, número primero, y sin elegir ante un conflicto** (`webhook_route_tenant.py`). El número identifica a
   quién le escribieron; la WABA decide si el evento no trae número (plantillas, cuenta) o el número no resuelve. Si
   el número es de un negocio y la WABA de otro, una de las dos filas es ajena (heredada sin verificar, o cargada
   por una vía que no comprueba la WABA): el evento no va a ninguno (`unresolved_tenant`) y se registra
   `webhook_numero_y_waba_de_negocios_distintos`. El webhook rutea cada grupo `(entry, número)` por separado, no la
   entry entera por su primer evento.
2. **Resolvedores sin ambigüedad** (migración `0052`, `CREATE OR REPLACE` con la misma firma). Solo cuentan
   conexiones vivas: no borradas y fuera de `draft`. Una conexión en `draft` es un Embedded Signup que nunca canjeó
   su `code`, y sus filas las escribía el navegador sin verificar. En `channel_connections` solo cuentan las de
   WhatsApp por Meta. Devuelven NULL si el activo lo reclaman cero o más de un negocio. Un evento ambiguo queda como
   `unresolved_tenant` en `webhook_events` (falla cerrado y se ve); en el endpoint por slug cae al tenant de la URL,
   como hasta hoy.
3. **Función de reclamo** `meta_asset_claimed_by_other_tenant(p_waba_id, p_phone_number_id, p_tenant) → boolean`
   (`SECURITY DEFINER`). Responde si **otro** negocio tiene la WABA o el número en una conexión viva; nunca dice
   cuál. `p_tenant` NULL (alta de un tenant nuevo) se compara con `IS DISTINCT FROM`, así que cualquier dueño cuenta.
   No reutiliza el resolvedor de ruteo: ese da NULL para un activo ya ambiguo, que quedaría libre para un tercero.
   Las tres funciones llevan `REVOKE ALL ... FROM PUBLIC` y `GRANT EXECUTE ... TO app_runtime` (ADR-0039 §3; los dos
   resolvedores eran ejecutables por PUBLIC desde 0003/0021) y `search_path = public, pg_temp`, para que una tabla
   temporal no pueda tapar las reales.
4. **Altas verificadas** (`core/gateway/meta_asset_claims.py`), en este orden:
   - `verify_waba_and_phone`: con las credenciales de la conexión, el token ve la WABA y el número está entre los de
     esa WABA (`list_phone_numbers`, paginado). Manual y Embedded Signup verifican con la app de la plataforma; BYOK,
     con la del negocio (ADR-0094).
   - `claim_assets`: en la **misma transacción** que inserta las filas, `pg_advisory_xact_lock` por activo (siempre
     WABA y luego número) y la función de reclamo. La verificación va primero, así solo pregunta "¿lo tiene otro?"
     quien ya demostró ante Meta que tiene acceso.
   - Embedded Signup: `persist_finish_payload` guarda solo el `business_id` (sujeto del token) y la marca de
     coexistence; la WABA y el número se guardan en `exchange_code` **después** del canje, verificados con el token
     nuevo, y la conexión sale de `draft` en esa misma transacción. Si algo falla, la conexión no cambia de origen
     ni avanza el wizard, y `last_error` guarda el motivo.
   - `onboard_tenant` y `save_connection` (tipo `whatsapp_meta`) usan la misma función de reclamo.
   - Sincronizar o agregar números (`sync_phone_numbers`, `add_phone_number`) toma el candado y **avisa** si otro
     negocio tiene el número, pero no bloquea: Graph los lista dentro de una WABA que este negocio ya tiene, así que
     la fila del otro es la ajena. El número queda ambiguo (deja de rutear por número) y decide la WABA.
5. **La migración aborta** si el rol que la corre no es superusuario ni tiene `BYPASSRLS`: con RLS forzada, las
   funciones verían 0 filas (ruteo siempre NULL, reclamo siempre "libre"), que es peor que fallar el despliegue. Con
   ese rol lista en el log, para revisarlos a mano: los activos que hoy reclaman dos negocios (dejan de rutear), las
   filas de conexiones en `draft` (dejan de contar) y los activos de altas manuales (se guardaron sin verificar que
   el número fuera de la WABA).

### Por qué una función `SECURITY DEFINER` más (y no una violación de ADR-0012)

ADR-0012 pide que la resolución sin RLS no crezca más allá de "resolver un id". La función de reclamo no devuelve un
tenant ni un catálogo: devuelve un booleano sobre un activo concreto. Es la única forma de saber, desde la
transacción de un negocio, si otro ya tiene ese activo (la RLS lo oculta, y por eso hoy el conflicto aparecía tarde,
como `IntegrityError`, y solo para números). No revela quién es el dueño.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener la WABA primero en el ruteo | El arreglo de las funciones no basta: un negocio que registra la WABA de otro con un número suyo seguiría recibiendo los mensajes de los números del otro |
| Que el número gane siempre, aunque la WABA sea de otro | Las filas de número tienen la verificación más débil: las heredadas no se verificaron, y `save_connection` (panel clásico, alta de tenant) comprueba el número pero no su WABA. Un número ajeno cargado así se llevaría los mensajes del dueño de la WABA |
| Ante dos dueños, elegir el más reciente (o el más antiguo) | "El más reciente" le da la victoria justo al ocupante, que llega después del dueño; "el más antiguo" deja al dueño legítimo sin salida si el ocupante llegó primero. NULL falla cerrado y queda registrado |
| `UNIQUE(waba_id)` en `waba_accounts` | Rompe casos legítimos del mismo negocio con la WABA en dos conexiones vivas (BYOK y manual, sandbox y producción) y no puede mirar `meta_connections.deleted_at` |
| `EXCLUDE USING gist (waba_id WITH =, tenant_id WITH <>)` | Exige la extensión `btree_gist`, falla con duplicados heredados y no ve si la conexión está borrada |
| Verificar el Embedded Signup más tarde (en `discover_assets`) | Las filas quedarían escritas con un `code` inventado: con el reclamo, el dueño legítimo quedaría bloqueado para siempre. Los IDs y el `code` llegan en la misma petición, así que basta con reordenar |
| Reutilizar `resolve_tenant_for_phone` para comprobar el reclamo | Da NULL para un número ya ambiguo: quedaría libre para un tercer negocio |

## Consecuencias

**Positivas:** un negocio ya no puede recibir mensajes de otro registrando su WABA o su número: las altas nuevas
verifican y reclaman, y las filas heredadas que contradicen a la WABA dejan de rutear. El conflicto se detecta antes de
escribir (mensaje claro, sin decir de quién es) y no como `IntegrityError` tardío. Un `code` inválido del Embedded
Signup responde 400 (`meta_error`) en vez de 500 y no deja filas.

**Negativas / lo que aceptamos pagar:**
- Una WABA pertenece a un solo negocio de la plataforma: dos negocios que compartan una WABA (dos sucursales con
  números distintos) deben ser un solo tenant o usar WABAs separadas.
- Si ya hay activos reclamados por dos negocios, o un número cuyo dueño no es el de su WABA, dejan de rutear (eventos
  `unresolved_tenant`) hasta que se borre la conexión que sobra; la migración lista los primeros y el webhook avisa
  de los segundos. Si el conflicto viene de un tercero con acceso real en Meta a la WABA (una agencia), el dueño
  legítimo deja de recibir hasta que soporte lo corrija: preferimos eso a entregar sus mensajes a otro.
- Las filas heredadas de un único negocio que no contradicen a nadie (una WABA cargada sin verificar por quien no es
  su dueño, y cuyo dueño no está en la plataforma) siguen contando hasta que se revisen; la migración lista las de
  altas manuales.
- Cada alta hace una llamada más a Graph (la lista de números de la WABA).
- Se mantiene el ruteo por WABA para los eventos sin número; si el número no resuelve (no registrado), decide la WABA.

**Qué haría que revisáramos esto:** un cliente real que necesite la misma WABA en dos tenants, o eventos
`unresolved_tenant` en producción por activos ambiguos que no se puedan corregir borrando una conexión.

## Fuera de alcance (anotado en `docs/BACKLOG.md`)

- El endpoint genérico junta los App Secrets de todos los tenants de las entries y procesa todas si la firma coincide
  con cualquiera: un negocio BYOK puede inyectar eventos en otro.
- `soft_delete_connection` borra físicamente `phone_numbers` y `waba_accounts` (invariante 8).
- `resolve_tenant_for_page`, `resolve_tenant_for_ig_user` y la resolución de plantillas por `meta_template_id`
  siguen con `LIMIT 1` sin desempate.
- Los eventos de plantillas resuelven su negocio por `message_template_id` sin compararlo con el de la entry: un
  negocio BYOK, firmando con su propio secreto, puede cambiar el estado de una plantilla de otro.
- `save_connection` (tipo `whatsapp_meta`, panel clásico en retiro) verifica el número contra Graph pero no su WABA.
  Con la regla de conflicto ya no entrega mensajes ajenos; conviene retirarla a favor de `POST /manual`.

## Impacto en el código

- `alembic/versions/0052_reclamo_de_numero_y_waba.py`: resolvedores y función de reclamo.
- `core/gateway/meta_asset_claims.py` (nuevo): `verify_waba_and_phone`, `claim_assets`, `asset_claimed_by_other_tenant`.
- `core/gateway/webhook_route_tenant.py`: número primero.
- `core/gateway/webhooks.py::_process_payload`: agrupa por `(entry, número)`.
- Altas: `core/api/routers/meta_wizard.py` (manual y `/wizard/exchange`), `core/gateway/meta_byok.py`,
  `core/gateway/meta_onboarding.py` (`persist_finish_payload`, `exchange_code`), `core/onboarding.py`,
  `core/gateway/connections.py::save_connection`, `core/gateway/meta_phone_numbers.py` (aviso al sincronizar).
- Tests: `tests/test_meta_asset_claims.py`, `tests/integration/test_meta_reclamo_waba_numero.py`.
