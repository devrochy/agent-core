# ADR-0214 — Programa de referidos: código del owner, atribución por el asistente y créditos `bonificacion` a los dos negocios

- **Estado:** propuesto (2026-11-27). Diseño aprobado por el dueño el mismo día con los valores por defecto y la reversa
  de pagos dentro del alcance. Revisado por el agente `arquitecto` (siete bloqueos corregidos). Sin código todavía; el
  código va en HU-REF-02 a HU-REF-17.
- **Fecha:** 2026-11-27
- **Contexto:** D-83 y SPEC-55 (`solvershub-docs`), HU-REF-01. Revisado contra `origin/develop` `dc75e3a8`.
  **Usa:** ADR-0213 §1 (tipo de abono `bonificacion`), §2 (orden de gasto), §3 (arrastre al cierre) y §10 (lo que
  muestra `GET /admin/plan`). ADR-0213 está **propuesto** y solo existe como borrador en `solvershub-docs`
  (SPEC-54, solvershub-docs#340); su código mínimo ya está en develop por HU-FB-02 (#945: migración
  `0161_credit_grants_bonificacion` y `core/billing/bonificacion.py`). También usa ADR-0012 (funciones
  `SECURITY DEFINER` mínimas para resolver el tenant), ADR-0107 (eventos de dominio durables con outbox), ADR-0121
  (alta de negocios), ADR-0167 (centro de notificaciones) y ADR-0209 (`credit_grants` por ciclo, solo de inserción).
  **Cambia:** `subscription_payments` suma `revertido_en` (§10).
  **No cambia:** `credit_grants`, `tenant_subscriptions`, `provisioning_requests`, `acreditar_bonificacion` ni el
  contrato `AgentSpecV3` (invariante 4).

## Contexto

El owner de un negocio activo quiere recomendar SolversHub y que eso valga algo. D-83 pide:

- que el owner (también en Free) tenga un código propio y lo comparta como `solvershub.site/?ref=CODIGO`;
- que el asistente de solvershub registre el código cuando el prospecto lo escribe en WhatsApp;
- que, cuando el negocio referido quede **dado de alta** (automática, en revisión o manual) **y** su **primer pago de
  plan** sea aprobado, los dos negocios reciban créditos `bonificacion` según el plan comprado, después de una espera
  de 15 días (parametrizable) en la que el pago siga vigente;
- topes por referidor y del programa, antiabuso, y que un pago revertido antes de liberar anule la recompensa.

Seis hechos del código condicionan el diseño:

1. **La puerta única del pago de plan no emite eventos.** `aplicar_pago_de_periodo` (`core/billing/service.py:151`)
   aplica el pago de cualquier canal; solo `apply_payment` (`core/billing/checkout.py:448`) emite
   `platform.period_paid`, y solo en el canal panel. La puerta la llaman el alta (`core/provisioning/consumer.py:530`)
   y el paso de Free a pago (`consumer.py:948`), entre otros.
2. **El pago llega antes que el alta.** En el consumidor del alta, `_registrar_periodo` (`consumer.py:530`) corre antes
   que `_completar` (`consumer.py:555`).
3. **La reversa de un pago de plan se ignora y no deja rastro.** Si un pago de plan `aprobado` llega como `rechazado`,
   `apply_payment` devuelve `False` sin cambiar el pago (`checkout.py:508-514`); solo la recarga revertida alerta. El
   pago sigue `aprobado` para siempre. Además, sale antes de esa rama si
   `payment.provider in (PROVEEDOR_RENOVACION, "asistente")` (`checkout.py:506-507`).
4. **El abono ya existe.** `core.billing.bonificacion.acreditar_bonificacion(session, tenant_id, origen, creditos, *,
   now)` inserta una `bonificacion` en el ciclo actual con `ON CONFLICT DO NOTHING` sobre
   `(tenant_id, ciclo, tipo, origen)` y devuelve `acreditada | ya_acreditada | sin_ciclo`. Exige `creditos` entero
   mayor que 0, corre en la transacción del llamador y no toma el candado del saldo. Free tiene `period_start` y
   crédito mensual, así que un referidor en Free tiene ciclo donde abonar.
5. **La unicidad del abono incluye `ciclo`.** Un reintento que cae en otro ciclo inserta otra fila; el cierre arrastra
   el remanente con el mismo `origen`, pero un abono ya gastado no se arrastra y no lo protege.
