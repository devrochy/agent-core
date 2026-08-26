# Seguridad de agent-core

*Documento público. Dos páginas. Se entrega en el proceso de venta.*
**Estado: esqueleto — completar en Sprint 5.**

## Aislamiento entre clientes
Multi-tenant con Row Level Security a nivel de base de datos, no solo a nivel de aplicación.
Toda tabla lleva identificador de cliente y una política que impide el acceso cruzado incluso
ante un error de programación. Modos de despliegue disponibles: compartido, datos aislados,
infraestructura dedicada.

## Cifrado
En tránsito (TLS) y en reposo. Las credenciales de cada cliente (tokens de Meta, claves de
API de sus sistemas) se almacenan cifradas con clave gestionada y son rotables.

## Control de acceso
Roles y permisos con principio de menor privilegio. Registro de auditoría de toda acción
administrativa: quién, qué, cuándo.

## Tratamiento de datos
- Retención configurable por cliente; supresión a solicitud.
- **Los datos del cliente no se utilizan para entrenar modelos.**
- Lista de subprocesadores publicada: proveedor de LLM, proveedor de WhatsApp, hosting,
  observabilidad.
- Cumplimiento de la Ley 1581 de 2012 (Colombia): aviso de privacidad, rol de encargado del
  tratamiento, procedimiento de supresión.

## Manejo de contenido no confiable
El contenido de mensajes, documentos y sistemas externos se trata como dato, nunca como
instrucción para el agente. Las acciones con efecto irreversible requieren confirmación
explícita.

## Confiabilidad
| Métrica | Compromiso |
|---|---|
| Disponibilidad | 99,5% mensual |
| p95 primera respuesta | < 6 s |
| Mensajes perdidos | 0 (cola con reintento y bandeja de fallos) |
| RTO | < 4 h |
| RPO | < 15 min |

## Respuesta a incidentes
Canal de reporte, tiempo de notificación al cliente, y postmortem escrito para todo
incidente con impacto en servicio.

## Portabilidad
Exportación completa de conversaciones, contactos y configuración en formato abierto,
en cualquier momento y sin costo.
