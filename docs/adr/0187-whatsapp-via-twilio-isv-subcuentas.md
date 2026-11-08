# ADR-0187 — WhatsApp vía Twilio sin cuenta del negocio: solvershub como ISV con una subcuenta por WABA

- **Estado:** propuesto. Su construcción está **bloqueada** hasta que Meta verifique a solvershub y apruebe su app de Tech Provider.
- **Fecha:** 2026-11-07
- **Contexto:** D-63 (docs), HU-CONN-18 (SPEC-08). Documentación de Twilio consultada el 2026-11-07: Tech Provider Program, su guía de integración, Senders API v2 y Self Sign-up.
- **Usa:** ADR-0186 (conexión Twilio BYOK), ADR-0030 (wizard reanudable), ADR-0128 (Embedded Signup real), ADR-0094 (credenciales por conexión).

## Contexto

Con ADR-0186 el negocio necesita cuenta Twilio propia y registrar su número con Self Sign-up en la consola de Twilio. El dueño quiere que el negocio gestione lo menos posible. Twilio solo permite que una plataforma registre los números de sus clientes si se cumple todo esto:

- La plataforma es **Meta Tech Provider**, con una **app de Meta nueva** (no se reutiliza la de ADR-0128) y aprobada.
- Tiene la **verificación de negocio en Meta** y 2FA.
- Aceptó la **Partner Solution** de Twilio (`solutionID`).
- Tiene **una subcuenta Twilio por cada WABA de cliente** (es obligatorio).

Según Twilio, los dos primeros pasos toman de 3 a 4 semanas. Hoy solvershub no tiene la verificación de Meta.

## Decisión

**Cuando solvershub cumpla los requisitos de Tech Provider, el negocio podrá conectar WhatsApp por Twilio sin cuenta propia: el sistema le crea una subcuenta Twilio, el negocio pasa por el Embedded Signup de Meta con la Partner Solution de Twilio y el sistema registra el número con la Senders API. Para todo lo demás (webhooks, plantillas, envío, salud) se reutiliza la conexión de ADR-0186 tal cual.**

1. **Subcuentas.** Se crea la tabla `twilio_subaccounts`: `tenant_id`, `account_sid`, `auth_token_encrypted` (AES-GCM, con el negocio y la subcuenta como AAD), `waba_id` (único global vivo), `friendly_name`, `status` y `deleted_at`. Lleva RLS forzado.
   - Se crea con `POST /2010-04-01/Accounts.json` usando la cuenta principal de solvershub. Sus credenciales viven en la configuración de la plataforma, nunca en el panel.
   - Hay una subcuenta por WABA. Si un negocio tiene dos WABA, tiene dos subcuentas.
2. **La conexión no copia el token.** En fase 2, `credentials_encrypted` guarda solo `{"subaccount_id"}` y `resolve_credentials` resuelve el SID y el token desde `twilio_subaccounts`. Al rotar el token de la subcuenta se cambia en un solo lugar.
3. **Wizard reanudable.** Usa `kind='twilio_isv'` sobre `connection_wizards` (ADR-0030). Pasos:
   1. Crear la subcuenta.
   2. Elegir el número: uno del negocio o uno comprado en Twilio dentro de la subcuenta, asignado antes del signup.
   3. Embedded Signup v4.
   4. Registrar el sender.
   5. Verificar el OTP.
   6. Esperar `ONLINE`.
   7. Configurar los webhooks como en ADR-0186.
   El wizard guarda `next_allowed_at`, porque Twilio pide «varios minutos» entre llamadas a la Senders API. Un candado Redis por subcuenta evita llamadas en paralelo.
4. **Embedded Signup parametrizado.** Se reutilizan `embedded-signup.tsx` y `meta_wizard.py`, con estos parámetros por proveedor: App ID y secreto de la app nueva, `config_id` (Embedded Signup v4 con token de system user y solo `whatsapp_business_management` y `whatsapp_business_messaging`) y `extras.setup.solutionID`. El evento `FINISH` devuelve `waba_id` y `phone_number_id`. El número, si es del negocio, se le pide aparte.
5. **Registro del sender.**
   - `POST /v2/Channels/Senders` con las credenciales de la subcuenta: `sender_id=whatsapp:+E164`, `configuration.waba_id` (solo en el primer número de la WABA), `verification_method` (`sms` o `voice`) y `profile`.
   - El OTP se confirma con `verification_code`.
   - Con un número Twilio con SMS, el OTP del primer sender se captura apuntando el `SmsUrl` del `IncomingPhoneNumber` a un endpoint nuestro. Ese endpoint está firmado, se busca por wizard y guarda el código solo mientras el wizard lo necesita. Los números siguientes de la WABA los verifica Twilio solo.
   - Los errores 63104, 63110, 63113, 63114 y 63116 se traducen a mensajes del panel.
6. **Producción.** La app de Meta pasa a Live y las URL de producción se registran en Facebook Login for Business. Los límites de la WABA del negocio (2 senders y tope de mensajes hasta que Meta lo verifique) se muestran en la ficha.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Una sola cuenta Twilio de solvershub con todos los números | Twilio exige una cuenta o subcuenta por WABA. Además, toda la facturación y el riesgo de baneo quedarían en solvershub |
| Que el operador de solvershub haga Self Sign-up en la consola de una subcuenta por el negocio | El popup de Meta necesita el login de Facebook del negocio. Hacerlo por él es manejar credenciales ajenas, y Twilio lo considera un uso de ISV que exige el programa |
| Copiar el AuthToken de la subcuenta en `credentials_encrypted` | Dos fuentes de verdad para un mismo secreto, que hay que rotar en las dos |
| No hacer fase ISV y quedarse con Meta directo (ADR-0128) cuando solvershub quede verificado | Es una opción real: ver la condición de revisión |

## Consecuencias

**Positivas**
- El negocio solo pasa por el popup de Facebook y por un código.
- Twilio factura a solvershub por subcuenta, lo que abre la reventa (decisión comercial aparte).

**Negativas / lo que aceptamos pagar**
- Una segunda app de Meta que mantener.
- Un trámite con Twilio (Partner Solution).
- La plataforma guarda credenciales de subcuentas de todos los negocios.
- Los clientes que ya se dieron de alta por Self Sign-up no pueden pasar su WABA existente a la Partner Solution.

**Qué haría que revisáramos esto**
- Que, con solvershub verificado, el Embedded Signup directo de Meta (ADR-0128) dé la misma experiencia al negocio y Twilio no aporte nada concreto (números, facturación, soporte). En ese caso esta fase no se construye y ADR-0186 queda para los negocios que ya usan Twilio.
- Que Twilio cambie los requisitos del programa.

## Impacto en el código

**Backend**
- Nueva `twilio_subaccounts` (migración).
- `connection_wizards.kind` gana `twilio_isv`.
- `core/gateway/twilio/` gana los clientes de subcuentas y del registro de senders.
- `resolve_credentials` resuelve la subcuenta.
- Endpoint de captura del OTP.

**Panel**
- Asistente de alta ISV con el Embedded Signup parametrizado.

**Docs**
- HU-CONN-18.

**Orden de construcción:** F2-1 subcuentas → F2-2 wizard y Senders API → F2-3 panel → F2-4 prueba con una WABA real.
