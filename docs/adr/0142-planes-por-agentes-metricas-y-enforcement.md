# ADR-0142 — Planes por agentes: métricas `agentes` y `colaboradores`, crédito mensual y punto de enforcement

- **Estado:** propuesto (HU-BILL-05, 2026-10-19). Cifras aprobadas por el dueño el 2026-10-19; las demás
  decisiones revisables están marcadas abajo.
- **Fecha:** 2026-10-19
- **Contexto:** D-35 («SolversHub vende agentes»), `modelo-de-precios.md §5` (docs), HU-BILL-05 y HU-AOS-37
  (achicada). **Usa:** ADR-0045 (planes, cuotas y enforcement), ADR-0112 §1 (instancias por conexión),
  ADR-0081 (invitaciones), ADR-0136 (propietarios pares) y ADR-0139 (presets v2).

## Contexto

D-35 cambia lo que se cobra: los planes ya no cuentan habilidades activas ni asistentes por negocio, sino
**agentes publicados**, **crédito mensual de uso** y **colaboradores**. Las cifras por plan salen de
`modelo-de-precios.md §5`:

| Dimensión | Free | Basic | Premium | Empresarial |
|---|---|---|---|---|
| Agentes publicados (`agentes`) | 1 | 2 | 5 | ilimitado |
| Crédito mensual (`monthly_credits`, 1 crédito = COP 10) | 2.500 | 20.000 | 75.000 | 240.000 |
| Colaboradores (`colaboradores`) | 2 | 5 | 15 | 50 |

El core ya mide cuotas derivándolas de sus tablas fuente (ADR-0045) y bloquea la unidad nueva con
`QuotaExceeded`. Lo que faltaba era medir las dos dimensiones nuevas, decidir dónde se bloquean y mostrar el
crédito, que todavía no tiene ledger (HU-BILL-04/06).

## Decisión

1. **Métrica `agentes`** (`core/billing/metrics.py`, alcance `lifetime`). Cuenta los agentes del negocio con
   `kind = 'specialist'`, `status = 'active'` y sin borrar, que tengan **al menos una instancia viva en una
   conexión viva**. Se cuentan agentes, no filas de `agent_instances`: un agente en WhatsApp y en el chat web
   ocupa un solo cupo.
   - El General es un especialista, así que cuenta.
   - El Coordinador (`kind = 'core'`) no cuenta.
   - El back-office queda fuera: es su propia dimensión en HU-AOS-37.
   - Que el agente tenga versión publicada lo garantiza `agregar_instancia`, que solo instancia agentes
     publicados.
2. **Métrica `colaboradores`** (alcance `lifetime`). Suma dos cosas:
   - Los vínculos `tenant_user` del negocio con rol `colaborador`, sin las cuentas de plataforma (master y
     tester).
   - Las invitaciones a colaborador pendientes y vigentes. Si no contaran, se podrían mandar N invitaciones
     estando a uno del tope.
   - Solo cuentan las cuentas **activas**: desactivar una cuenta libera su cupo (decisión del dueño,
     2026-10-19). Para que desactivar y reactivar no sea un atajo, **reactivar vuelve a medir el tope** en cada
     negocio donde la cuenta es colaboradora: por encima, 409 `plan_limit_colaboradores` y la cuenta sigue
     desactivada (lo conservador; también para el master, cuya salida es subir el plan). La medida y el cambio
     de estado van en transacciones separadas: dos reactivaciones simultáneas al borde pueden pasar las dos;
     solo deja al negocio por encima del tope, no lo rompe.
   - *Revisable:* §5 dice «excluye al propietario», en singular. Aquí no cuenta **ningún** propietario, porque
     desde ADR-0136 los propietarios son pares entre sí y solo el master vincula a uno extra.
3. **Enforcement: solo se bloquea crecer.** `enforcement.enforce_growth(tenant, métrica, session=...)` toma
   `pg_advisory_xact_lock(hashtext('billing:<métrica>'), hashtext('<tenant>'))`, mide en la misma sesión y levanta
   `QuotaExceeded` si `used >= limit`. Así, dos altas concurrentes no pasan las dos con un solo cupo.
   - Un negocio que ya está por encima del tope **no se rompe**: sigue atendiendo y conserva sus agentes,
     instancias, colaboradores y versiones. Solo se rechaza la unidad siguiente. Esto le pasa, por ejemplo, a
     un plan bajado o a un negocio que tenía más cuentas antes de este catálogo.
4. **Dónde se bloquea un agente.** El único punto es `core.agents.instancias.agregar_instancia`, y solo cuando
   es la **primera instancia viva** del agente, que es el momento en que pasa a ser unidad facturable. Si el
   agente ya está en otra conexión, la cuenta no crece y no se mide. Ese punto cubre todas las entradas:
   - `POST /admin/connections/{id}/agents` responde 409 `plan_limit_agentes`.
   - `POST /admin/agents/{id}/publish` (editor, HU-AOS-24) hace un pre-chequeo **antes** de publicar la
     versión y responde 409 `plan_limit_agentes`, así no queda una versión nueva sin conexión. Si otra
     publicación gana la carrera, esa conexión vuelve con `error = plan_limit_agentes`.
   - Presets v2: la instancia queda con el código `plan_limit_agentes`. Un agente nuevo que no entró en
     ninguna conexión se archiva, como en cualquier otro conflicto (ADR-0139).
   - **`POST /admin/agents/{id}/versions` no se bloquea.** La DoD de HU-BILL-05 lo nombra, pero publicar una
     versión sin instancia no crea una unidad facturable: D-35 y §5 cuentan «agentes activos con instancia».
     Sin enforcement ahí, el dueño puede preparar un agente aunque esté al tope. *Pendiente:* enmendar la DoD
     de la HU en el repo de docs.
   - **No se bloquean `asegurar_instancias_base` ni `clonar_instancias`.** La primera pone el Coordinador y el
     General al dar de alta una conexión, y el General es el agente incluido en todos los planes. La segunda
     es la promoción y copia los mismos agentes. *Revisable:* un Free que quitó al General de todas sus
     conexiones y puso otro especialista vuelve a tener 2 cuando crea una conexión nueva. Es un caso borde y
     queda anotado. Lo mismo pasa al **revivir una conexión borrada**: borrar la conexión deja sus instancias
     vivas, el agente deja de contar y, al volver a dar de alta la conexión, vuelve a contar sin medir. Solo
     puede dejar al negocio por encima del tope, nunca romperlo, y queda como pendiente.
   - El lock se toma **antes** de mirar si el agente ya cuenta, así dos altas del mismo agente nuevo en dos
     conexiones no se miden como dos agentes.