6. **El owner del negocio nuevo no está en `tenant_user` al completarse el alta.** La membresía nace al aceptar la
   invitación (`core/provisioning/owner.py:135-147`, `core/api/routers/platform.py:196-197`), y el alta manual no crea
   `ProvisioningRequest` ni `PedidoDeAlta`.

## Decisión

**El programa vive en el core (`core/referrals/`), con tres tablas de negocio con RLS forzada y cinco funciones
`SECURITY DEFINER` que devuelven solo ids o una suma. La atribución la registra una tool del asistente en el tenant
vendedor; un referido nace al completarse el alta y califica con el primer pago de plan que califica, en el orden en
que lleguen. Un cron en serie libera los dos abonos `bonificacion` tras la espera, en transacciones separadas y sin
candados de dos negocios a la vez. Tres eventos nuevos por el outbox conectan el alta, el pago y su reversa, y la
reversa queda guardada en el pago.**

### 1. Datos

Migración de referidos: la siguiente libre al abrir el PR (la 0162 está reservada para las tablas de HU-FB-03 según la
0161). Las tres con `tenant_id uuid not null`, RLS habilitada y **forzada** con la policy `nullif` de
`0155_credit_grants`, sin DELETE para `app_runtime` y `prevent_mutation()` en DELETE y TRUNCATE.

| Tabla | Tenant dueño | Escribe | Lee |
|---|---|---|---|
| `referral_codes` | referidor | owner (obtener o crear, regenerar) | owner; `referral_resolve_code` |
| `referral_attributions` | vendedor (`solvershub`) | tool del asistente; handler del alta | handler del alta |
| `referrals` | referidor | handlers, cron, master | owner («mis referidos»); master vía `referral_bandeja` |

- **`referral_codes`**: `codigo` de 8 caracteres de un alfabeto sin `0 O 1 I L` (≈ 2^40 combinaciones), aleatorio,
  no derivado del slug, guardado en mayúsculas; `estado` (`activo|retirado`); único parcial
  `(tenant_id) WHERE estado='activo'`.
- **`referral_attributions`**: `customer_id`, `conversation_id`, `codigo`, `referidor_tenant_id`, `estado`
  (`vigente|consumida|descartada`), `vence_en`. Único parcial `(tenant_id, customer_id) WHERE estado='vigente'`.
  Sin PII.
- **`referrals`**: `referido_tenant_id` (uuid **sin FK**: es otro negocio), `referido_nombre` (snapshot),
  `alta_referencia`, `via` (`asistente|revision|manual`), `codigo`, `estado`
  (`atribuido|en_espera|otorgado|anulado|rechazado`), `motivo_codigo`, `motivo_publico`.
  - **Congelados al calificar:** `pago_id`, `plan_id`, `recompensa_referidor`, `recompensa_referido` (enteros, porque
    `acreditar_bonificacion` exige `int`), `espera_dias`, `config_version`, `calificado_en`, `libera_en`.
  - **Resultado:** `otorgado_en`, `anulado_en`, `regalo_referido_estado` (`pendiente|entregado|omitido`).
  - **Operación:** `intentos_liberacion`, `ultimo_motivo_espera`, `updated_at`.
- **Invariantes en el esquema:** `UNIQUE (referido_tenant_id)` (un negocio se refiere una vez en su vida);
  `CHECK (tenant_id <> referido_tenant_id)`; CHECK de estados; `en_espera` y `otorgado` exigen los congelados;
  `anulado` y `rechazado` exigen `motivo_codigo`.
- **Trigger `BEFORE UPDATE` de inmutabilidad** en `referrals`. Compara solo el estado y los congelados, y enumera por
  estado lo que se puede escribir:
  - `atribuido` y `en_espera`: el estado (por las transiciones de arriba), los congelados al pasar a `en_espera`, y las
    columnas de operación;
  - `otorgado`: solo `regalo_referido_estado` de `pendiente` a `entregado` u `omitido`, y las columnas de operación;
  - `anulado` y `rechazado`: solo las columnas de operación.
  El trigger **no** es `ENABLE ALWAYS`: `purge_tenant` (0072) depende de `session_replication_role = replica`.
