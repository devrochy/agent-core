# ADR-0134 — Rol tester: master en la operación, dueño en Colaboradores, solo en test

- **Estado:** aceptado (dueño, 2026-10-13)
- **Fecha:** 2026-10-13
- **Contexto:** SPEC-30 de solvershub-docs (D-33, D-SPEC-30-2; HU-QA-04). **Enmienda:** ADR-0047 (roles de
  plataforma). **Usa:** ADR-0121/0124 (enlace de un solo uso), ADR-0133 (centro de pruebas).

## Contexto

- El dueño quiere que testers internos y externos «puedan ejecutar las acciones de un master, pero el único
  acceso al que no pueden tener es el de colaboradores». También pidió que el rol exista **solo en test**: en
  producción un tester vería datos reales de clientes.
- El mismo día el dueño pidió habilitar también Colaboradores «para que pueda hacer las pruebas de esta
  sección». Se habilitó como **dueño del negocio activo**, no como master (decisión 3).
- Hasta ahora el master se decidía comparando `role == "master"` en cada sitio, más la función SQL
  `admin_tenants()`.

## Decisión

1. **`admins.role = 'tester'`.** `core/admin/roles.py` concentra la regla:
   - `actua_como_master(admin)`: master, o tester con `QA_TESTERS_ENABLED=true`;
   - `cuenta_habilitada(admin)`: activa y, si es tester, con el rol habilitado;
   - `acceso_de_plataforma(admin, sección)`: master y tester habilitado tienen `write` en todas las
     secciones, incluida `collaborators`. `SECCIONES_VEDADAS_AL_TESTER` queda vacía, lista para volver a vedar
     una sección si hiciera falta.
2. **Qué cuenta como operar** y pasa a `actua_como_master`:
   - `effective_access` y `permissions_for` (y con ellas `require`, el middleware SSR, la búsqueda, el sync y
     los primeros pasos);
   - las rutas de plataforma (negocios, alta automática, App de Meta, requisitos de Embedded Signup);
   - las operaciones de Meta reservadas al master;
   - la escritura de agentes y releases con el runtime legacy;
   - cerrar un ciclo de pruebas.

   La app de campo ya trataba como `master` el rol `global` de `admin_tenants()`.
3. **Colaboradores: el tester actúa como dueño del negocio activo.** Entra a la sección y los servicios de
   cuentas lo llevan por el camino del owner, porque siguen preguntando `role == "master"`. Así:
   - crea colaboradores y owners en el negocio activo;
   - invita, da y quita permisos de negocio (nunca `tenants` ni `qa`);
   - cambia contraseñas, desactiva y revoca invitaciones, solo de cuentas exclusivas de ese negocio.

   El panel clásico y la revocación de invitaciones lo tratan igual. **Riesgo aceptado:** las cuentas que el
   tester cree en test siguen vivas después de revocarlo. Solo existen en la base de test, separada de
   producción.

   **Qué cuenta como gestionar cuentas de plataforma** y sigue siendo `role == "master"`:
   - crear masters;
   - tocar cuentas de master, tester o con permisos globales;
   - vincular cuentas entre negocios;
   - invitar y revocar testers;
   - importar el catálogo de pruebas;
   - leer las ejecuciones de otros testers.

   Un owner trata a un tester como a un master: no lo gestiona, no lo vincula y no lo invita.

   Límites del tester dentro de lo que opera (revisión de seguridad del 2026-10-13):
   - **Alta de negocios:** solo negocios **nuevos** con una cuenta **nueva**. Reusar un slug existente (el alta
     es idempotente y vincularía al owner que se mande) o un correo con cuenta da 409. El tester no queda como
     owner del negocio que crea.
   - **Purga:** solo master, también en test (invariante 8).
   - **Gestores de cobranza:** crea gestores con cuenta nueva, que es parte de probar el campo; no invita
     cuentas existentes (409).
   - **Checklist de paso a producción:** lo marca solo el master; el tester marca la configuración de test.
   - **Recuperar contraseña:** a un tester sin contraseña no se le manda código. Solo entra por su enlace, que
     vence a los 7 días.
4. **`admin_tenants()` (migración 0088)** devuelve todos los negocios activos a `master` y `tester`.
5. **Solo en test.** Sin la variable, ni el login del panel ni el de la app de campo admiten a un tester. La
   validación de sesión de cada request y el refresh de la app tampoco, así que apagarla corta las sesiones
   abiertas. La app de campo revalida la cuenta en cada pedido y no acepta el refresh token como acceso: revocar
   o apagar la variable la corta en el siguiente pedido, no cuando vence el token. Tampoco se puede crear un tester por la API ni por el CLI. Producción no carga la variable; está
   en el checklist de paso a producción (`prod-rol-tester-apagado-en-produccion`).
6. **Alta solo por invitación del master.** `POST /api/v1/admin/qa/testers` crea la cuenta sin contraseña y
   manda por el correo de sistema un enlace de un solo uso: `<id>.<HMAC>` en el `#`, con vigencia de 7 días y
   tabla de sistema `qa_tester_invitations`. La API también devuelve el enlace, por si el correo no está
   configurado. El canje (`POST /api/v1/auth/accept-tester-invitation`) define la contraseña con un UPDATE
   condicional, acepta con otro UPDATE sobre el nonce y abre la sesión; tiene el bloqueo progresivo del login.
   Revocar desactiva la cuenta y borra sus sesiones. Reinvitar emite un enlace nuevo y obliga a definir otra
   contraseña.

## Consecuencias

- **Pendientes que no bloquean:**
  - el login SSR clásico y el login unificado de la app de campo no tienen bloqueo progresivo (es anterior a
    este ADR);
  - confirmar que test usa una App de Meta propia: el tester hereda las operaciones de Meta del master;
  - un chequeo al arrancar que impida `QA_TESTERS_ENABLED` con las URL de producción.

- Todo chequeo nuevo de master tiene que elegir entre `actua_como_master` (operar) y `role == "master"`
  (gestionar cuentas de plataforma).
- Un tester en test ve todos los negocios de test, puede crearlos y modificarlos, y gestiona sus
  colaboradores como dueño (no los purga). Es lo pedido: test no tiene datos reales.
- Las cuentas tester no aparecen como colaboradores de ningún negocio. Se gestionan en Plataforma › Pruebas ›
  Testers.
