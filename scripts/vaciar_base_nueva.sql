-- Vacía la base NUEVA de producción justo antes de restaurar los datos
-- (`docs/runbooks/produccion-vps.md`, HU-ARCH-01).
--
-- La base nueva se crea con las migraciones (esquema desde cero) y el arranque de la API
-- escribe filas propias: el negocio `tenant-1` de la migración 0003, los planes, los tipos
-- de conexión y el registro de plugins. Esas filas chocarían con las de la base de origen
-- en `pg_restore --data-only`, así que se borran todas y queda solo el esquema.
--
-- Seguro: solo vacía una base marcada a mano al crearla en el servidor nuevo con
--     COMMENT ON DATABASE agent_core IS 'solvershub-produccion-vps';
-- La base de origen (la "producción" de la máquina de desarrollo) no tiene esa marca, así
-- que este archivo no la puede vaciar aunque se corra contra ella por error.
--
-- No toca las tablas de las extensiones (p. ej. `spatial_ref_sys` de PostGIS).
--
-- Uso, como superusuario y en el servidor de datos:
--     docker exec -i <contenedor-postgres> \
--       psql -U postgres -d agent_core -v ON_ERROR_STOP=1 < vaciar_base_nueva.sql
DO $$
DECLARE
    marca  text;
    tablas text;
    total  integer;
BEGIN
    SELECT shobj_description(d.oid, 'pg_database')
      INTO marca
      FROM pg_database d
     WHERE d.datname = current_database();

    IF marca IS DISTINCT FROM 'solvershub-produccion-vps' THEN
        RAISE EXCEPTION 'La base % no tiene la marca de la base nueva (COMMENT ON DATABASE % IS ''solvershub-produccion-vps''): no se vacía.',
            current_database(), current_database();
    END IF;

    SELECT string_agg(format('%I.%I', n.nspname, c.relname), ', ' ORDER BY n.nspname, c.relname),
           count(*)
      INTO tablas, total
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relkind IN ('r', 'p')
       AND n.nspname <> 'information_schema'
       AND n.nspname !~ '^pg_'
       AND NOT EXISTS (
               SELECT 1
                 FROM pg_depend d
                WHERE d.classid = 'pg_class'::regclass
                  AND d.objid = c.oid
                  AND d.deptype = 'e');

    IF total = 0 THEN
        RAISE EXCEPTION 'La base % no tiene tablas: ¿corrieron las migraciones?', current_database();
    END IF;

    EXECUTE 'TRUNCATE TABLE ' || tablas || ' RESTART IDENTITY';
    RAISE NOTICE 'Base % vaciada: % tablas sin filas, esquema intacto.', current_database(), total;
END
$$;