- **Dos excepciones declaradas a la invariante 1 y a la 8:**
  - **Índices que no empiezan por `tenant_id`:** el único global por `referral_codes.codigo` y
    `UNIQUE (referrals.referido_tenant_id)`. Son reglas entre negocios que no se pueden expresar por tenant; tienen
    precedente en `uq_subscription_payments_transaccion` (0158).
  - **Sin `deleted_at`:** las tres tablas son mutables pero nunca se borran; `estado` hace de baja lógica (un código
    `retirado`, una atribución `descartada`, un referido `anulado`). `purge_tenant` las borra igual.
- **Lo que sobrevive a una purga:** al purgar al referido queda `referido_nombre` en el referidor; al purgar al
  referidor quedan atribuciones con su `referidor_tenant_id` en el vendedor. El flujo de purga (`core/tenants.py`)
  anonimiza `referido_nombre` y descarta esas atribuciones, escribiendo bajo el scope del dueño de cada fila.

**Estados:**

```
(sin fila) ──negocio completado + código válido──► atribuido
atribuido ──pago que califica + antiabuso OK──► en_espera
atribuido ──antiabuso falla / plan sin recompensa──► rechazado
atribuido ──sin pago en vigencia_pago_dias | pago revertido | master──► anulado
en_espera ──libera_en vencido + condiciones OK──► otorgado
en_espera ──pago revertido | referido suspendido o eliminado | referidor sin ciclo > tope | master──► anulado
```

`calificado` es un instante (`calificado_en`), no un estado de reposo.

### 2. Funciones entre negocios (ADR-0012)

Cinco funciones `SECURITY DEFINER`, de solo lectura, con `REVOKE ALL FROM PUBLIC` y `GRANT EXECUTE` solo al rol de la
app, con el patrón de `feedback_bandeja` (ADR-0213 §6) y `admin_meta_connection_ids` (0033). **Devuelven solo ids,
tenants o una suma**; el contenido se lee después bajo `tenant_scope` del dueño:

- `referral_resolve_code(p_codigo)` → `tenant_id` del referidor si el código está activo, o null.
- `referral_del_negocio(p_referido_tenant_id)` → `(id, tenant_id)` del referido, o null. La usan los handlers de pago y
  reversa para salir en O(1) si el negocio no es referido.
- `referral_trabajo_pendiente(p_ahora, p_limit)` → `(id, tenant_id, motivo)` para el cron: espera vencida, regalo
  pendiente, atribuido sin pago fuera de vigencia, y **atribuido con un pago que califica** (cubre la carrera en la que
  el evento de pago corre antes del commit del nacimiento).
- `referral_bandeja(p_estado, p_tenant, p_cursor_ts, p_cursor_id, p_limit)` → `(id, tenant_id, created_at, estado)`
  para el master, paginada por `(created_at, id)`.
- `referral_creditos_otorgados_mes(p_desde)` → la suma de `recompensa_referidor + recompensa_referido` de los
  `otorgado` desde `p_desde`, para el tope del programa (§6). Solo un número.

**Disparador de revisión de ADR-0012 (sus líneas 45-47):** ese ADR pide revisar el modelo cuando las funciones
definer pasan de dos. Este ADR suma cinco, todas de solo lectura, sin contenido de negocio y con su test de
aislamiento. Se acepta porque la alternativa (las tablas en el tenant vendedor, ver Alternativas) cambia de dueño los
datos del referidor; si aparece una sexta función de referidos, se revisa esa alternativa.

**Las escrituras nunca saltan la RLS:** se resuelve el tenant y se escribe dentro de `tenant_scope(<dueño del dato>)`.

### 3. Eventos (ADR-0107)

Tres eventos nuevos en `core/shared/events.py`, por el outbox de `domain_events`, emitidos en la transacción del
cambio que los origina y **en el tenant del negocio referido** (el payload lleva el slug del vendedor):

| Evento | Emisor | `dedupe_key` |
|---|---|---|
| `provisioning.negocio_completado` | `_completar` y el alta manual (`POST /admin/platform/tenants` con `codigo_referido` opcional) | `negocio_completado:{tenant_id}` (el negocio nuevo; el alta manual no tiene `ProvisioningRequest`) |
| `billing.plan_payment_applied` | `aplicar_pago_de_periodo` cuando el resultado es `APLICADO` | `plan_paid:{payment_id}` |
| `billing.plan_payment_reversed` | `apply_payment`, en la rama de `checkout.py:508-514`, solo si `payment.status == 'aprobado'`, `payment.concepto == 'plan'` y el estado entrante es `rechazado` (esa rama también la recorren `DUPLICADO` y `monto_distinto`) | `plan_reversed:{payment_id}` |

