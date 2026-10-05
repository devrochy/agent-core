# ADR-0096 — Un evento del webhook solo actúa sobre el negocio que lo firmó

- **Estado:** aceptado
- **Fecha:** 2026-10-05
- **Enmienda:** ADR-0040, fila de "Alternativas consideradas" sobre probar varios secretos de firma: el endpoint genérico
  sí prueba varios, pero cada secreto autoriza solo los eventos de su dueño (el de la plataforma, los de todos).
- **Contexto:** hallazgo alto preexistente del `arquitecto` al revisar ADR-0095; relacionado con ADR-0028 (`webhook_events`),
  ADR-0031 (plantillas por webhook), ADR-0032 (estados de mensajes) y ADR-0095 (ruteo por número).

## Contexto

`POST /webhooks/whatsapp` recibe eventos de varios negocios en un mismo POST (todos los suscritos a la misma app de
Meta). Desde el arreglo del 2026-09-29 (BYOK firmaba con su app y todo daba 401), juntaba en una lista el App Secret
de cada negocio del payload más el de la plataforma, aceptaba la firma con **cualquiera** y procesaba **todas** las
entries. Un negocio con app propia conoce su secreto: firmaba un POST con una entry suya y otra con el número de A, y
le inyectaba mensajes a A, que su agente respondía con las credenciales de A al número que eligiera el atacante.

Además, los eventos de plantillas (`message_template_*`) y de estado (`statuses`) resuelven el negocio por el ID de la
plantilla o del mensaje y escribían en él sin compararlo con el negocio de la entry: con una firma válida propia, un
negocio cambiaba el estado de una plantilla ajena o marcaba como fallidos los mensajes de otro, también por el
endpoint por slug.

## Decisión

Cada grupo de eventos `(entry, número)` se procesa solo si lo firmó la plataforma o su propio negocio, y ningún evento
escribe en un negocio distinto del de su grupo.

1. **Quién firmó** (`core/gateway/webhooks.py::_firmantes_del_post`). Se rutea el POST una sola vez
   (`_rutear_grupos`, con `route_event` de ADR-0095) y se verifica la firma:
   - con el secreto de la **plataforma** (`connections.platform_app_secret`): autoriza a cualquier negocio. Ningún
     negocio lo conoce, y es con el que Meta firma las conexiones BYOK que tomó el Embedded Signup;
   - si no, con el secreto **propio** de cada negocio de los grupos (`connections.resolve_own_app_secret`: conexión
     manual → BYOK, sin caer al `.env`): autoriza solo a ese negocio. Un grupo en conflicto no aporta candidato.

   Sin plataforma ni negocio que verifique: 401, como hasta ahora. Sin ningún secreto contra el que verificar:
   fail-closed (HU-ARCH-05), con el escape de test intacto; un error al leer el secreto de un negocio cuenta como
   "tiene secreto", así nunca habilita ese escape.
2. **La puerta por grupo** (`_process_payload`, ahora con `firmantes` obligatorio). Tras resolver el negocio (conflicto
   → mismatch del slug → sin resolver), un grupo de un negocio que no firmó queda en `webhook_events` con
   `process_error="signature_tenant_mismatch"`, `signature_ok=False` y sin negocio, se loguea y se alerta (una vez por
   ventana). Va antes del chequeo de facturación, para no revelarle a quien firmó el estado de otro negocio. La
   respuesta sigue siendo 200 sin contar los rechazos: un 5xx haría que Meta reintentara en vano. El endpoint por slug
   pasa su propio negocio (`Firmantes.de_negocio`) y no cambia.
3. **Plantillas y estados** (`meta_template_webhooks.handle_template_*`, `meta_message_status.apply_status_update`)
   reciben `expected_tenant_id`, obligatorio: el negocio del grupo. Si el resolvedor `SECURITY DEFINER` da otro, el
   evento se descarta con un aviso; si no, el scope y las consultas usan el negocio esperado. No se escribe la fila,
   ni un `MessageCost`, ni se manda la alerta de "plantilla rechazada" de otro negocio.
4. **El cron de "worker caído"** (`count_stalled_webhook_events`) deja de contar las filas rechazadas
   (`process_error` no nulo): nunca se marcan procesadas, y un negocio podía provocar el rechazo a voluntad. Y
   marcar un evento como procesado filtra por el negocio del grupo: un `external_id` repetido por B no marca la fila
   de A.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| 401 para todo el POST si algún grupo no está autorizado | Un POST legítimo de la app de B perdería sus propios eventos y Meta lo reintentaría en vano |
