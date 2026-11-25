## El script de migración de datos del cliente carga también Cobranza (2026-11-25)

- `scripts/migrar_datos_cliente_agentes.py` no importaba `pack_cobranza.datos`: en un ambiente con política global, un agente de Cobranza habría quedado sin sus operaciones de datos del cliente (`cobranza.promesa_de_pago`, HU-AOS-93). Producción no se ve afectada: su dry-run no encontró ningún negocio con política global.