- No se reutiliza `platform.period_paid`: solo cubre el canal panel y tiene otra semántica.
- Los consumidores corren con la identidad de servicio `referidos`. Si lanzan, el outbox reintenta y luego la bandeja
  de fallos; **el alta y el pago no se afectan**.
- **Reversa de un pago `asistente`:** la salida temprana de `checkout.py:506-507` impide que llegue a esa rama.
  HU-REF-09 decide de dónde sale; mientras tanto, un pago `asistente` revertido solo se cubre con la anulación manual.

### 4. Atribución

- **Tool `registrar_codigo_de_referido(codigo)`** en el pack del agente que toma las inscripciones de solvershub (hoy
  Suscripciones; el pack ya delega en `core.provisioning`). Delega en `core.referrals.atribucion`. Va en una
  **habilidad opcional**, para que no aparezca en el agente de Suscripciones de cada cliente, y solo funciona en un
  tenant vendedor (`es_vendedor`, `core/provisioning/vendedores.py`) y con binding fail-closed (HU-ARCH-34).
- **El código tiene que aparecer literalmente en un mensaje entrante de la conversación** (invariante 7). El código se
  normaliza (mayúsculas, sin espacios ni guiones) antes de comparar; la búsqueda mira los mensajes entrantes de la
  conversación del `ToolContext` (no un argumento de la tool) dentro de `vigencia_atribucion_dias`, como hace
  `antiabuso._mensaje_que_acepta`.
- Respuesta siempre neutra: `registrado | ya_registrado | invalido | no_disponible | limite`. Nunca revela quién es el
  referidor.
- Límites con `Bucket` de `core/security/rate_limit.py`: 5 intentos por hora por cliente y 10 atribuciones por día por
  código.
- No registra si el cliente ya tiene negocio o si el programa está apagado. Un segundo código reemplaza al anterior si
  no fue consumido (`vigente` → `descartada`).

### 5. Nacimiento y calificación

- **Nacimiento** (`on_negocio_completado`): tomar el `referidor_tenant_id` **guardado** en la atribución vigente del
  `customer_id` (o resolver el código del payload del alta manual) y comprobar en `tenants` que el referidor siga
  activo; no se vuelve a resolver el código de una atribución, porque si el owner lo regeneró el viejo ya está
  `retirado`. Después: `INSERT … ON CONFLICT (referido_tenant_id) DO NOTHING` con `atribuido` bajo el scope del
  referidor → atribución `consumida` bajo el scope del vendedor (otra transacción) → `evaluar_calificacion`.
- **Calificación** (`evaluar_calificacion`, idempotente; la llaman el nacimiento, el pago y el cron `recalificar`):
  1. **El pago que califica** es el primer pago del negocio referido que cumple todo: `concepto='plan'`,
     `status='aprobado'`, `revertido_en IS NULL`, `amount_cop > 0` y proveedor en `proveedores_que_califican`
     (`plataforma`, la renovación, no está). Free no tiene pago y nunca califica. Un pago que no cumple no descarta al
     referido: espera al siguiente dentro de `vigencia_pago_dias`.
  2. `SELECT … FOR UPDATE` de la fila y `UPDATE … WHERE estado='atribuido'`.
  3. Antiabuso (§8). Si falla: `rechazado` con motivo público genérico.
  4. Congelar plan, montos, espera, `config_version` y `libera_en = ahora + espera_dias`.
  5. `en_espera`, `notify` `referido_calificado` (§9) y auditoría.
- «Quien llega segundo completa»: si el pago llegó antes, el nacimiento califica en el acto; si el alta llegó antes, el
  evento de pago califica; si el evento de pago corrió antes del commit del nacimiento, lo recoge `recalificar`.

### 6. Liberación y abonos

El cron horario corre **en serie** bajo `pg_advisory_lock('referidos:programa')` (una sola corrida a la vez en toda la
plataforma) sobre `referral_trabajo_pendiente`. Por cada referido con la espera vencida:

1. **Bajo el scope del referido:** el pago congelado sigue `aprobado` con `revertido_en IS NULL` y el negocio activo.
   Revertido → `anulado` con `pago_revertido`; suspendido o eliminado → `anulado`. Una cancelación con el período ya
   pagado no anula.
