-- Fixture database mirroring how data-quality-synchronization uses pgwarehouse: one Poka
-- tenant schema, synced to Snowflake by a read-only `snowflake_agent_user`. Column types and
-- names are taken from Poka's tenant schema dump (multi_tenant/schema_dump/schema_dump.sql.gz);
-- grants mirror data-quality-synchronization's create_user Lambda.

-- Aurora clones run in UTC (psql exports timestamptz in the server's timezone)
DO $$ BEGIN EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC'); END $$;

CREATE SCHEMA extensions;
CREATE EXTENSION hstore SCHEMA extensions;

CREATE SCHEMA acme;

-- One column per Postgres type found in Poka tenant tables (pgvector's vector(384) is
-- left out because the extension is not available in the test image)
CREATE TABLE acme.all_types (
    id integer PRIMARY KEY,
    c_integer integer,
    c_bigint bigint,
    c_smallint smallint,
    c_timestamptz timestamp with time zone,
    c_varchar varchar(255),
    c_boolean boolean,
    c_uuid uuid,
    c_hstore extensions.hstore,
    c_text text,
    c_jsonb jsonb,
    c_interval interval,
    c_varchar_arr varchar(100)[],
    c_integer_arr integer[],
    c_date date,
    c_time time without time zone,
    c_double double precision,
    c_bytea bytea,
    c_timestamptz_arr timestamp with time zone[],
    c_numeric_20_6 numeric(20,6),
    c_numeric_10_8 numeric(10,8),
    c_inet inet
);
INSERT INTO acme.all_types VALUES (
    1, 42, 9007199254740993, 7, '2026-10-13 13:45:00+00', 'varchar', true,
    'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '"en"=>"Name, \"quoted\"", "fr"=>"Nom"', 'text',
    '{"k": "v", "n": 1}', '1 day 02:00:00', '{a,"b c"}', '{1,2}', '2026-10-13', '13:45:00',
    2.5, '\x48656c6c6f', '{"2026-10-13 13:45:00+00"}', 1234.567891, 0.12345678, '10.0.0.1'
);
INSERT INTO acme.all_types (id) VALUES (2);

-- Text values that stress CSV round-tripping (rows are inserted by the tests)
CREATE TABLE acme.text_edge_cases (
    id integer PRIMARY KEY,
    value text
);

-- Column names from Poka tenant tables that are reserved or unusual in Snowflake
CREATE TABLE acme.reserved_names (
    id integer PRIMARY KEY,
    "order" integer,
    "values" jsonb,
    "start" timestamp with time zone,
    "current" boolean,
    "end" timestamp with time zone,
    "default" boolean,
    "_data" jsonb
);
INSERT INTO acme.reserved_names VALUES (1, 1, '{}', now(), true, now(), false, '{}');

CREATE TABLE acme.empty_table (
    id integer PRIMARY KEY,
    name varchar(50) NOT NULL
);

CREATE TABLE acme.big_table (
    id integer PRIMARY KEY,
    payload text
);
INSERT INTO acme.big_table SELECT g, md5(g::text) FROM generate_series(1, 5000) g;

-- Legacy single-tenant instances keep their tables in `public` (the weekly prod sync uses it)
CREATE TABLE public.legacy_table (id integer PRIMARY KEY, name varchar(50) NOT NULL);
INSERT INTO public.legacy_table VALUES (1, 'legacy');

-- Same grants as data-quality-synchronization's create_user Lambda
CREATE ROLE snowflake_agent_user LOGIN PASSWORD 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11';
GRANT USAGE ON SCHEMA acme TO snowflake_agent_user;
GRANT SELECT ON ALL TABLES IN SCHEMA acme TO snowflake_agent_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA acme GRANT SELECT ON TABLES TO snowflake_agent_user;
GRANT USAGE ON SCHEMA extensions TO snowflake_agent_user;
GRANT SELECT ON ALL TABLES IN SCHEMA extensions TO snowflake_agent_user;
GRANT USAGE ON SCHEMA public TO snowflake_agent_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO snowflake_agent_user;
