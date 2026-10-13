# ADR-0135 — Datos de prueba propios de cada tester

- **Estado:** aceptado (dueño, 2026-10-13)
- **Fecha:** 2026-10-13
- **Contexto:** SPEC-30 de solvershub-docs (D-SPEC-30-6; HU-QA-07 y HU-QA-08). **Usa:** ADR-0083 (`seed_data`),
  ADR-0110 (fragmentos de preset por pack), ADR-0133 (tablas del centro de pruebas), ADR-0134 (rol tester).

## Contexto

- El dueño pidió que el centro de pruebas «facilite los datos necesarios para probar, o por lo menos cómo
  gestionarlos o crearlos». Unas 45 pruebas del catálogo piden la cuenta del dueño de Cobranza Demo, y esas
  contraseñas no se reparten a testers externos.
- El dueño eligió las dos opciones: un botón que genera datos propios por tester (solo en test) y una guía para
  los datos externos (Wompi, Meta, correo). Las cuentas demo compartidas quedan para el equipo interno.

## Decisión

1. **«Preparar mis datos»** (`POST /api/v1/admin/qa/datos`, sección `qa`, solo con `QA_TESTERS_ENABLED`) crea
   un negocio de prueba por tester (`qa-<8 hex del id>`) con `onboard_tenant`: Comercio, Agenda y Cobranza, plan
   premium, y tres cuentas en el dominio reservado `pruebas.solvershub.test`:
   - **dueño** (owner): panel y APK como propietario;
   - **gestor** (colaborador con perfil de gestor): app de campo, con la cartera asignada y todos sus permisos;
   - **colaborador**: Resumen, Conversaciones, Leads y Escalaciones.
2. **Los datos de ejemplo salen de fragmentos `packages/pack-*/presets/qa.json`** y entran por el `seed_data`
   de cada pack (ADR-0083, ADR-0110): el core no importa packs. Se suman `seed_data` a Agenda (tipos, citas y
   bloqueos) y a Cobranza (cartera, caja, gestor y clientes al día, en mora y sin préstamo). Ambos son
   idempotentes. Los clientes no llevan teléfono, para que ningún recordatorio llegue a un número real.
3. **Las contraseñas se generan al azar y se devuelven una sola vez** (al preparar o al pedir contraseñas nuevas,
   `POST /datos/contrasenas`, que además cierra las sesiones del panel de esas cuentas). No se guardan.
4. **`qa_tester_datasets`**, tabla de sistema sin RLS como el resto de `qa_*`: una fila por tester reclama la
   corrida con un INSERT condicional. Si una corrida falla a medias, la siguiente reutiliza el negocio y las
   cuentas que esa misma fila creó, y nunca toma una cuenta ajena con el mismo correo. `tenant_id` es FK lógica:
   si el master purga el negocio, el tester lo vuelve a preparar.
5. **Catálogo v6 (HU-QA-08):** cada prueba lleva `entorno` (panel, apk, whatsapp, web o api), `area` (la sección
   del panel o la pantalla de la APK) y `requiere` (slugs de la sección nueva `datos`, la guía de datos externos).

## Consecuencias

- Cada tester prueba con su propio negocio sin pisar a los demás. En test quedan negocios `qa-*`, que el master
  purga desde Negocios cuando un tester se va.
- Los testers se ven los negocios de prueba entre sí: el rol tester ve todos los negocios de test (ADR-0134).
- Un cambio de esquema en Comercio, Agenda o Cobranza que rompa el seed lo detecta
  `tests/integration/test_qa_datos.py`.
- La app de campo emite tokens propios de 12 horas: pedir contraseñas nuevas no los corta, pero `_auth`
  revalida la cuenta en cada pedido (ADR-0134).
