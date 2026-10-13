"""
Tests for the way data-quality-synchronization uses pgwarehouse (its only consumer).

data-quality-synchronization (docker/main.py) runs, against a point-in-time clone of a
Poka database, with configuration coming only from environment variables:

    for table in TABLES_TO_BACKUP.split(","):   # "all" or a comma separated list
        PGWarehouse(backend_type="snowflake", command="extract", table=table, data_dir=DATA_DIR)
    for table in TABLES_TO_BACKUP.split(","):
        PGWarehouse(backend_type="snowflake", command="reload", table=table, data_dir=DATA_DIR)

These tests run that exact sequence against a real Postgres (a Poka-like tenant schema, read
through a user with the same grants as production) and a recording fake of the Snowflake
connector, which captures every SQL statement and every file uploaded with PUT.

Tests marked xfail(strict=True) document known defects by asserting the safe behavior. They
start failing (XPASS) once the defect is fixed, at which point the marker must be removed.
"""

import csv
import gzip
import io
import os
import re

import psycopg2
import pytest
import testing.postgresql
from snowflake.connector.connection import DEFAULT_CONFIGURATION
from snowflake.connector.errors import DatabaseError, ProgrammingError

from pgwarehouse import snowflake_backend
from pgwarehouse.pgwarehouse import PGWarehouse

SF_DATABASE = "DEV_PGWAREHOUSE"
SF_SCHEMA = "ACME"
TENANT_TABLES = {"all_types", "big_table", "empty_table", "reserved_names", "text_edge_cases"}


def _setup_database(postgresql):
    conn = psycopg2.connect(**postgresql.dsn())
    with conn.cursor() as cursor:
        with open(os.path.join(os.path.dirname(__file__), "dqs_setup.sql")) as f:
            cursor.execute(f.read())
    conn.commit()
    conn.close()


DQSPostgresql = testing.postgresql.PostgresqlFactory(cache_initialized_db=True, on_initialized=_setup_database)


def teardown_module(module):
    DQSPostgresql.clear_cache()


###############
# Fake Snowflake connector
###############


class FakeSnowflake:
    """Records what pgwarehouse sends to Snowflake instead of talking to a real account."""

    def __init__(self):
        self.connect_calls = []
        self.statements = []
        self.uploads = {}  # table -> CSV text of the last file PUT for that table
        self.fail_when = None  # predicate on a statement; matching statements raise ProgrammingError
        self.copy_errors = 0  # rows reported as rejected by each COPY INTO

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        return FakeConnection(self)

    def statements_for(self, table):
        return [s for s in self.statements if re.search(rf"\b{table}\b", s)]

    def reloaded_tables(self):
        return {m.group(1) for s in self.statements if (m := re.match(r"CREATE TABLE IF NOT EXISTS \w+\.(\w+) ", s))}

    def columns(self, table):
        """Column name -> Snowflake type, from the CREATE TABLE statement issued for a table."""
        create = next(s for s in self.statements if re.match(rf"CREATE TABLE IF NOT EXISTS \w+\.{table} ", s))
        body = create[create.index("(") + 1 : create.rindex(")")]
        return dict(column.rsplit(" ", 1) for column in body.split(", "))

    def copy_statement(self, table):
        return next(s for s in self.statements_for(table) if s.startswith("COPY INTO"))

    def csv_rows(self, table):
        return list(csv.reader(io.StringIO(self.uploads[table])))


class FakeConnection:
    def __init__(self, fake):
        self.fake = fake

    def cursor(self):
        return FakeCursor(self.fake)


class FakeCursor:
    def __init__(self, fake):
        self.fake = fake
        self.rows = []

    def execute(self, sql):
        statement = " ".join(sql.split())
        self.fake.statements.append(statement)
        if self.fake.fail_when and self.fake.fail_when(statement):
            raise ProgrammingError(msg=f"SQL compilation error: {statement}")

        self.rows = []
        if put := re.match(r"PUT file://(\S+) @[\w.]+\.%(\w+);", statement):
            with gzip.open(put.group(1), "rt", newline="") as f:
                self.fake.uploads[put.group(2)] = f.read()
        elif copy := re.match(r"COPY INTO [\w.]+\.(\w+) FROM \S+/(\S+)", statement):
            parsed = len(self.fake.csv_rows(copy.group(1))) - 1
            errors = self.fake.copy_errors
            status = "PARTIALLY_LOADED" if errors else "LOADED"
            self.rows = [(copy.group(2), status, parsed, parsed - errors, parsed, errors, "error" if errors else None)]
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


