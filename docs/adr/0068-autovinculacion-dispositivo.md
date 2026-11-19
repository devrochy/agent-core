# ADR-0068 — Autovinculación de dispositivo del gestor (HU-39/43)

- **Estado:** aceptado
- **Fecha:** 2026-09-25
- **Modificado por:** ADR-0174 (la vinculación pasa a `admin_id`↔equipo en el core, con política por negocio y rol; la tabla `access_events` reemplaza «sin infraestructura de alertas nueva»).

## Contexto

El login del gestor es **usuario/contraseña** (ADR-0066): cualquiera entra desde
cualquier equipo con esas credenciales. HU-39/43 exigen **vincular el dispositivo**
(IMEI + WhatsApp) con **aprobación del administrador**, para que un equipo robado o
ajeno no sirva solo con las credenciales. Es el bloqueante de producción declarado.

El pack ya tenía `devices` (ADR-0057) con `codigo`/`api_key_hash`/`estado` para la
app v1; ahora la identidad del gestor es el `Admin` del core, y la app usa el
contrato `/gestor/*` (ADR-0065).

## Decisión

1. **Vinculación con aprobación**: el gestor, tras su primer login, vincula el equipo
   con `POST /gestor/dispositivo` `{imei, whatsapp, publicKey?}` → crea un `Device`
   en estado **`pendiente`** (idempotente por IMEI) + `IntentoAcceso("solicitud")`.
   El admin **aprueba** en la pantalla **Dispositivos** (`POST /cobranza/dispositivos/{id}/estado`).
2. **Política de enforcement** (el login recibe `imei` + `whatsapp`):
   - **Sin devices** → no bloquea; devuelve `requiereVinculacion: true`.
   - **Device(s) activo(s) con IMEI** → es **bloqueante**: el `imei` debe coincidir
     con uno activo; si no → **403** + `IntentoAcceso("denegado")`.
   - **Solo `pendiente`** (primer vínculo sin aprobar) → **no bloqueante** (el gestor
     nuevo no queda fuera mientras el admin aprueba); devuelve `estado: pendiente`.
   - **Device manual/legado sin IMEI** (creado por el admin) → no se puede exigir
     equipo; no bloquea (compatibilidad con el flujo del panel).
3. **"IMEI"** = `Application.getAndroidId()` de `expo-application` (Android ID, no el
   IMEI real — mejor privacidad; el campo se llama `imei` por continuidad).
4. **Varios equipos por gestor** (reemplazo de teléfono): se permite; cada login debe
   coincidir con **uno activo**.
5. **X25519 / HU-40 diferido**: `public_key` se guarda (para el cifrado del snapshot
   del día, otra tanda); el login no lo usa.
6. **Alerta al admin**: la pantalla **Dispositivos** muestra los `pendiente` (badge),
   sin infraestructura de alertas nueva.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Bloqueante siempre (incluso el primer vínculo) | El gestor nuevo no podría entrar a vincular → huevo-gallina |
| No bloqueante siempre | No protege nada (el equipo robado sigue sirviendo) |
| IMEI real del dispositivo | Requiere permisos de sistema y es dato sensible; el Android ID basta |
| Cifrado X25519 en esta tanda | Alcance mayor (HU-40); el binding es el bloqueante |

## Consecuencias

**Positivas:** solo un equipo aprobado opera tras el primer vínculo; el intento queda
en `IntentoAcceso` (auditoría); el admin controla desde el panel; el gestor nuevo no
queda fuera.

**Negativas / lo que aceptamos pagar:** el enforcement se apoya en el Android ID
(no infalible si se manipula el equipo); mientras un device está `pendiente` el
gestor opera sin equipo verificado (ventana hasta la aprobación); el admin debe
aprobar manualmente.

**Qué haría que revisáramos esto:** que se exija atestación de hardware (Play
Integrity), o que un gestor necesite operar sin equipo (→ excepción por gestor).

## Impacto en el código

- `packages/pack-cobranza/pack_cobranza/api_gestor.py` (`gestor_login` + `/gestor/dispositivo`),
  `admin.py` + `dispositivos.html` (aprobar/bloquear), migración `0012` (IMEI único).
- `solvershub-apk`: `expo-application`, `imei`/`whatsapp` en el login y pantalla de
  vinculación.
