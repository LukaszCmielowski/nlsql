import pytest
import sqlglot

from nlsql.schema import TableSchema
from nlsql.validation import ColumnValidationError, validate_sql

TABLES = [TableSchema("orders", "public", (("id", "int64"), ("amount", "double")))]


def validate(sql, **kwargs):
    return validate_sql(sql, kwargs.get("tables", TABLES), kwargs.get("dialect", "postgres"), 25)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders",
        "SELECT * FROM public.orders LIMIT 1000",
        "WITH x AS (SELECT * FROM orders) SELECT * FROM x",
        "SELECT * FROM (SELECT * FROM orders) AS x",
        "SELECT id FROM orders UNION ALL SELECT id FROM orders",
        "SELECT CASE WHEN amount > 0 THEN amount ELSE 0 END FROM orders",
        "SELECT o.id FROM orders o JOIN orders p ON o.id = p.id",
        "SELECT * FROM orders WHERE id IN (SELECT id FROM orders)",
    ],
)
def test_valid_queries_have_bounded_results(sql):
    result = validate(sql)
    assert int(sqlglot.parse_one(result).args["limit"].expression.this) == 25
    assert '"public"."orders"' in result


def test_preserves_smaller_limit_and_is_idempotent():
    result = validate("SELECT * FROM orders LIMIT 3")
    assert result.endswith("LIMIT 3")
    assert validate(result) == result


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "-- nothing",
        "SELECT 1; SELECT 2",
        "SELECT * FROM orders; DROP TABLE orders",
        "DELETE FROM orders",
        "UPDATE orders SET amount=0",
        "DROP TABLE orders",
        "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d",
        "SELECT * INTO copy FROM orders",
        "SELECT * FROM orders FOR UPDATE",
        "COPY orders TO '/tmp/export'",
        "SELECT pg_sleep(1)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT set_config('search_path', 'private', false)",
        "SELECT * FROM read_csv('/tmp/data.csv')",
        "SELECT public.sum(amount) FROM orders",
        "SELECT * FROM private.orders",
        "SELECT * FROM secrets",
        "WITH orders AS (SELECT * FROM secrets) SELECT * FROM orders",
        "SELECT * FROM another.public.orders",
        "SELECT * FROM orders LIMIT -1",
        "SELECT * FROM orders LIMIT $1",
        "SELECT * FROM orders LIMIT '10'",
        "SELECT * FROM orders LIMIT 10 PERCENT",
    ],
)
def test_rejects_unsafe_or_unresolved_queries(sql):
    with pytest.raises((ValueError, sqlglot.errors.SqlglotError)):
        validate(sql)


def test_ambiguous_unqualified_table_is_rejected():
    with pytest.raises(ValueError, match="ambiguous"):
        validate("SELECT * FROM orders", tables=TABLES + [TableSchema("orders", "other", ())])


def test_quoted_case_sensitive_table():
    tables = [TableSchema("Orders", "public", ())]
    assert '"Orders"' in validate('SELECT * FROM public."Orders"', tables=tables)
    with pytest.raises(ValueError):
        validate("SELECT * FROM public.Orders", tables=tables)


@pytest.mark.parametrize("dialect", ["postgres", "sqlite", "duckdb"])
def test_supported_dialects(dialect):
    assert validate("SELECT COUNT(*) FROM orders", dialect=dialect).endswith("LIMIT 25")


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb"])
@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "OrderID" FROM "Sales"."OrderItems"',
        "SELECT OrderItems.OrderID FROM OrderItems",
    ],
)
def test_case_insensitive_dialects_resolve_mixed_case_catalog_names(dialect, sql):
    tables = [TableSchema("OrderItems", "Sales", (("OrderID", "int64"),))]
    validated = validate(sql, tables=tables, dialect=dialect)
    assert '"Sales"."OrderItems"' in validated
    assert validate(validated, tables=tables, dialect=dialect) == validated


def test_postgres_keeps_quoted_mixed_case_columns_distinct():
    tables = [TableSchema("orders", "public", (("OrderID", "int64"),))]
    assert 'SELECT "OrderID"' in validate('SELECT "OrderID" FROM orders', tables=tables)
    with pytest.raises(ColumnValidationError):
        validate("SELECT OrderID FROM orders", tables=tables)


@pytest.mark.parametrize("dialect", ["sqlite", "duckdb"])
def test_case_insensitive_dialects_preserve_non_ascii_identifier_distinctions(dialect):
    tables = [
        TableSchema("Événements", "main", (("État", "string"),)),
        TableSchema("événements", "main", (("état", "string"),)),
    ]
    sql = validate('SELECT "ÉTAT" FROM "ÉVéNEMENTS"', tables=tables, dialect=dialect)
    assert '"main"."Événements"' in sql
    assert '"État"' in sql
    with pytest.raises(ColumnValidationError):
        validate('SELECT "état" FROM "Événements"', tables=tables, dialect=dialect)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT nonexistent FROM orders",
        "SELECT o.amount FROM orders x",
        "SELECT id FROM orders o JOIN orders p ON o.id = p.id",
        "WITH totals AS (SELECT SUM(amount) AS total FROM orders) SELECT amount FROM totals",
        "SELECT (SELECT MAX(p.amount) FROM orders p WHERE p.missing = o.id) FROM orders o",
        "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM orders p WHERE p.missing = o.id)",
    ],
)
def test_missing_and_ambiguous_columns_are_rejected(sql):
    with pytest.raises(ColumnValidationError):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH totals AS (SELECT SUM(amount) AS total FROM orders) SELECT total FROM totals",
        "SELECT (SELECT MAX(p.amount) FROM orders p WHERE p.id = o.id) FROM orders o",
        "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM orders p WHERE p.id = o.id)",
        "SELECT SUM(amount) AS total FROM orders ORDER BY total",
        "SELECT x.amount FROM (SELECT amount FROM orders) x",
    ],
)
def test_column_validation_resolves_ctes_subqueries_and_output_aliases(sql):
    assert validate(sql).endswith("LIMIT 25")


def test_column_check_does_not_trust_a_column_from_the_wrong_joined_table():
    tables = TABLES + [TableSchema("customers", "public", (("id", "int32"), ("name", "string")))]
    with pytest.raises(ColumnValidationError, match="amount"):
        validate("SELECT c.amount FROM orders o JOIN customers c ON o.id = c.id", tables=tables)
