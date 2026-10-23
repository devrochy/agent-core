# ADR-0154 — Geocodificación de la dirección del cliente con LocationIQ

- **Estado:** propuesto (falta que lo acepte el dueño).
- **Fecha:** 2026-10-22
- **Contexto:** SPEC-33 «CRM», tanda 1B. **Usa:** ADR-0114 (la dirección y las coordenadas son hechos
  `perfil`), ADR-0153 (perfil canónico) y ADR-0053 (PII, retención y purga).

## Contexto

El perfil del cliente guarda la dirección como texto y la ficha ya arma un enlace de búsqueda en
OpenStreetMap. El dueño pidió georreferenciar la dirección con un servicio real para ubicar
entregas y visitas. La plataforma paga el servicio, así que hay que controlar el costo y no
exponer direcciones en logs.

## Decisión

1. **Proveedor por defecto: LocationIQ** (`GEOCODING_PROVIDER=locationiq`), detrás de una interfaz
   del core. Motivos: free tier de 5.000 llamadas/día y 2 req/s, permite **almacenar la respuesta**
   (los resultados de Mapbox permanent cuestan ~$5/1.000 y sus términos restringen mostrar
   coordenadas; Google también restringe el almacenamiento). La cobertura es OpenStreetMap: buena
   en ciudades, irregular en direcciones rurales o informales. Cambiar a otro proveedor es
   configuración, no un cambio del panel.
2. **La API key es de la plataforma.** Los negocios no la ven ni la configuran. `GEOCODING_PROVIDER=none`
   apaga la función. Sin key o sin proveedor, el botón explica que no está configurada y no se llama
   a ningún tercero.
3. **Solo se envía la dirección compuesta** (dirección, municipio, departamento y país). Nunca
   nombre, teléfono, correo ni documento. Los logs llevan proveedor, estado, latencia y precisión,
   nunca la dirección.
4. **Dos caminos:** automático al guardar una dirección nueva o cambiada (best-effort; si el
   proveedor falla, el perfil ya quedó guardado) y un botón «Geocodificar dirección» / «Recalcular
   ubicación» para reintentar. El resultado se guarda como hechos `perfil`: `latitud`, `longitud`,
   `direccion_normalizada`, `geocodificado_en`, `geocodificacion_precision` y
   `geocodificacion_proveedor`.
5. **Topes dobles en Redis:** por negocio (200/día por defecto) y global (4.000/día por defecto,
   por debajo del free tier). Si Redis no responde se falla cerrado (503): no se geocodifica sin
   poder contar. Un 429 del proveedor se traduce a 429 con un mensaje claro.
6. **Mapa:** enlace a OpenStreetMap con las coordenadas y atribución «Geocodificación: LocationIQ».
   Sin mapa embebido ni SDK en la primera versión.
7. **Privacidad:** LocationIQ es subencargado de tratamiento; la purga del cliente borra sus hechos,
   incluidas las coordenadas. La dirección normalizada se muestra como sugerencia, no pisa la
   dirección que escribió la persona.

## Riesgos asumidos

- **Cobertura desigual.** Una dirección rural o informal puede no encontrarse; se devuelve 422 y se
  puede corregir la dirección o reintentar. No se inventan coordenadas.
- **Costo por llamada.** Los topes y la caché implícita del cliente (no se vuelve a geocodificar si
  la dirección no cambió) lo acotan, pero un negocio que edita direcciones en masa puede consumir su
  cupo. El tope global protege a la plataforma.
- **Términos del proveedor.** Antes de producción hay que validar el contrato y la atribución del
  plan gratuito. Si se cambia a Mapbox/Google, revisar sus restricciones de almacenamiento.

## Alternativas consideradas

| Opción | Por qué no |
|---|---|
| Mapbox | Mejor cobertura, pero *permanent geocoding* sin free tier y términos que restringen mostrar lat/lng |
| Google Geocoding | Mejor cobertura, pero restricciones de almacenamiento y costo a escala |
| Nominatim propio | Sin costo por llamada y permite almacenar, pero operar el servicio y su cobertura es la misma OSM |
| Solo enlace de búsqueda en el mapa | Cero costo, pero no guarda coordenadas ni sirve para operación de campo |

## Consecuencias

**Positivas:** la ficha gana una ubicación real y consistente, con control de costo y sin exponer
PII a logs. El proveedor se cambia sin tocar el panel.

**Negativas:** una dependencia externa y su DPA; la calidad depende de OSM; el geocoding automático
añade hasta ~5 s al guardado de una dirección (con el resto del perfil ya guardado).

**Qué haría que revisáramos esto:** que el volumen supere el free tier de forma sostenida, que la
cobertura en las direcciones reales del negocio sea insuficiente, o que se necesite geocodificación
en la app móvil.
