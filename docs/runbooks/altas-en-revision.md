# Altas automáticas en revisión (runbook)

El alta automática de negocios (HU-SH-03, ADR-0121) deja en `revision` lo que no puede resolver sola.
Cada alta es una fila de `provisioning_requests` en el negocio vendedor (`solvershub`). Avisa por push a
los dueños del vendedor con dos títulos:

- **"Alta automática en revisión"**: no se creó el negocio, o se creó pero no se pudo invitar al dueño.
- **"Alta sin acceso enviado"**: el negocio está creado, pero el correo con el acceso no salió (sin SMTP).

Los comandos se corren en el contenedor de la app o del worker (mismo entorno), con el **UUID** del
vendedor. Lo mismo se puede hacer desde el panel, en **Plataforma → Alta automática** (solo el master):
la cola de altas en revisión con aprobar y descartar, el interruptor y los documentos legales (HU-SH-07,
ADR-0125).

## 1. Ver las altas

```bash
agent-core tenant altas <uuid-del-vendedor>                 # las que están en revisión
agent-core tenant altas <uuid-del-vendedor> --estado todas  # todas, las más nuevas primero
```

Cada fila muestra la referencia (el número del pedido), el estado, los intentos, el negocio creado (si
lo hay) y el motivo. El detalle completo está en `provisioning_requests.detalle`: preset, entrega, pasos
y la huella del correo. Lo auditado está en `audit_log`, con actor `provisioning` y acciones
`aprovisionamiento_*` y `negocio_aprovisionado`.

## 2. Decidir según el motivo

| Motivo | Qué hacer |
|---|---|
| El pedido no trae un correo válido del dueño | Confirmar el correo con el cliente, corregir `datos_alta.email` del pedido y **reintentar** |
| El pedido todavía no es visible o no está pagado (3 intentos) | Revisar el pago del pedido; si se pagó, **reintentar** |
| Plan de la plataforma desconocido | Corregir el atributo `precio_de_plan` de la variante y **reintentar** |
| El vendedor no tiene una habilidad de pedidos | Habilitar `pack_commerce` en el vendedor y **reintentar** |
| Ya hubo un alta para este cliente o este correo (un Free más) | Es el antiabuso: **descartar**. Si es legítimo, que compre un plan pago o crear el negocio a mano desde Plataforma |
| No se puede invitar a ese correo (master, permisos globales o cuenta desactivada) | **Descartar** y pedirle al cliente otro correo (un pedido nuevo) |
| El cliente del pedido no coincide con el de la suscripción | Posible manipulación: revisar la conversación y **descartar** |
| El correo del pedido no es el de la cuenta que esta alta ya había preparado | El correo se corrigió después de crear la cuenta del anterior: volver al correo original y **reintentar**, o **descartar** y crear el negocio a mano (ver § 5) |
| El slug ya es de otro negocio | No debería pasar (el slug lleva un sufijo propio): **descartar** y crear el negocio a mano |
| La suscripción se activó sin un pedido | Activación manual o un journey: **descartar** (o crear el negocio a mano) |
| El alta automática está pausada por el master | Se pausó por un ataque (§ 7). Revisar cada una: **aprobar** las legítimas y **descartar** el resto. Al reanudar, también se pueden **reintentar** |
| No hay aceptación registrada de términos, privacidad y DPA | Faltaban documentos publicados (§ 8) o el pedido se creó a mano. Si el cliente aceptó por otro medio (p. ej. por correo, guardado), **aprobar**; si no, pedirle que acepte en un pedido nuevo y **descartar** este |
| Un Free por persona: ese teléfono o documento ya tuvo un Free en los últimos 12 meses | Antiabuso: **descartar** y ofrecerle un plan pago. Si es legítimo (otra persona con el mismo teléfono de la empresa, por ejemplo), **aprobar** |
| Señal de riesgo: correo de un dominio desechable | Escribirle al cliente para que use un correo permanente (pedido nuevo) y **descartar**; si es legítimo, **aprobar** |
| Señal de riesgo: N pedidos de alta de la misma persona en 24 h | Revisar las altas de esa persona (`altas --estado todas`): si son pruebas o abuso, **descartar**; si es un cliente con varios negocios, **aprobar** |
| Señal de riesgo: datos inconsistentes (documento_invalido, telefono_invalido) | Confirmar los datos con el cliente en la conversación; si cuadran, **aprobar** |

El cliente ya recibió por WhatsApp el mismo aviso que en un alta normal ("el acceso llega a tu correo"), así
que conviene escribirle si el alta se descarta.

## 3. Reintentar

```bash
agent-core tenant reintentar-alta <uuid-del-vendedor> <referencia>
```

Vuelve a correr el alta desde el principio y retoma el mismo negocio, porque el id y el slug son
deterministas: no crea otro. El reintento queda en `audit_log` (`aprovisionamiento_reintentado`, actor
`cli`). Si el problema sigue, el alta vuelve a revisión con el motivo nuevo.

## 3.b Aprobar (HU-SH-07)

```bash
agent-core tenant aprobar-alta <uuid-del-vendedor> <referencia>
```