5. **Dónde se bloquea un colaborador.** Hay tres puntos:
   - En `create_collaborator`, tanto en el alta de una cuenta nueva como en la invitación de una existente.
     Lo usan `POST /admin/collaborators` y el formulario del panel clásico.
   - En `crear_invitacion` con rol colaborador, salvo cuando se refresca una pendiente que ya era de
     colaborador. Lo usan `POST /admin/invitations` y el alta de gestores de `pack_cobranza`.
   - En el alta de gestores de `pack_cobranza` con cuenta nueva, desde la API y desde el panel clásico.
   - No se bloquean dos cosas: aceptar una invitación (su cupo ya se contó al invitar) y el `link_tenant` del
     master, que es una acción de plataforma (también puede pasar un propietario a colaborador sin medir).
   - `POST /admin/invitations` con `role = owner` queda solo para el master y el tester (403 para un
     propietario), igual que `create_collaborator` (ADR-0048 §3, ADR-0136). Sin eso, invitar «propietarios»
     saltaba el tope, porque los propietarios no cuentan.
6. **Contrato del error.** Se mantiene el de `plan_limit_plugins`: HTTP 409, `error.code =
   plan_limit_<métrica>` y `error.details = {metric, used, limit}`, con un mensaje que explica el tope y
   ofrece mejorar el plan. Lo construye una sola función, `core/api/plan_limits.py`, que comparten todas las
   entradas.
7. **Catálogo** (`core/billing/plans.py`).
   - Se agregan `agentes` y `colaboradores` a `limits`.
   - `plugins` sale de `limits`, con lo que «Habilidades activas» deja de ser tope. La métrica sigue definida
     para las lecturas, y como `quota_decision` no encuentra la clave, deja pasar. Los datos no se tocan.
   - «Asistentes por negocio» no existía como métrica.
   - Las métricas de volumen (`conversaciones`, `mensajes`, audio, OCR y `costo_llm_usd`) **se quedan** hasta
     que el crédito tenga ledger y enforcement (HU-BILL-04/06). Quitarlas antes dejaría el costo LLM sin tope.
8. **Crédito mensual.** Es un campo tipado, `Plan.monthly_credits`, que `sync_plans` espeja a
   `plans.features.credito_mensual`. No hace falta migración: la tabla ya tiene `features` jsonb.
   - `GET /admin/plan` lo expone como `credit: {limit, used: null, percent: null}`, y `plans[].monthly_credits`
     trae el de cada plan.
   - `used` queda en `null` hasta que exista el ledger de créditos. El core no tiene una tasa USD→COP, y
     convertir `costo_llm_usd` con una tasa inventada daría una barra falsa.
9. **Sin migración.** Los IDs de los planes no cambian, las suscripciones no se tocan y `sync_plans` reescribe
   `limits` y `features` al arrancar.

## Consecuencias

- **La transición es inmediata.** El criterio «Transición» de HU-BILL-05 pide que un negocio en un plan v1 con
  período vigente conserve sus condiciones hasta la renovación. Este ADR no lo cumple. El catálogo no está
  versionado por suscripción, y cumplirlo exige guardar una copia del plan en cada suscripción, que es trabajo
  de otra HU. Para que el cambio sea inmediato sin romper nada, solo se bloquea crecer.
  *Pendiente del dueño:* aceptar esta transición o pedir la copia del plan por suscripción.
- **Cifras aprobadas por el dueño el 2026-10-19:** agentes 1/2/5/ilimitado, colaboradores 2/5/15/50 y crédito
  2.500/20.000/75.000/240.000. *Pendiente en docs:* quitar la marca de «propuesta» de §5.
- **Free queda con «1 agente cualquiera»** (aprobado por el dueño el 2026-10-19): el tope de Free es 1 agente,
  sea o no el General.
- **Las habilidades dejan de tener tope.** Ya no hay 409 `plan_limit_plugins`, y
  `core/presets/apply.py`, que validaba `limits["plugins"]`, deja de rechazar presets con muchos packs. Queda
  como deuda el código de cuota de habilidades, que ya no se dispara: `can_activate_plugin`, los caminos de
  cuota de `core/admin/views.py` y el 409 de `plugins.py`. Se conserva para los planes que vuelvan a definir
  `plugins`.
- **Fuera de esta entrega** (siguen en HU-BILL-05 o en las HU que se indican):
  - conexiones adicionales por agente y conexión obligatoria al publicar (el editor ya la exige, HU-AOS-24);
  - canales aplicados;
  - plan anual (HU-BILL-12);
  - fuente única de los productos del tenant `solvershub` (HU-SH-01);
  - medición y enforcement del crédito (HU-BILL-04/06);
  - operaciones entre agentes y back-office (HU-AOS-37).