| Validar el genérico solo con el secreto de la plataforma | Vuelve el bug del 2026-09-29: las entregas BYOK al genérico dan 401 |
| Obligar a los negocios BYOK a usar solo el endpoint por slug | Exige migrar las suscripciones ya registradas en Meta; el genérico seguiría aceptando lo que le llegue |
| Que el secreto de la plataforma solo autorice a negocios sin app propia | Rompe las conexiones BYOK tomadas por Embedded Signup hasta HU-CONN-05, sin cerrar ningún hueco (nadie más conoce ese secreto) |
| En los handlers, buscar la fila dentro del scope esperado sin el resolvedor | Igual de seguro, pero pierde la señal "es de otro negocio" para el log |
| Registrar el grupo rechazado con el `tenant_id` del negocio que no firmó | Le atribuiría a A un evento que A no mandó; se registra sin negocio, como `tenant_mismatch` y `tenant_conflict` |

## Consecuencias

**Positivas:** un negocio ya no puede inyectar mensajes en otro, ni cambiar sus plantillas o el estado de sus mensajes,
con su propio secreto, por ninguno de los dos endpoints. La firma y el despacho usan la misma resolución (ADR-0095),
una sola vez por POST. El cron de salud deja de alertar en falso por eventos rechazados.

**Negativas / lo que aceptamos pagar:**
- Dos negocios con la misma app (el mismo App Secret) se autorizan entre sí: no se puede distinguir con criptografía, y
  quien tiene el secreto de A ya puede hacerse pasar por su app ante Meta. Se registra `webhook_secreto_compartido`.
- Un negocio con varias conexiones solo autoriza con su secreto efectivo (manual, luego el BYOK más reciente), como
  hasta hoy: una entrega firmada por otra de sus apps sigue dando 401. HU-CONN-05 debería dar el secreto de la conexión
  dueña del número o la WABA del grupo (`_secretos_propios` ya devuelve una tupla).
- La WABA de A suscrita a la app BYOK de B que apunta al genérico se procesaba; ahora se rechaza y alerta.
- Quien conozca el secreto de la plataforma (`WHATSAPP_APP_SECRET`, o `META_APP_SECRET` si el primero está vacío)
  autoriza a cualquier negocio. Si alguno es el de la app de un cliente concreto (heredado del monolito), ese cliente
  puede firmar "como plataforma": hay que confirmarlo antes de desplegar.
- Una fila rechazada puede ocupar de antemano el `external_id` de un evento legítimo en `webhook_events`
  (`ON CONFLICT DO NOTHING`; en plantillas es un hash del `value`). Solo se pierde la fila de auditoría, no el
  procesamiento; ya pasaba con los otros motivos de rechazo.
- Messenger e Instagram usan la misma regla con el secreto propio de WhatsApp del negocio (`resolve_own_app_secret`): autoriza solo sus propios grupos, nunca los de otro. Hoy no hay clientes.

**Qué haría que revisáramos esto:** HU-CONN-05 (un secreto por conexión), filas `signature_tenant_mismatch` en
producción que vengan de configuraciones legítimas, o que Meta empiece a mandar en el POST qué app lo firmó.

## Impacto en el código

- `core/gateway/connections.py`: `platform_app_secret`, `resolve_own_app_secret`; `resolve_effective_app_secret` queda
  igual (`own or platform`).
- `core/gateway/webhooks.py`: `_Grupo`, `_rutear_grupos`, `Firmantes`, `_firmantes_del_post` (reemplaza a
  `_tenant_y_secretos`), `receive_whatsapp`, la puerta en `_process_payload`, `signature_ok` en `_log_webhook_event`,
  `_alert_on_firma_ajena`, el filtro de `count_stalled_webhook_events` y los dispatchers con el negocio del grupo.
- `core/gateway/meta_template_webhooks.py`, `core/gateway/meta_message_status.py`: `expected_tenant_id`.
- Tests: `tests/test_webhook_byok_signature.py` (reescrito), `tests/integration/test_webhook_firma_por_negocio.py`
  (nuevo) y los llamadores de los handlers.