2. **Tope del programa:** `referral_creditos_otorgados_mes` del mes calendario más este referido no pasa
   `tope_creditos_mes_programa`. La serie del cron hace que dos referidores no lo pasen juntos.
3. **Transacción A, bajo el scope del referidor**, candados en este orden: suscripción →
   `pg_advisory_xact_lock('billing:creditos:<referidor>')` → fila (`FOR UPDATE`). Es la convención de `creditos.py` y
   de ADR-0213 §7.
   - Sin ciclo (`acreditar_bonificacion` → `sin_ciclo`) o suscripción suspendida: sigue en espera, se reintenta a
     diario y a los `espera_sin_ciclo_max_dias` (90) queda `anulado` con `referidor_sin_ciclo`.
   - Tope del referidor superado: sigue en espera hasta el mes siguiente; no se rechaza.
   - Si no: buscar el `origen` `referral:<id>:referidor` en cualquier ciclo, abonar y pasar a `otorgado` con
     `regalo_referido_estado = 'pendiente'`. Un `UPDATE` exitoso con un abono `ya_acreditada` es una inconsistencia:
     rollback y alerta crítica.
4. **Transacción B, bajo el scope del referido:** suscripción → `pg_advisory_xact_lock('billing:creditos:<referido>')`
   → buscar el `origen` `referral:<id>:referido` en cualquier ciclo → si no está, `acreditar_bonificacion`. Aquí la
   búsqueda por `origen` dentro del candado **es la defensa principal** (la fila `referrals` vive en el referidor y la
   RLS la oculta bajo este scope): basta porque `credit_grants` es solo de inserción y la fila no desaparece. Encontrarlo
   o recibir `ya_acreditada` es el camino normal de recuperación tras una caída, no una alerta. Si el referido ya no
   está activo o no tiene ciclo, no se abona.
5. **Transacción C, bajo el scope del referidor:** `regalo_referido_estado` `pendiente → entregado` (u `omitido`).
   Si falla, el cron `regalo_referido` repite B y C.

**Regla de candados:** la calificación, la reversa y la anulación del master solo toman la fila; nunca toman después la
suscripción ni el candado del saldo. El cron nunca sostiene candados de dos negocios a la vez.

**El regalo depende del referidor:** si el referido se anula (por ejemplo, `referidor_sin_ciclo`), el referido no
recibe su regalo.

El abono usa `acreditar_bonificacion` sin cambiar su contrato; HU-REF-02 suma la búsqueda por `origen` en cualquier
ciclo. Se gasta y se arrastra como dicen ADR-0213 §2 y §3, y se ve en `bonuses` de `GET /admin/plan` (ADR-0213 §10).

### 7. Parámetros

En `tenants.settings["referidos"]` del vendedor, con el patrón de `core/provisioning/interruptor.py`. Con varios
vendedores (`PROVISIONING_SELLER_TENANTS`), manda el del vendedor que recibió la atribución; en el alta manual, el
primero de la lista. Cada cambio sube `version` y se audita. Al calificar se congelan plan, montos, espera y
`libera_en`; siguen vivos el interruptor, los topes y las vigencias.

| Parámetro | Por defecto |
|---|---|
| `activo` | `false` |
| `espera_dias` | 15 |
| `vigencia_atribucion_dias` | 90 |
| `vigencia_pago_dias` | 180 (`atribuido` sin pago → `anulado`) |
| `espera_sin_ciclo_max_dias` | 90 |
| `proveedores_que_califican` | `["wompi", "asistente"]` |
| `referidor_plan_minimo` | `free` |
| `tope_otorgados_mes_referidor` | 5 |
| `tope_creditos_mes_referidor` | 30.000 |
| `tope_creditos_mes_programa` | 300.000 |
| `recompensas` | basic 3.000 / 1.500 · premium 7.000 / 3.000 · empresarial 18.000 / 6.000 (referidor / referido) |
| techo duro (env) `REFERIDOS_MAX_RECOMPENSA` | 50.000 por abono; la configuración no lo puede superar |

`activo = true` exige que `ck_credit_grants_tipo` admita `bonificacion` (lo admite desde la 0161). Apagar el programa
congela todo sin perder estado: la tool responde `no_disponible`, nadie nace ni califica y el cron no libera.

### 8. Antiabuso (al calificar)