Una persona revisó el alta y la deja seguir: corre **sin** los controles de riesgo que la frenaron
(interruptor, aceptación, un Free por persona, señales, Free repetido, recompra). El motivo lista **todos**
los controles que fallaron: leerlo entero antes de aprobar. Las validaciones de datos (plan, correo, pedido
pagado, cliente, owner) siguen aplicando. Queda en `audit_log` (`aprovisionamiento_aprobado`) con quién
aprobó y por qué estaba en revisión.

- Solo se aprueba un alta en revisión **por un control de riesgo** (los motivos de la tabla que dicen
  **aprobar**). Una en revisión por un dato (correo, cliente, pago, plan) se corrige y se **reintenta**.
- Desde el panel, aprobar deja el alta `en_curso` y el worker la corre en segundos (evento
  `provisioning.alta_aprobada` del outbox, con sus reintentos). Desde la CLI, además corre en el acto.
- La aprobación es de un solo uso: si el alta vuelve a revisión por otro motivo, hay que aprobarla de
  nuevo. Reintentar la borra y vuelve a aplicar todos los controles: sirve después de corregir un dato.

## 4. Descartar

```bash
agent-core tenant descartar-alta <uuid-del-vendedor> <referencia> --motivo "pedido de prueba"
```

El alta queda `omitido` con el motivo (`aprovisionamiento_descartado` en `audit_log`). Un evento repetido
del mismo pedido no la reabre. Si ya se había creado un negocio, sigue existiendo: se suspende o se purga
desde Plataforma.

## 5. El dueño no puede usar el enlace (409)

El enlace solo define la contraseña de una cuenta **limpia**: sin contraseña, sin acceso a ningún
negocio y sin otras invitaciones pendientes. Si el correo ya tiene otra invitación pendiente (por
ejemplo, compró dos negocios con el mismo correo) o acceso a otro negocio, el canje responde 409
(`account_exists`). En ese caso, el dueño:

1. Entra con **"olvidé mi contraseña"** usando su correo: el código le llega ahí y define la contraseña.
2. Inicia sesión y **acepta en la app** cada invitación pendiente (una por negocio).

El enlace de la invitación ya no hace falta.

Si el alta queda en revisión con "el correo del pedido no es el de la cuenta que esta alta ya había
preparado", es que el correo se corrigió después de que el alta creó la cuenta del correo anterior. El
enlace no se manda al correo nuevo. Hay que decidir a mano: volver al correo original y **reintentar**,
o **descartar** el alta y crear el negocio desde Plataforma con el correo correcto.

## 6. "Alta sin acceso enviado"

1. Configurar `SYSTEM_SMTP_*` (`docs/despliegue-coolify.md`).
2. El dueño entra con **"olvidé mi contraseña"** usando su correo: la cuenta se creó sin contraseña y el
   código de recuperación le sirve para definirla.
3. Después acepta la invitación en la app.

El enlace del alta deja de servir en cuanto la cuenta tiene contraseña.

## 7. Interruptor: pausar el alta automática ante un ataque

```bash
agent-core tenant interruptor-altas <uuid-del-vendedor> --pausar --motivo "ráfaga de Free"
agent-core tenant interruptor-altas <uuid-del-vendedor>             # ver el estado
agent-core tenant interruptor-altas <uuid-del-vendedor> --reanudar
```

O desde **Plataforma → Alta automática**. Con el alta pausada, los pedidos y las suscripciones siguen
registrándose, pero cada alta nueva queda en revisión ("el alta automática está pausada por el master") con
un push al equipo: nada se pierde. Pausar y reanudar quedan en `audit_log` del vendedor. Para cortar además
los pedidos, se pausa el asistente del vendedor (kill-switch del negocio). Si el ataque viene de un mismo
número, el cupo `ALTA_PEDIDOS_POR_HORA` ya lo frena.

## 8. Documentos legales (términos, privacidad y DPA)

Se publican desde **Plataforma → Alta automática → Documentos legales**: tipo, versión y enlace `https`.
Una versión nueva rige desde que se publica y no reemplaza la historia: cada aceptación guarda la versión
que vio el comprador (`pedidos_de_alta.aceptacion`, y en el negocio creado,
`tenants.settings["alta"]["aceptacion"]`). Conviene que el enlace sea propio de esa versión (p. ej.
`/terminos/2026-10`), para que lo aceptado no cambie si se edita la página.

Mientras falte alguno de los tres, el asistente no pide la aceptación y **toda** alta va a revisión con "no hay
aceptación registrada". Los textos de hoy son preliminares: cuando lleguen los del abogado (HU-GTM-02), se
publica una versión nueva de cada uno, sin tocar código. Una versión publicada no se edita ni se borra (la
base lo impide): si tiene un error, se publica otra.

## 9. Requisitos del vendedor para el antiabuso

- `checkout_fields.plan` del vendedor (configuración de comercio) tiene que pedir `id_document` (por
  defecto lo pide) y conviene que pida `phone`: "un Free por persona" usa esos datos además del número de
  WhatsApp.
- Un Free descartado sigue contando 12 meses para esa persona. Si alguien legítimo queda bloqueado
  (`free_no_disponible` en el chat), crear el negocio a mano desde Plataforma.
