# ADR-0168 — El token de push pertenece al dispositivo: se reasigna a quien lo registra y se libera en los demás negocios

- **Estado:** propuesto (se acepta al mergear).
- **Fecha:** 2026-10-26 (enmendado el mismo día: punto 4, liberación en otros negocios)
- **Contexto:** prueba en dispositivo del centro de notificaciones (ADR-0167). **Enmienda:** ADR-0099 (registro de dispositivos de push: regla de «token de otra cuenta»). **Usa:** ADR-0167 (push por categoría con destinatarios por visibilidad).

## Contexto

ADR-0099 rechazaba con `409 token_de_otra_cuenta` el registro de un token de Expo que ya estaba a nombre de otra cuenta del mismo negocio, para que una cuenta no pudiera «robar» los avisos de otra. En la práctica el token de Expo identifica al **teléfono**, no a la persona: cuando alguien cierra sesión y entra otra cuenta en el mismo teléfono (el dueño que presta el equipo, el colaborador que usa el del negocio, el probador que alterna cuentas), el segundo alta daba 409 y el dispositivo quedaba **sin push** hasta que el token se diera de baja a mano. Con el push como el mismo aviso del centro (ADR-0167), ese hueco deja a la persona con sesión activa sin enterarse de lo urgente.

El riesgo que la regla quería cubrir no existe en este modelo: para registrar un token hay que conocerlo, y el token solo lo conoce la app instalada en ese teléfono (no es un dato que viaje a otras cuentas ni aparezca en ninguna respuesta de la API).

## Decisión

1. **El alta reasigna.** `POST /api/v1/me/push-token` con un token que ya existe en el negocio lo deja a nombre de la cuenta autenticada, reactiva la fila si estaba dada de baja y actualiza la plataforma. Responde 204 como siempre; ya no existe `409 token_de_otra_cuenta`. Sigue habiendo una sola fila por `(tenant_id, token)` (índice único parcial) y por tanto una sola cuenta recibe en ese dispositivo: la anterior deja de recibir sin acción alguna.
2. **Se registra** con el log `push_token_reasignado` (negocio y cuenta nueva; nunca el token) para poder rastrear el cambio.
3. **La baja sigue siendo propia.** `DELETE /me/push-token` solo da de baja el token de la cuenta autenticada: una cuenta no puede apagar el dispositivo de otra aunque conozca el token. Cerrar sesión en la app da de baja el token (ADR-0099).
4. **Un teléfono, un solo negocio y una sola cuenta.** Tras registrar, el alta llama a `push_token_release_elsewhere(token, negocio, cuenta)` en la misma transacción: da de baja (`deleted_at = now()`, nunca borrado físico, invariante 8) toda fila **activa** con ese token exacto que no sea la recién registrada, sea de otro negocio o de otra cuenta. Se registra `push_token_liberado` con el conteo (nunca el token). Sin esto, la unicidad por `(tenant_id, token)` y la RLS dejaban al teléfono recibiendo los avisos de otro negocio bajo la cuenta anterior (visto en test el 2026-10-26: el OPPO con sesión en «Cobranza Demo» seguía recibiendo los de `solvershub` bajo la cuenta del dueño, registrada el 10-15 y nunca dada de baja). La app ya daba de baja el token del negocio anterior al cambiar de negocio, pero no cubre cerrar la sesión sin red ni el cambio de cuenta; esta es la garantía del servidor. Un teléfono con la misma cuenta en dos negocios recibe los avisos del que tiene activo (el último en el que se registró); al volver al otro negocio, la app lo registra de nuevo.

### Por qué la función SECURITY DEFINER es segura (migración 0109)

`push_tokens` tiene RLS forzada, así que desde el negocio activo no se ve ningún otro: la baja entre negocios exige saltar la RLS. Se hace con una única función, y se acota así:

- **Solo puede dar de baja.** Su única escritura es `UPDATE push_tokens SET deleted_at = now(), updated_at = now()` sobre filas activas; no inserta, no borra y no reactiva. Sin `RETURNING` hacia el llamador: devuelve un conteo, nunca filas, ids de cuenta ni de negocio, de modo que no filtra qué otros negocios tienen ese token.
- **Solo por token exacto.** El token de Expo solo lo conoce la app instalada en ese teléfono; no aparece en ninguna respuesta de la API. Quien lo envía ya demostró tenerlo, y el único efecto es apagar los avisos en *ese* dispositivo en otros negocios. No hay comodines ni filtros por negocio o cuenta ajenos.
- **Cierra en falso con parámetros vacíos.** Con token vacío o `p_keep_tenant`/`p_keep_admin` NULL no toca nada (un NULL liberaría la fila recién registrada).
- **Superficie mínima.** `search_path = public, pg_temp`, `REVOKE ALL … FROM PUBLIC` y `GRANT EXECUTE` solo a `app_runtime` (ADR-0039 §3). El dueño debe poder saltar la RLS (`BYPASSRLS`): la migración aborta si no (como 0104).
- **Quién la llama.** Solo `_registrar`, con el negocio y la cuenta del `Principal` ya autenticado (no valores del cuerpo de la petición), después de registrar y antes del commit.

Riesgo residual aceptado: quien conozca el token de Expo de otro teléfono podría apagar sus avisos en otros negocios (ya podía reasignarlo en el propio, punto 1). Dos altas simultáneas del mismo token en negocios distintos pueden liberarse mutuamente; la app vuelve a registrar al abrirse, así que se autocorrige.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mantener el 409 y pedir baja manual | Deja el dispositivo mudo tras un cambio de cuenta, que es el caso normal de uso compartido. |
| Dar de baja el token de la cuenta anterior al cerrar sesión | Se hace igualmente, pero depende de que el cierre llegue al servidor (sin red, app cerrada a la fuerza); no basta. |
| Reasignar solo si la cuenta anterior ya cerró sesión | El servidor no sabe si cerró sesión en ese teléfono; exigiría estado nuevo. |
| Hacer único el token en toda la plataforma (índice sobre `token`) | Choca con la RLS por negocio y con las filas ya dadas de baja; obligaría a un índice global sobre una tabla de negocio. |
| Que el cliente pida la baja en los otros negocios | Una cuenta no ve los otros negocios de otra cuenta, y la baja propia no alcanza las filas de la cuenta anterior. |

## Consecuencias

**Positivas:** el dispositivo siempre recibe los avisos de la cuenta con sesión en él, y solo de ese negocio; sin tablas nuevas (una función en la migración 0109).

**Negativas / lo que aceptamos pagar:** quien conozca un token de Expo de otro dispositivo del mismo negocio podría reasignárselo; el token no se expone en ninguna respuesta y solo lo ve el teléfono, por lo que lo damos por aceptable. La traza `push_token_reasignado` queda para auditar.

**Qué haría que revisáramos esto:** exponer el token en alguna respuesta de la API o registrar dispositivos con un identificador que no sea el token de Expo.

## Impacto en el código

- `core/api/routers/push.py` (`_registrar`), `alembic/versions/0109_push_token_liberar_en_otros_negocios.py`, `tests/integration/test_push_tokens.py` (`test_token_de_otra_cuenta_se_reasigna_al_dispositivo`, sustituye al del 409, y los de liberación entre negocios).
