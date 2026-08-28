# ADR-005 · Migraciones Alembic por plugin (version_table separada)

**Estado**: Aceptada · **Fecha**: 2026-08-19

## Contexto

Ni agent-sale (56 migraciones planas en node-pg-migrate) ni agent-dashboard
(19 revisiones lineales de Alembic) tienen migraciones modulares. En un sistema de
plugins, cada plugin necesita evolucionar su schema sin tocar el árbol del core ni
el de otros plugins.

## Decisión

Cada plugin con tablas propias trae su PROPIO árbol Alembic:

```
plugins/pack_commerce/
├── alembic.ini              # version_table = alembic_version_pack_commerce
└── alembic/
    ├── env.py               # metadata = Base propia del plugin
    └── versions/0001_*.py
```

- El `plugin_manager` ejecuta `alembic upgrade head` del árbol del plugin al ACTIVARLO.
- `version_table` separada por plugin: los árboles no colisionan entre sí ni con el core.
- Tablas prefijadas (`crm_leads`) y FKs LÓGICAS hacia el core (columnas UUID sin
  constraint física).
- Al desinstalar un plugin sus tablas QUEDAN (los datos son del negocio).

## Justificación

- La alternativa (registrar migraciones de plugins en el árbol del core) acopla el
  versionado: desinstalar un plugin rompería la cadena de revisiones.
- FKs lógicas permiten instalar/desinstalar plugins sin `DROP CASCADE` accidentales.

## Consecuencias

- (+) Plugins 100% autocontenidos, instalables/desinstalables sin tocar el core.
- (−) Sin integridad referencial física plugin→core (aceptado: el core nunca borra
  customers/conversations; solo se archivan).
- (−) N version_tables en la BD (costo cosmético).