###############
# Fixtures
###############


@pytest.fixture
def postgresql():
    postgresql = DQSPostgresql()
    yield postgresql
    postgresql.stop()


@pytest.fixture
def admin(postgresql):
    """Superuser connection, to arrange test data."""
    conn = psycopg2.connect(**postgresql.dsn())
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture
def snowflake(monkeypatch):
    fake = FakeSnowflake()
    monkeypatch.setattr(snowflake_backend.snowflake.connector, "connect", fake.connect)
    return fake


@pytest.fixture
def dqs_env(postgresql, monkeypatch):
    """The environment data-quality-synchronization's Fargate task sets."""
    dsn = postgresql.dsn()
    env = {
        "PGHOST": dsn["host"],
        "PGPORT": str(dsn["port"]),
        "PGDATABASE": dsn["database"],
        "PGUSER": "snowflake_agent_user",
        "PGPASSWORD": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "PGSCHEMA": "acme",
        "PGSSLMODE": "disable",  # production uses "require"; the test server has no TLS
        "SNOWSQL_ACCOUNT": "poka-test",
        "SNOWSQL_DATABASE": SF_DATABASE,
        "SNOWSQL_SCHEMA": SF_SCHEMA,
        "SNOWSQL_WAREHOUSE": "COMPUTE_WH",
        "SNOWSQL_USER": "sync_user",
        "SNOWSQL_PWD": "secret",
        "SNOWSQL_ROLE": "DATA_QUALITY_SYNC",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    # Same relative data dir as production, which runs from the container's working directory
    monkeypatch.chdir(tmp_path)
    return "./pgw_data"


def run_dqs_sync(tables_to_backup, data_dir):
    """Same calls as data-quality-synchronization docker/main.py sync_data()."""
    for table in tables_to_backup.split(","):
        PGWarehouse(backend_type="snowflake", command="extract", table=table, data_dir=data_dir)
    for table in tables_to_backup.split(","):
        PGWarehouse(backend_type="snowflake", command="reload", table=table, data_dir=data_dir)


###############
# Happy path
###############


def test_sync_all_reloads_every_table_of_the_tenant_schema(dqs_env, snowflake, data_dir):
    run_dqs_sync("all", data_dir)

    assert snowflake.reloaded_tables() == TENANT_TABLES
    assert snowflake.statements_for("legacy_table") == []


def test_sync_all_from_the_public_schema(dqs_env, snowflake, data_dir, monkeypatch):
    # The weekly prod sync: a legacy single-tenant instance, whose tables live in `public`
    monkeypatch.setenv("PGSCHEMA", "public")
    monkeypatch.setenv("SNOWSQL_SCHEMA", "MARS")

    run_dqs_sync("all", data_dir)

    assert snowflake.reloaded_tables() == {"legacy_table"}
    assert f"DROP TABLE IF EXISTS {SF_DATABASE}.MARS.legacy_table" in snowflake.statements
    assert snowflake.csv_rows("legacy_table") == [["id", "name"], ["1", "legacy"]]


def test_sync_table_list_only_reloads_listed_tables(dqs_env, snowflake, data_dir):
    run_dqs_sync("big_table,empty_table", data_dir)

    assert snowflake.reloaded_tables() == {"big_table", "empty_table"}


def test_reload_replaces_the_snowflake_table(dqs_env, snowflake, data_dir):
    run_dqs_sync("big_table", data_dir)

    statements = snowflake.statements_for("big_table")
    assert statements[0] == f"DROP TABLE IF EXISTS {SF_DATABASE}.{SF_SCHEMA}.big_table"
    assert statements[1].startswith(f"CREATE TABLE IF NOT EXISTS {SF_SCHEMA}.big_table ")
    assert re.match(rf"PUT file://\S+/big_table_data/big_table10\.csv\.gz @{SF_DATABASE}\.{SF_SCHEMA}\.%big_table;", statements[2])
    assert statements[3].startswith(f"COPY INTO {SF_DATABASE}.{SF_SCHEMA}.big_table FROM @{SF_DATABASE}.{SF_SCHEMA}.%big_table/big_table10.csv.gz")


def test_copy_into_options(dqs_env, snowflake, data_dir):
    # Pins the load contract: changing these options changes what lands in Snowflake
    run_dqs_sync("big_table", data_dir)

    copy = snowflake.copy_statement("big_table")
    options = copy[copy.index("FILE_FORMAT") :]
    assert options == "FILE_FORMAT = (type = csv field_optionally_enclosed_by='\\\"' SKIP_HEADER=1) ON_ERROR=CONTINUE FORCE=TRUE PURGE = TRUE"


def test_uploaded_csv_contains_every_row(dqs_env, snowflake, data_dir):
    run_dqs_sync("big_table", data_dir)

    rows = snowflake.csv_rows("big_table")
    assert rows[0] == ["id", "payload"]
    assert len(rows) == 1 + 5000
    assert rows[1] == ["1", "c4ca4238a0b923820dcc509a6f75849b"]


def test_empty_table_is_recreated_with_a_header_only_file(dqs_env, snowflake, data_dir):
    run_dqs_sync("empty_table", data_dir)

    assert snowflake.columns("empty_table") == {"id": "BIGINT", "name": "STRING"}
    assert snowflake.uploads["empty_table"] == "id,name\n"


def test_connects_with_the_dqs_credentials(dqs_env, snowflake, data_dir):
    run_dqs_sync("empty_table", data_dir)

    assert snowflake.connect_calls[0] == {
        "user": "sync_user",
        "password": "secret",
        "account": "poka-test",
        "database": SF_DATABASE,
        "schema": SF_SCHEMA,
        "client_session_keep_alive": True,
        "role": "DATA_QUALITY_SYNC",
    }
    assert "use warehouse COMPUTE_WH;" in snowflake.statements


def test_connection_parameters_are_supported_by_the_installed_connector(dqs_env, snowflake, data_dir):
    # Guards connector upgrades: an unknown or renamed connect() parameter would only fail at runtime
    run_dqs_sync("empty_table", data_dir)

    assert set(snowflake.connect_calls[0]) <= set(DEFAULT_CONFIGURATION)


###############
# Type mapping (Postgres types found in Poka tenant tables)
###############

TYPE_MAPPING = {
    "id": "BIGINT",
    "c_integer": "BIGINT",
    "c_bigint": "BIGINT",
    "c_smallint": "SMALLINT",
    "c_timestamptz": "TIMESTAMP",
    "c_varchar": "STRING",
    "c_boolean": "BOOLEAN",
    "c_uuid": "String",
    "c_hstore": "String",
    "c_text": "STRING",
    "c_jsonb": "STRING",
    "c_interval": "STRING",
    "c_varchar_arr": "String",
    "c_integer_arr": "String",
    "c_date": "DATETIME",
    "c_time": "TIME",
    "c_double": "FLOAT",
    "c_bytea": "String",
    "c_timestamptz_arr": "String",
    "c_numeric_20_6": "NUMBER(20,6)",
    "c_numeric_10_8": "NUMBER(10,8)",
    "c_inet": "String",
}


def test_column_type_mapping(dqs_env, snowflake, data_dir):
    run_dqs_sync("all_types", data_dir)

    assert snowflake.columns("all_types") == TYPE_MAPPING


@pytest.mark.parametrize(
    "pg_type,snowflake_type",
    [
        ("numeric", "FLOAT"),
        ("numeric(12)", "NUMBER(12,0)"),
        ("real", "FLOAT"),
        ("interval day", "STRING"),
        ("integer", "BIGINT"),
    ],
)
def test_numeric_and_interval_type_variants(pg_type, snowflake_type):
    # Variants not used by Poka tenant tables, so not in the fixture schema
    backend = object.__new__(snowflake_backend.SnowflakeBackend)

    assert backend.pg_to_sf_type(pg_type) == snowflake_type


def test_postgres_text_formats_are_exported_verbatim(dqs_env, snowflake, data_dir):
    run_dqs_sync("all_types", data_dir)

    header, row, null_row = snowflake.csv_rows("all_types")
    values = dict(zip(header, row))
    assert values["c_timestamptz"] == "2026-10-13 13:45:00+00"
    assert values["c_boolean"] == "t"
    assert values["c_uuid"] == "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    assert values["c_hstore"] == '"en"=>"Name, \\"quoted\\"", "fr"=>"Nom"'
    assert values["c_jsonb"] == '{"k": "v", "n": 1}'
    assert values["c_interval"] == "1 day 02:00:00"
    assert values["c_varchar_arr"] == '{a,"b c"}'
    assert values["c_timestamptz_arr"] == '{"2026-10-13 13:45:00+00"}'
    assert values["c_bytea"] == "\\x48656c6c6f"
    assert values["c_double"] == "2.5"
    assert values["c_numeric_20_6"] == "1234.567891"
    assert values["c_numeric_10_8"] == "0.12345678"
    assert null_row == ["2"] + [""] * (len(header) - 1)


###############
# Text values
###############

TEXT_EDGE_CASES = {
    1: "plain",
    2: "comma, separated",
    3: 'double "quotes"',
    4: "multi\nline",
    5: "windows\r\nline",
    6: "emoji 🎉 and accents éàü",
    7: "  padded  ",
    8: "<p>rich &amp; <b>html</b></p>",
    9: "x" * 100_000,
    10: "C:\\temp\\new",
    11: "\\N",
    12: "",
    13: None,
}


@pytest.fixture
def text_edge_cases(admin):
    with admin.cursor() as cursor:
        for id, value in TEXT_EDGE_CASES.items():
            cursor.execute("INSERT INTO acme.text_edge_cases VALUES (%s, %s)", (id, value))


def test_text_values_round_trip_through_csv(dqs_env, snowflake, data_dir, text_edge_cases):
    run_dqs_sync("text_edge_cases", data_dir)

    exported = {int(id): value for id, value in snowflake.csv_rows("text_edge_cases")[1:]}
    assert exported == {id: "" if value is None else value for id, value in TEXT_EDGE_CASES.items()}


def test_null_and_empty_string_are_distinguishable_in_csv(dqs_env, snowflake, data_dir, text_edge_cases):
    run_dqs_sync("text_edge_cases", data_dir)

    lines = snowflake.uploads["text_edge_cases"].splitlines()
    assert '12,""' in lines
    assert "13," in lines


@pytest.mark.xfail(
    strict=True,
    reason="Values containing a backslash but no comma/quote/newline are exported unquoted (bytea '\\x…', "
    "Windows paths, '\\N'). Snowflake's default NULL_IF=('\\\\N') loads '\\N' as NULL (documented), and its default "
    "ESCAPE_UNENCLOSED_FIELD='\\\\' likely reinterprets other backslash sequences (to confirm against a real account)",
)
def test_unquoted_backslashes_are_loaded_verbatim(dqs_env, snowflake, data_dir, text_edge_cases):
    run_dqs_sync("text_edge_cases", data_dir)

    assert "10,C:\\temp\\new" in snowflake.uploads["text_edge_cases"].splitlines()
    # A fix moving these options to a named file format must update this assertion
    copy = snowflake.copy_statement("text_edge_cases")
    assert re.search(r"ESCAPE_UNENCLOSED_FIELD\s*=\s*NONE", copy, re.IGNORECASE)
    assert re.search(r"NULL_IF\s*=\s*\(\s*\)", copy, re.IGNORECASE)


###############
# Column names
###############


@pytest.mark.parametrize("column", ["order", "values", "start", "current"])
def test_snowflake_reserved_column_names_are_quoted(dqs_env, snowflake, data_dir, column):
    # Quoted lowercase, so these columns are case-sensitive in Snowflake (e.g. SELECT "order")
    run_dqs_sync("reserved_names", data_dir)

    assert f'"{column}"' in snowflake.columns("reserved_names")


@pytest.mark.parametrize("column", ["end", "default", "_data"])
def test_other_unusual_column_names_are_left_unquoted(dqs_env, snowflake, data_dir, column):
    run_dqs_sync("reserved_names", data_dir)

    assert column in snowflake.columns("reserved_names")


###############
# Failure modes
###############


@pytest.mark.parametrize("missing", ["PGHOST", "PGUSER", "SNOWSQL_ACCOUNT", "SNOWSQL_PWD", "SNOWSQL_ROLE"])
def test_missing_configuration_fails_fast(dqs_env, snowflake, data_dir, monkeypatch, missing):
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=f"Missing {missing.lower()}"):
        run_dqs_sync("big_table", data_dir)
    assert snowflake.statements_for("big_table") == []


