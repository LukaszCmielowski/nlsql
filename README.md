# Natural-language SQL through Data Connect Hub

A small Python CLI prototype using **LangGraph** to orchestrate natural-language queries. **Every live schema lookup and SQL query uses the DCH Python SDK.** Query execution uses DCH credentials; PostgreSQL credentials are needed only when registering a new source.

```mermaid
flowchart TD
    Q[Question + selected DCH connection] --> S[DCH schema discovery]
    S --> G[Generate SQL with configured LLM]
    G --> V[Validate SQL + result limit]
    V -->|Valid| E[Execute through DCH SDK]
    V -->|Column error, one correction available| C[Generate corrected SQL]
    V -->|Rejected or dry run| Stop[Stop]
    E -->|Success| R[SQL + result table]
    E -->|Recognized SQL error, one correction available| C
    C --> V
    E -->|Other error or correction exhausted| Stop
```

`nlsql/workflow.py` defines a real LangGraph `StateGraph` with separate schema, generation, validation, execution, and repair nodes. The generation nodes use the OpenAI client protocol, so a RHOAI/vLLM endpoint works without requiring model tool calling, embeddings, or a vector database. DCH is pinned to a verified source commit; `uv.lock` records the resolved dependencies.

## Setup

Requires Python 3.11+, `uv`, Git, a running DCH gateway reachable from your machine, and a PostgreSQL source. The wizard can register your first DCH connection.

For guided setup, run this from the project directory in an interactive terminal:

```sh
uv run dch-nlsql setup
```

The wizard saves progress in `.env` and walks through five numbered stages. On reruns, it shows a summary of saved gateway and model settings: press Enter to reuse them, or answer `n` to edit. Tokens and passwords appear only as `saved (hidden)`.

1. DCH gateway, tenant, token, and trusted gateway CA. If settings are missing, it can use your current `oc` login for the token and the standard OpenShift gateway route.
2. Selecting an existing PostgreSQL connection or creating one, resolving the connection type by name. Only PostgreSQL sources appear in the menu; the saved connection is marked. You can provide an existing credential Secret, a PostgreSQL URI, or build the URI from host, port, database, username, password, and SSL mode.
3. Readiness, discovery of the three `nlsql_demo` tables, and a SQL row-count check through DCH. Missing tables trigger instructions for the existing seed script or SQL file. Secret access failures show tenant-specific RBAC commands, scoped to the selected connection's credential Secret for Flight, for an administrator to review and run. You can retry after fixing the step in another terminal.
4. An OpenAI-compatible model endpoint, model token, and optional private CA bundle (`SSL_CERT_FILE`). The wizard lists available ready models and saves the selected ID verbatim. If model listing is unavailable, it prompts for an exact ID.
5. An optional natural-language question executed through LangGraph and DCH. You can skip this step and finish with DCH SQL access verified.

Invalid ports, SSL modes, certificate paths, and menu selections prompt again in place. Certificate paths accept `~` and are saved as absolute paths. Optional values stay empty when skipped; `-` clears a saved optional value. After a successful question, the wizard prints SQL, results, and a command you can copy to ask another question.

The wizard masks token, password, and URI entry and saves `.env` with owner-only read/write permissions. It records a newly created connection ID before readiness checks so rerunning can reuse it. It displays administrator RBAC commands for relevant Secret access failures and database seeding instructions for missing tables; it does not execute those commands. If an endpoint call fails or you interrupt setup, correct that step and run `setup` again. Exported shell values override `.env`; the wizard names any conflicting variables so you can update their exports. No live model or cluster setup is performed simply by installing or testing the wizard.

The remaining sections document the equivalent manual steps.

```sh
uv sync --extra dev
cp .env.example .env
```

Fill in `.env`:

| Variable | Purpose |
| --- | --- |
| `DCH_HOST` | Gateway hostname and optional port, without an API path |
| `DCH_TOKEN` | Bearer token authorized for the selected tenant, without `Bearer ` |
| `DCH_TENANT_ID` | Tenant namespace |
| `DCH_CONNECTION_ID` | Connection ID, or pass `--connection-id` per command |
| `DCH_CA_CERT` | Optional trusted CA certificate file |
| `LLM_BASE_URL` | OpenAI-compatible API base URL, usually ending in `/v1` |
| `LLM_API_KEY` | Model endpoint token; leave empty for an unauthenticated endpoint. MaaS requires a valid API key. |
| `LLM_MODEL` | Model ID served by that endpoint |
| `NLSQL_DIALECT` | Source dialect: `postgres`, `sqlite`, or `duckdb` |

For OpenAI itself, use `https://api.openai.com/v1`. For OpenShift AI, use the deployed model's compatible base URL and model ID. Configure CA trust for a private model endpoint with the standard `SSL_CERT_FILE` environment variable.

For a vLLM deployment behind a gateway path such as `/llm/MODEL_NAME`, the API base may be `https://HOST/llm/MODEL_NAME/v1`; the client appends `/chat/completions` to that base. Preserve the path prefix required by the deployment. A model **HTTP 401** means authentication was rejected, possibly by the gateway before it checked the API path. Use the endpoint's real serving token/API key when required. When `LLM_API_KEY` is empty, the client sends no Authorization header and does not fall back to `OPENAI_API_KEY`. Model-serving credentials are independent of PostgreSQL credentials and DCH permissions.

Optional settings use empty strings, for example `LLM_API_KEY=`, `DCH_CA_CERT=`, `POSTGRES_CA_CERT=`, and `SSL_CERT_FILE=`. In the wizard, Enter accepts an empty optional value when none is saved; `-` clears a saved optional value to an empty string.

MaaS model IDs can include a publisher prefix, for example `publishers/llm/models/meta-llama/Llama-3.1-8B-Instruct`. Use the ID returned by MaaS model discovery, including its prefix and capitalization, rather than the model deployment's short name. The wizard discovers models through the configured API base's `/models` endpoint and preserves each ID exactly as returned.

For a cluster that accepts your OpenShift login token for DCH, you can set `export DCH_TOKEN="$(oc whoami -t)"` after logging in. Cluster access alone does not guarantee DCH tenant permissions or Flight access. TLS verification stays enabled; configure the appropriate CA rather than disabling verification. `.env` and local credential files are ignored by Git.

## Register your PostgreSQL database in DCH

