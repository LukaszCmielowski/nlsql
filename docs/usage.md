# CLI usage

Run the commands from the repository root. The [quick start](../README.md#quick-start) includes example commands.

## Demo data and questions

Table lists and context files are optional. The graph discovers tables through DCH, then retrieves PostgreSQL native types, primary/foreign keys, and table/column comments through the same connection. For example, it discovers `unit_price` as `numeric(10,2)` and the join path from customers through orders to order items.

Business definitions can still be ambiguous. Specify “completed orders” or “all orders” in the question when that matters. An optional [business context file](../examples/demo_context.txt) supplies organization-specific rules that are not documented in the database; it is not required for schema discovery or valid joins.

| Demo table | Rows | Purpose |
| --- | ---: | --- |
| `nlsql_demo.customers` | 6 | Customers and countries; includes one customer without orders. |
| `nlsql_demo.orders` | 12 | Order headers, dates, and completed/pending/cancelled statuses. |
| `nlsql_demo.order_items` | 24 | Products, quantities, and unit prices in USD. |

Other questions to try without extra options:

- “Which three customers spent the most on completed orders? Show total spending.”
- “Which three customers spent the least across all orders? Show total spending.”
- “Which customers have never placed an order?” — Sofia Rossi in the unmodified demo.
- “Show completed revenue by country.”
- “Show completed revenue by month for January through April 2026.”

The demo dates are fixed; they do not represent current sales.

## Reading answers

Answers show the question and results first, followed by formatted SQL and the connection ID. Column headings use readable labels, numbers are aligned and grouped, and missing values appear as `NULL`. For example, a top-customer query over completed orders produces:

```text
Results
╭───────────────┬─────────────╮
│ Customer name │ Total spent │
├───────────────┼─────────────┤
│ Jordan Lee    │       2,700 │
│ Jan Kowalski  │       1,260 │
│ Lena Fischer  │         730 │
╰───────────────┴─────────────╯
3 rows returned · query limit 3
```

The footer shows the SQL query's actual limit, which may be lower than `--max-rows`. Formatting preserves decimal precision and text identifiers; it does not infer currency or request a model summary. Terminal colors are omitted when output is redirected.

While waiting, an animated spinner shows the current stage: schema discovery and inspection, SQL generation, validation, database planning, execution, or correction. It clears before results or errors are printed, including on interruption. The spinner is disabled when output is redirected. This applies to `ask`, `query`, and the setup wizard's question step.

## Commands

| Command | Purpose | Calls the model? |
| --- | --- | --- |
| `setup` | Guided setup and optional end-to-end question. | In its optional model phase. |
| `connection-types` | List available DCH connection types. | No |
| `connections` | List visible DCH connections. | No |
| `create-connection` | Register a PostgreSQL source in DCH. | No |
| `schema` | Inspect live table and column metadata. | No |
| `query 'SQL'` | Validate and execute supplied SQL through DCH. | No |
| `ask 'QUESTION'` | Generate SQL and execute it through LangGraph and DCH. | Yes |

`--dry-run` still retrieves live metadata and, for `ask`, calls the model. PostgreSQL schema inspection uses fixed read-only catalog queries. The draft is checked with `EXPLAIN` (never `EXPLAIN ANALYZE`); the generated SELECT itself is not executed.

| Option | Applies to | Behavior |
| --- | --- | --- |
| `--connection-id ID` | `schema`, `query`, `ask` | Overrides `DCH_CONNECTION_ID`. |
| `--table schema.table` | `schema`, `query`, `ask` | Repeat to restrict included tables; for queries it also restricts permitted source tables. Without it, all tables returned by DCH are included. |
| `--context FILE` | `ask` | Adds UTF-8 business descriptions appropriate for the selected connection. |
| `--dialect NAME` | `schema`, `query`, `ask` | `postgres`, `sqlite`, or `duckdb`; match the source's dialect. |
| `--max-rows N` | `query`, `ask` | Limits returned rows; default 100, maximum 10,000. |
| `--dry-run` | `query`, `ask` | Validates and plans without executing the generated SELECT. |
| `--max-corrections 0` | `ask` | Disables the default single SQL correction attempt. |

The wizard and demo target PostgreSQL. One question queries one connection; there is no federation across connections. Use `uv run dch-nlsql COMMAND --help` for command options. `uv run python main.py ...` provides the same CLI.