def test_postgres_connection_failure_fails_fast(dqs_env, snowflake, data_dir, monkeypatch):
    monkeypatch.setenv("PGPORT", "1")

    with pytest.raises(psycopg2.OperationalError):
        run_dqs_sync("big_table", data_dir)
    assert snowflake.statements == []


def test_snowflake_connection_failure_fails_fast(dqs_env, snowflake, data_dir, monkeypatch):
    def refuse(**kwargs):
        raise DatabaseError(msg="Incorrect username or password was specified.")

    monkeypatch.setattr(snowflake_backend.snowflake.connector, "connect", refuse)

    with pytest.raises(DatabaseError):
        run_dqs_sync("big_table", data_dir)


def fail_on_create_empty_table(statement):
    return statement.startswith(f"CREATE TABLE IF NOT EXISTS {SF_SCHEMA}.empty_table ")


def test_snowflake_error_aborts_the_sync(dqs_env, snowflake, data_dir):
    # Tables are reloaded in alphabetical order: the ones after the failing table keep their previous data
    snowflake.fail_when = fail_on_create_empty_table

    with pytest.raises(ProgrammingError):
        run_dqs_sync("all", data_dir)

    assert snowflake.reloaded_tables() == {"all_types", "big_table", "empty_table"}


@pytest.mark.xfail(strict=True, reason="reload drops the Snowflake table before recreating and loading it, so a failure leaves it missing")
def test_snowflake_error_does_not_leave_the_table_dropped(dqs_env, snowflake, data_dir):
    snowflake.fail_when = fail_on_create_empty_table

    with pytest.raises(ProgrammingError):
        run_dqs_sync("all", data_dir)

    assert f"DROP TABLE IF EXISTS {SF_DATABASE}.{SF_SCHEMA}.empty_table" not in snowflake.statements


