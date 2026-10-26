# ADR-0168 — El token de push pertenece al dispositivo: se reasigna a quien lo registra

- **Estado:** propuesto (se acepta al mergear).
- **Fecha:** 2026-10-26
- **Contexto:** prueba en dispositivo del centro de notificaciones (ADR-0167). **Enmienda:** ADR-0099 (registro de dispositivos de push: regla de «token de otra cuenta»). **Usa:** ADR-0167 (push por categoría con destinatarios por visibilidad).

## Contexto

ADR-0099 rechazaba con `409 token_de_otra_cuenta` el registro de un token de Expo que ya estaba a nombre de otra cuenta del mismo negocio, para que una cuenta no pudiera «robar» los avisos de otra. En la práctica el token de Expo identifica al **teléfono**, no a la persona: cuando alguien cierra sesión y entra otra cuenta en el mismo teléfono (el dueño que presta el equipo, el colaborador que usa el del negocio, el probador que alterna cuentas), el segundo alta daba 409 y el dispositivo quedaba **sin push** hasta que el token se diera de baja a mano. Con el push como el mismo aviso del centro (ADR-0167), ese hueco deja a la persona con sesión activa sin enterarse de lo urgente.

El riesgo que la regla quería cubrir no existe en este modelo: para registrar un token hay que conocerlo, y el token solo lo conoce la app instalada en ese teléfono (no es un dato que viaje a otras cuentas ni aparezca en ninguna respuesta de la API).

## Decisión

1. **El alta reasigna.** `POST /api/v1/me/push-token` con un token que ya existe en el negocio lo deja a nombre de la cuenta autenticada, reactiva la fila si estaba dada de baja y actualiza la plataforma. Responde 204 como siempre; ya no existe `409 token_de_otra_cuenta`. Sigue habiendo una sola fila por `(tenant_id, token)` (índice único parcial) y por tanto una sola cuenta recibe en ese dispositivo: la anterior deja de recibir sin acción alguna.
2. **Se registra** con el log `push_token_reasignado` (negocio y cuenta nueva; nunca el token) para poder rastrear el cambio.
3. **La baja sigue siendo propia.** `DELETE /me/push-token` solo da de baja el token de la cuenta autenticada: una cuenta no puede apagar el dispositivo de otra aunque conozca el token. Cerrar sesión en la app da de baja el token (ADR-0099).
4. **Límite heredado.** La reasignación ocurre solo dentro del negocio activo en el momento del alta (`push_tokens` es por negocio, ADR-0099). Si la misma persona usa el teléfono en dos negocios, el token queda registrado en cada uno por separado, y cada alta reasigna dentro de su negocio.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener el 409 y pedir baja manual | Deja el dispositivo mudo tras un cambio de cuenta, que es el caso normal de uso compartido. |
| Dar de baja el token de la cuenta anterior al cerrar sesión | Se hace igualmente, pero depende de que el cierre llegue al servidor (sin red, app cerrada a la fuerza); no basta. |
| Reasignar solo si la cuenta anterior ya cerró sesión | El servidor no sabe si cerró sesión en ese teléfono; exigiría estado nuevo. |

## Consecuencias

**Positivas:** el dispositivo siempre recibe los avisos de la cuenta con sesión en él; sin estado nuevo ni migración.

**Negativas / lo que aceptamos pagar:** quien conozca un token de Expo de otro dispositivo del mismo negocio podría reasignárselo; el token no se expone en ninguna respuesta y solo lo ve el teléfono, por lo que lo damos por aceptable. La traza `push_token_reasignado` queda para auditar.

**Qué haría que revisáramos esto:** exponer el token en alguna respuesta de la API o registrar dispositivos con un identificador que no sea el token de Expo.

## Impacto en el código

- `core/api/routers/push.py` (`_registrar`), `tests/integration/test_push_tokens.py` (`test_token_de_otra_cuenta_se_reasigna_al_dispositivo`, sustituye al del 409).