Once the tables exist, register the database using the DCH SDK connection lifecycle. This follows the upstream [connection example](https://github.com/opendatahub-io/data-connect-hub/blob/6d58bc9975a431c8f8a9040e610520d20c980f36/sdk/python/examples/connections.py), while keeping the connection for subsequent queries.

1. Set `DCH_HOST`, `DCH_TOKEN`, and `DCH_TENANT_ID` in `.env`. `DCH_HOST` is the **DCH gateway**, not the PostgreSQL hostname on port 5432. Configure `DCH_CA_CERT` if the gateway uses a private CA.
2. Choose the existing connection type by name. The default is `Postgres`; you can list names if needed:

   ```sh
   uv run dch-nlsql connection-types
   ```

   The command looks up the ID automatically using an exact, case-insensitive name match. It checks that the selected type uses provider `postgres` and is Flight-ready. Missing or duplicate names produce an error before creation. If the type is absent, your DCH administrator needs to provision the built-in [Postgres connection type](https://github.com/opendatahub-io/data-connect-hub/blob/main/config/connection-types/postgres.yaml).
3. Put the PostgreSQL URI in `.env`, with credentials percent-encoded as needed:

   ```dotenv
   DCH_CONNECTION_TYPE_NAME=Postgres
   POSTGRES_URI='postgresql://USERNAME:PASSWORD@POSTGRES_HOST:5432/DATABASE'
   # Optional CA file for the source PostgreSQL server:
   # POSTGRES_CA_CERT=/absolute/path/to/postgres-ca.pem
   ```

   Add the TLS options required by your PostgreSQL deployment to the URI. `POSTGRES_CA_CERT` is read as PEM and stored as the source's `CA_CERT` credential; `DCH_CA_CERT` configures the client's connection to the gateway. The URI is read from the environment or `.env`, not supplied as a CLI argument or printed. PostgreSQL must be reachable **from DCH**, which may have different network access from your laptop.
4. Validate the credentials and create the connection:

   ```sh
   uv run dch-nlsql create-connection --name nlsql-demo-postgres --connection-type Postgres
   ```

   `--name` names the new connection; `--connection-type` selects an existing connection type and overrides `DCH_CONNECTION_TYPE_NAME`. If both are omitted, the connection name defaults to `nlsql-demo-postgres` and the type name defaults to `Postgres` (or the configured environment name). Use `--connection-type-id ID` for deployments with duplicate type names; it cannot be combined with `--connection-type`.

   After resolving the type ID, this calls `test_credentials`, creates a tabular connection with an inline credential Secret named `nlsql-postgres-…`, and checks readiness. Copy the printed `DCH_CONNECTION_ID=…` line into `.env`; the command does not edit `.env`. It leaves the connection and credential Secret in DCH. A repeated connection name is rejected with the existing connection ID rather than creating a duplicate. If readiness fails after creation, the ID is still printed so you can inspect the persisted connection; do not create another one just to retry readiness.
5. Verify the demo tables through DCH:

   ```sh
   uv run dch-nlsql schema --table nlsql_demo.customers \
     --table nlsql_demo.orders --table nlsql_demo.order_items
   ```

No separate PostgreSQL schema field is required when registering the connection; DCH discovers the schema and table names from the selected database.

If credentials are already stored in a Kubernetes Secret in `DCH_TENANT_ID`, use that reference instead of `POSTGRES_URI`:

```sh
uv run dch-nlsql create-connection --name nlsql-demo-postgres \
  --connection-type Postgres --credentials-secret YOUR_SECRET_NAME
```

The existing Secret needs a `URI` key and, when applicable, a `CA_CERT` key. This mode does not upload or validate inline credentials; it creates the connection and checks readiness. Your DCH token needs permission to create connections, and the DCH service needs tenant Secret permissions for the chosen credential mode. The query role in PostgreSQL must be able to read `nlsql_demo`.

## Run against DCH

List connections, then inspect the selected connection's live schema. These commands need no LLM:

```sh
uv run dch-nlsql connections
uv run dch-nlsql schema --connection-id CONNECTION_ID
```

Use the exact table labels reported by `schema`. For a PostgreSQL connection exposing `public.orders`:

```sh
# DCH connectivity and SQL execution smoke test, without an LLM.
uv run dch-nlsql query 'SELECT COUNT(*) AS count FROM public.orders'

# Generate and validate; --dry-run still retrieves live metadata and calls the LLM.
uv run dch-nlsql ask 'How many orders do we have?' --table public.orders --dry-run

# Generate, validate, execute through DCH, and display results.
uv run dch-nlsql ask 'How many orders do we have?' --table public.orders

# Disable the optional correction attempt.
uv run dch-nlsql ask 'How many orders do we have?' --max-corrections 0
```

`python main.py ...` also works inside the environment. Select another database with `--connection-id OTHER_ID --dialect sqlite`, for example. One question targets one connection; this sample does not federate joins across connections.

Repeat `--table` to restrict schema context and allowed source tables. Without it, all tables returned by DCH metadata are included. Optional `--context business-context.txt` adds descriptions, column meanings, and business rules to the prompt. These descriptions must be appropriate for the selected user and connection. No persistent schema or conversation cache is shared across connections.

## Set up a demo PostgreSQL dataset

The demo creates **three tables in the `nlsql_demo` schema of an existing database**: `customers` (6 rows), `orders` (12 rows), and `order_items` (24 rows). It includes completed, pending, and cancelled orders, several countries, and a customer without orders. Order dates are fixed in January–April 2026.

Run the setup directly against PostgreSQL with a user allowed to create the schema and tables:

```sh
bash scripts/setup_demo_db.sh \
  -h YOUR_POSTGRES_HOST -p 5432 -U YOUR_USERNAME -d YOUR_DATABASE
```

The wrapper requires `psql`, prompts for a password when required, and accepts standard PostgreSQL connection environment variables, including `PGSSLMODE` and `PGPASSFILE`. Alternatively, open [examples/demo_db.sql](examples/demo_db.sql) in Postico, DBeaver, pgAdmin, or PyCharm's SQL editor and execute the whole script.

The setup uses one transaction for all changes and preserves existing rows with matching primary keys when rerun. It does not delete data or reset edits. If unrelated tables already exist with these names and incompatible definitions, setup fails rather than migrating them. The wrapper stops on the first SQL error. A fresh setup reports **6 customers, 12 orders, 24 order items**, and **USD 5,510.00** in completed revenue.

Connect DCH to that same database. Its source role needs `USAGE` on `nlsql_demo` and `SELECT` on these tables. If it uses a different, existing read-only role, the setup owner can grant access in a SQL console (replace the role name):

```sql
GRANT USAGE ON SCHEMA nlsql_demo TO your_existing_dch_role;
GRANT SELECT ON ALL TABLES IN SCHEMA nlsql_demo TO your_existing_dch_role;
```

The setup script does not create roles or change credentials or grants automatically. DCH is used for querying; schema creation and data seeding use the direct PostgreSQL administrative connection.

After setting `DCH_CONNECTION_ID` and the LLM settings in `.env`:

```sh
uv run dch-nlsql schema --table nlsql_demo.customers \
  --table nlsql_demo.orders --table nlsql_demo.order_items

uv run dch-nlsql ask 'Which three customers spent the most?' \
  --table nlsql_demo.customers --table nlsql_demo.orders \
  --table nlsql_demo.order_items --context examples/demo_context.txt
```

The supplied [business context](examples/demo_context.txt) explains joins and revenue rules, which are not included in Arrow column metadata. Try “What is total completed revenue?” (USD 5,510.00), “Which customers have never placed an order?” (Sofia Rossi), or “Show completed revenue by country.” Expected results assume the seed data has not been edited.

## LangGraph workflow

`ask` runs the graph. The diagnostic `connections`, `schema`, and `query` commands continue to call DCH directly; `query` applies the same SQL validation without involving an LLM.

The application fixes the DCH client, connection ID, model, dialect, table selection, and limits in an immutable per-invocation `QueryContext`. These dependencies live outside graph state and are never supplied as model arguments. The model returns a SQL draft, and a deterministic validation node must accept it before DCH execution. Successful results stay local; there is no model summarization call.

Column references are checked against DCH's discovered schema before execution. A missing or ambiguous column can trigger a correction with specific local validation feedback. A recognizable source SQL error (syntax, missing/ambiguous column or table, or expression/type errors) can also trigger correction, using a fixed error category; raw DCH exception bodies are never forwarded. Both paths share **one** correction attempt by default. The model receives its previous SQL and the same schema, and the corrected SQL goes through all validation again. Unsafe SQL, refusals, incomplete model output, access failures, transport errors, timeouts, and unrecognized errors stop the workflow. `--max-corrections 0` disables correction. If correction fails, the final draft is displayed for diagnosis.

No checkpointer, persistent memory, or remote tracing is configured by this sample. If you enable framework tracing yourself, review what your tracing destination captures, including graph state and result rows.

## Validation and limits

- Requires a single SELECT, including supported CTEs and set operations; rejects writes, SELECT INTO, locks, unknown tables, cross-catalog references, and functions outside a conservative allowlist.
- Resolves CTEs separately from physical tables and qualifies physical names against the discovered metadata.
- Checks column references, including joins, aliases, CTEs, and correlated subqueries, against the discovered schema before execution.
- Adds or lowers the outer result limit (`--max-rows`, default 100, maximum 10,000).
- Validates again at the DCH execution boundary. Invalid model responses, refusals, and truncated generations never execute.
- Sends question, schema, and optional business context to the configured model. DCH tokens and result rows are not sent to it. No result explanation call is made.

This is a CLI prototype for a trusted operator. SQL parsing and table selection are **not an authorization or sandbox boundary**. Use read-only source credentials and source permissions for the intended tables/columns and rows. DCH metadata visibility can differ from query permissions. Custom operators, views, or overridden database functions can have effects that static checks cannot establish. Set source-side statement/resource limits: a row limit does not bound scanned data or runtime, and the SDK Flight timeout is not a guarantee of database-side cancellation. Column names and SQL semantics are ultimately checked by the source.

Live schema discovery requires DCH `get_tables(..., include_schema=True)` and serialized Arrow column schemas. Providers or older DCH deployments that do not implement this fail with an error; this sample never silently substitutes guessed schemas or a direct database connection. Large schemas should be narrowed with `--table`.

## Verification

A readiness failure reported as **HTTP 502 / `connection_check_failed`** can mean Flight cannot read the saved credential Secret. Flight may surface this as `Secret not found: Failed to obtain credentials` even when the Secret exists. Verify the actual service account against the exact Secret name:

```sh
oc auth can-i get secret/YOUR_CREDENTIAL_SECRET -n YOUR_TENANT \
  --as=system:serviceaccount:DCH_SERVICE_NAMESPACE:FLIGHT_SERVICE_ACCOUNT
```

If it returns `no`, have the tenant administrator apply the Secret-specific Role and RoleBinding printed by the wizard. A successful inline credential test does not exercise Flight's ability to read stored Secrets. The wizard reports the failed operation separately from schema discovery and SQL execution, and includes safe HTTP status/error-code diagnostics.

If `connection-types` reports **TLS certificate verification failed**, set `DCH_CA_CERT` to a trusted CA bundle for the DCH gateway. The CLI distinguishes TLS, DNS, refused connections, and timeouts without printing raw transport errors or credentials. Exported environment variables take precedence over `.env`.

For an OpenShift gateway using the cluster's default ingress certificate, you can obtain its public trust bundle through your authenticated OpenShift context:

```sh
mkdir -p certs
oc get configmap default-ingress-cert -n openshift-config-managed \
  -o jsonpath='{.data.ca-bundle\.crt}' > certs/dch-ingress-ca.pem
```

Set `DCH_CA_CERT` in `.env` to the absolute path of that file and retry `uv run dch-nlsql connection-types`. A gateway using a custom certificate may need a different CA bundle from its administrator. Certificate files ending in `.pem` are ignored by Git.

```sh
uv run pytest -q
uv run ruff check .
```

Tests invoke the real compiled LangGraph with real Arrow metadata serialization and mocked DCH/model clients. They cover connection routing, tenant propagation, SQL rejection, result limits, dry-run routing, correction budgets, rejection of unsafe corrections, error sanitization, and isolation between graph invocations. Live cluster and model verification requires deployment credentials.

## References

- [DCH SDK example](https://github.com/opendatahub-io/data-connect-hub/blob/6d58bc9975a431c8f8a9040e610520d20c980f36/sdk/python/examples/flight_query.py)
- [DCH SDK API](https://github.com/opendatahub-io/data-connect-hub/blob/6d58bc9975a431c8f8a9040e610520d20c980f36/sdk/python/src/data_connect_hub/client.py)
- [LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)
- [LangGraph SQL workflow example](https://docs.langchain.com/oss/python/langgraph/sql-agent)