@pytest.mark.xfail(strict=True, reason="A table that is not found, or an empty list item, is skipped with a print, and the sync succeeds")
@pytest.mark.parametrize("tables", ["big_table,not_a_table", "big_table, empty_table", "big_table,"])
def test_unknown_table_in_list_fails_the_sync(dqs_env, snowflake, data_dir, tables):
    with pytest.raises(Exception):
        run_dqs_sync(tables, data_dir)


@pytest.mark.xfail(strict=True, reason="With table='all', a RuntimeError for one table is printed and swallowed, and the sync succeeds")
def test_failing_table_in_all_mode_fails_the_sync(dqs_env, snowflake, data_dir, monkeypatch):
    dump_schema = PGWarehouse.dump_schema

    def failing_dump_schema(self, table, schema_file):
        if table == "empty_table":
            raise RuntimeError("Error saving schema")
        return dump_schema(self, table, schema_file)

    monkeypatch.setattr(PGWarehouse, "dump_schema", failing_dump_schema)

    with pytest.raises(RuntimeError):
        run_dqs_sync("all", data_dir)


@pytest.fixture
def unreadable_big_table(admin):
    # A column-level grant keeps the table visible to the sync user, but SELECT * fails
    with admin.cursor() as cursor:
        cursor.execute("REVOKE SELECT ON acme.big_table FROM snowflake_agent_user")
        cursor.execute("GRANT SELECT (id) ON acme.big_table TO snowflake_agent_user")


def test_extraction_failure_fails_the_sync(dqs_env, snowflake, data_dir, unreadable_big_table):
    with pytest.raises(RuntimeError, match="Error extracting table big_table"):
        run_dqs_sync("big_table", data_dir)

    assert snowflake.statements_for("big_table") == []


def test_extraction_failure_in_all_mode_keeps_the_snowflake_table(dqs_env, snowflake, data_dir, unreadable_big_table):
    run_dqs_sync("all", data_dir)

    assert snowflake.statements_for("big_table") == []
    assert snowflake.reloaded_tables() == TENANT_TABLES - {"big_table"}


@pytest.mark.xfail(strict=True, reason="COPY INTO uses ON_ERROR=CONTINUE: rejected rows are only logged")
def test_rejected_rows_fail_the_sync(dqs_env, snowflake, data_dir):
    snowflake.copy_errors = 1

    with pytest.raises(Exception):
        run_dqs_sync("big_table", data_dir)