- **Mismo dueño:** la cuenta del alta del referido (`ProvisioningRequest.detalle["owner"]["admin_id"]`, o el
  `admin_email` del alta manual) contra los owners del referidor en `tenant_user`. No se usa el `tenant_user` del
  referido: al calificar en el nacimiento todavía no existe (contexto, punto 6).
- **Mismo correo:** el correo de la cuenta del alta contra los correos de los owners del referidor (no solo contra el
  pedido del referidor: un referidor dado de alta a mano no tiene pedido).
- **Misma persona:** huellas `tel:` y `doc:` (HMAC) de `PedidoDeAlta.huellas` del referido contra las del referidor,
  con `core/provisioning/antiabuso.py`. `olvidar_huellas_vencidas` las vacía a los 12 meses; la huella del correo del
  consumidor (`consumer.py:124`) es SHA-256 sin clave. Un alta manual no tiene `PedidoDeAlta`: ahí solo cuentan la
  cuenta y el correo.
- **Pagos sin dinero real:** solo los proveedores listados con monto mayor que cero.
- **Fuerza bruta y cosecha:** alfabeto de ≈ 2^40, código literal en el chat, límites de intentos y topes.

El motivo público es genérico y nunca dice qué señal saltó; los logs no llevan PII.

### 9. Autorización y notificaciones

| Acción | Principal | Enforcement |
|---|---|---|
| Ver y regenerar mi código, ver mis referidos | owner de un negocio activo | sección RBAC nueva `referidos` (grupo «Cuenta», solo owner) |
| Configurar, encender y apagar, bandeja, anular | master | `_require_master` |
| Tool `registrar_codigo_de_referido` | asistente del tenant vendedor | `es_vendedor` + binding fail-closed |
| Handlers y cron | identidad de servicio `referidos` | sin ruta de API |

El owner ve nombre, estado, plan, su recompensa y la fecha estimada; nunca PII del referido ni el motivo interno.
`Idempotency-Key` obligatoria solo en «regenerar código»; las acciones del master son idempotentes por estado.

**Notificaciones (ADR-0167):** entidad `referido` en `ENTIDADES` (`core/notifications/spec.py`), categoría `cobros`, y
plantillas `referido_calificado` (push sin el nombre del negocio), `referido_otorgado`, `regalo_de_bienvenida` y
`referido_anulado`. En `data` va el id del referido, nunca el `tenant_id` del otro negocio.

### 10. Reversa

- **La reversa se guarda en el pago:** `subscription_payments.revertido_en timestamptz null` (migración de HU-REF-09).
  La rama de `checkout.py:508-514` lo fija con las condiciones de §3 y emite `billing.plan_payment_reversed`. El pago
  no cambia de `status` (no se toca la conciliación de hoy); la calificación (§5) y la liberación (§6.1) leen
  `revertido_en`. Así una reversa que llega antes del nacimiento no se pierde: el nacimiento ve el pago revertido y no
  califica con él.
- Sobre un referido `atribuido` o `en_espera` → `anulado` con `pago_revertido`. Sobre uno `otorgado` → alerta crítica y
  revisión a mano: **no hay abonos negativos** (ADR-0209, ADR-0213).
- El master también anula con `POST /admin/platform/referidos/{id}/anular` desde `atribuido` o `en_espera`.

### 11. Observabilidad

- Logs `referral_*` con ids, estado anterior y nuevo, motivo y `trace_id`; `record_audit` en cada transición y en cada
  cambio del programa.
- Métricas (ADR-0188): `referrals_transitions_total{a_estado,motivo}`, `referrals_credits_granted_total{rol,plan}`,
  `referrals_antiabuso_rejections_total{regla}` y salud del cron (duración, fallos, antigüedad de la fila vencida más
  vieja).
