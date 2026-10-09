# Altas automáticas en revisión (runbook)

El alta automática de negocios (HU-SH-03, ADR-0121) deja en `revision` lo que no puede resolver sola.
Cada alta es una fila de `provisioning_requests` en el negocio vendedor (`solvershub`). Avisa por push a
los dueños del vendedor con dos títulos:

- **"Alta automática en revisión"**: no se creó el negocio, o se creó pero no se pudo invitar al dueño.
- **"Alta sin acceso enviado"**: el negocio está creado, pero el correo con el acceso no salió (sin SMTP).

Los comandos se corren en el contenedor de la app o del worker (mismo entorno), con el **UUID** del
vendedor.

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

El cliente ya recibió por WhatsApp el mismo aviso que en un alta normal ("el acceso llega a tu correo"), así
que conviene escribirle si el alta se descarta.

## 3. Reintentar

```bash
agent-core tenant reintentar-alta <uuid-del-vendedor> <referencia>
```

Vuelve a correr el alta desde el principio y retoma el mismo negocio, porque el id y el slug son
deterministas: no crea otro. El reintento queda en `audit_log` (`aprovisionamiento_reintentado`, actor
`cli`). Si el problema sigue, el alta vuelve a revisión con el motivo nuevo.

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
