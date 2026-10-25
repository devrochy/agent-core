## API para las claves sin pantalla: palabras de baja y política de WhatsApp Web (HU-CFG-05, SPEC-37) (2026-10-25)

- Nuevo `GET/PUT /api/v1/admin/settings/bajas` (sección Configuración): el dueño edita las palabras con las que un cliente se da de
  baja de los envíos a segmentos (ADR-0150). Se guardan sin vacíos ni repetidos (sin importar mayúsculas, tildes ni signos), hasta
  50 de 60 caracteres. Lista vacía = restablecer las de por defecto. Cada cambio queda en `audit_log`.
- Nuevo `GET/PUT /api/v1/admin/settings/whatsapp-web` (solo master; 403 al resto): modo, pausa mínima y máxima, tope diario y
  calentamiento, con los rangos de `core/config_limites.py`. Una pausa máxima menor que la mínima responde 422 con el campo (antes
  el runtime las igualaba en silencio). Pasar a `allow_outbound` exige `acepta_riesgo: true` (aviso `RISK_ACK_VERSION`) y deja una
  fila `settings.waweb_allow_outbound` en `audit_log` con quién, antes, después y la versión del aviso.
- Sin migraciones; sin cambios de comportamiento para quien no toque estas opciones.