- Alertas: fila vencida más de 24 h sin liberar, **sin contar** las que esperan por `tope` o `sin_ciclo` (esas tienen
  su propio conteo); regalo pendiente más de 48 h; reversa sobre un `otorgado` (crítica); estado del referido que no
  coincide con el abono (crítica).

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Recompensa al registrarse el referido | Premia altas que nunca pagan y es fácil de abusar |
| Reutilizar `platform.period_paid` como disparador | Solo lo emite el canal panel; pagos por el asistente, alta automática y Free → pago no lo emiten |
| `referrals` y `referral_attributions` en el tenant vendedor, como `provisioning_requests` y `pedidos_de_alta` (ADR-0121) | Recortaría tres funciones definer y el índice global, y dejaría cron, bandeja y tope bajo un solo scope. Pero los datos del referidor («mis referidos») vivirían fuera de su tenant y harían falta funciones definer para que el owner los lea. Se elige el referidor como dueño; se revisa si aparece una sexta función |
| Tabla de plataforma sin RLS | Son datos de dos negocios; deben quedar bajo la invariante 1 y en `purge_tenant` |
| FK de `referido_tenant_id` a `tenants` | La fila vive en el referidor y apunta a otro negocio; el borrado de ese negocio no debe tocar el historial del referidor |
| Un solo abono a los dos negocios en la misma transacción | Exigiría dos `tenant_scope` y candados de dos negocios a la vez |
| Detectar la reversa solo por el evento, sin guardarla | El pago sigue `aprobado`: la liberación no la vería y una reversa anterior al nacimiento se perdería |
| Abono negativo al revertir tras otorgar | `credit_grants` es solo de inserción y no tiene abonos negativos; el caso es raro y se resuelve a mano |
| Confiar en la unicidad de `credit_grants` para no duplicar | Incluye `ciclo`: un reintento en otro ciclo duplicaría |
| Código derivado del slug del negocio | Se adivina y revela quién refiere |

## Consecuencias

**Positivas:**
- La recompensa reutiliza el saldo reproducible de ADR-0209 y el abono de ADR-0213 sin duplicarlos.
- Un solo evento en la puerta única cubre todos los canales de pago de plan.
- Las reglas de dinero (una vez por negocio, congelados, sin borrado) están en el esquema.
- La reversa del pago de plan deja de ignorarse y queda guardada.

**Negativas / lo que aceptamos pagar:**
- Tres tablas, tres eventos, cinco funciones definer, una columna en `subscription_payments`, un cron en serie y
  superficies en landing, asistente, panel y APK.
- Dos excepciones declaradas: índices globales y tablas sin `deleted_at`.
- La reversa tras otorgar es un riesgo residual que se resuelve a mano.
- La reversa de un pago `asistente` no tiene camino automático hasta que HU-REF-09 lo decida.
- Los créditos regalados son un pasivo que no vence mientras la cuenta exista (ADR-0213).
- El antiabuso no frena a una red de negocios con cuentas, teléfonos, documentos y correos distintos; lo acotan los
  topes y la revisión de la bandeja.

**Qué haría que revisáramos esto:**
- Que haga falta una sexta función definer de referidos (se revisa la alternativa del tenant vendedor).
- Que los créditos de referidos superen el 10 % de los consumidos en un mes.
- Que las reversas tras otorgar dejen de ser raras.
- Que el dueño quiera recompensas en dinero o recurrentes.

## Impacto en el código

- **Migraciones** con `db-migration` y `auditor-multitenant`: las tablas y funciones (HU-REF-03) y
  `subscription_payments.revertido_en` (HU-REF-09).
- **`core/referrals/`** (nuevo): `codigos`, `atribucion`, `nacimiento`, `calificacion`, `liberacion`, `reversa`,
  `programa` y `antiabuso` (que usa `core/provisioning/antiabuso.py`).
- **`core/billing/`:** búsqueda por `origen` en cualquier ciclo antes de `acreditar_bonificacion` (HU-REF-02);
  `billing.plan_payment_applied` en `service.py` (HU-REF-05) y `billing.plan_payment_reversed` con `revertido_en` en
  `checkout.py` (HU-REF-09).
- **`core/provisioning/consumer.py`** y el alta manual: `provisioning.negocio_completado` y `codigo_referido`.
- **`core/tenants.py`:** anonimizar y descartar en la purga (§1).
- **API:** `core/api/routers/referidos.py` (owner) y `core/api/routers/platform_referidos.py` (master); sección RBAC
  `referidos`; `docs/openapi.json`.
- **Tool** en una habilidad opcional del pack de Suscripciones; **playbook** de ventas de solvershub en
  `solvershub-comercial`.
- **Notificaciones:** entidad `referido` y plantillas en `core/notifications/`.
- **Tareas:** `liberar`, `regalo_referido` y `recalificar` en el beat.
- **No cambian:** `credit_grants`, `usage_ledger`, `acreditar_bonificacion` (solo se envuelve), el cron de cierre ni
  `AgentSpecV3`.
