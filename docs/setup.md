# Setup and troubleshooting

Run the commands from the repository root. See the [quick start](../README.md#quick-start) for installation and demo data.

## Guided setup

Run `uv run dch-nlsql setup` to check your DCH connection and try a demo question.

You do not need to copy `.env.example` first. The wizard creates or updates `.env` through five stages:

| Stage | What happens |
| --- | --- |
| 1. Connect to DCH | Enter the gateway, tenant, bearer token, and optional trusted gateway CA. |
| 2. Choose a connection | Reuse a PostgreSQL connection or register one using an existing credential Secret, a PostgreSQL URI, or individual database fields. The connection type is resolved by name. |
| 3. Check the database | Check readiness, discover the three demo tables, and run a customer count through DCH. |
| 4. Connect a model | Enter its API base URL, API key, and optional CA bundle. Select an exact discovered model ID, or enter it manually if listing is unavailable. Choose a text-generation model, not an embedding model. |
| 5. Ask a question | Generate and validate SQL, execute it through DCH, and display SQL and results. |

On reruns, press Enter to reuse a saved settings section, or answer `n` to edit. Credentials are hidden. Optional values may be empty; `-` clears a saved optional value. Certificate paths accept `~` and are saved as absolute paths. Invalid inputs prompt again in place.

Progress is saved after each setting, including a newly created connection ID before readiness checks. If a step fails, fix the reported problem and rerun setup. You can also skip model setup and finish with DCH SQL access verified.

The wizard can register a persistent DCH connection and its credential Secret. It displays seeding instructions and relevant administrator RBAC commands when needed; it does not execute those instructions or change cluster permissions. The wizard saves `.env` with owner-only read/write permissions, and Git ignores local `.env` files.

## Configuration reference

The wizard manages `.env`. For manual setup, copy [the template](../.env.example) **only if `.env` does not already exist**, then fill in its values:

```sh
cp -n .env.example .env
```

Commands read `.env` from the current directory. Exported shell variables take precedence, so update or unset conflicting exports when changing `.env`. Setup reports conflicting variable names without printing credentials.

| Variable | Meaning |
| --- | --- |
| `DCH_HOST` | DCH gateway hostname and optional port, such as `gateway.example.com:443`. Omit `https://` and API paths. This is separate from the PostgreSQL host on port 5432. |
| `DCH_TENANT_ID` | DCH tenant's Kubernetes namespace. |
| `DCH_TOKEN` | Bearer token authorized for that tenant, without the `Bearer ` prefix. |
| `DCH_CONNECTION_ID` | Saved connection ID; required for query commands unless supplied with `--connection-id`. |
| `DCH_CA_CERT` | Optional trusted CA file for the DCH gateway. Empty uses the default trust store. |
| `DCH_CONNECTION_TYPE_NAME` | Existing type name used when registering PostgreSQL; defaults to `Postgres`. |
| `POSTGRES_URI` | PostgreSQL URI, needed only to register a source with inline credentials. |
| `POSTGRES_CA_CERT` | Optional source PostgreSQL CA file uploaded with inline credentials. Distinct from the gateway CA. |
| `LLM_BASE_URL` | OpenAI-compatible API root, including any gateway prefix and `/v1` where required. |
| `LLM_MODEL` | Exact model ID, including prefixes and capitalization returned by discovery. |
| `LLM_API_KEY` | Model API key. Leave empty only for endpoints that allow anonymous access. |
| `SSL_CERT_FILE` | Optional CA bundle used by the model's HTTP client for private certificates. |
| `DCH_FLIGHT_TIMEOUT` | Timeout in seconds for Flight discovery RPCs and SQL query/fetch operations. Defaults to `30`. |
| `LLM_TIMEOUT` | Model request timeout in seconds; default `60`. |
| `NLSQL_DIALECT` | Default dialect for `schema`, `ask`, and `query`; default `postgres`. |
| `NLSQL_MAX_ROWS` | Default row limit for `ask` and `query`; default `100`. |

Keep optional values empty, for example `LLM_API_KEY=` or `DCH_CA_CERT=`. When `LLM_API_KEY` is empty, the client omits the Authorization header and does not fall back to `OPENAI_API_KEY`. Model credentials, DCH tokens, and PostgreSQL credentials serve different purposes.

### Model URLs and MaaS IDs

The client appends `/chat/completions` to `LLM_BASE_URL` and uses `/models` for discovery. Preserve required gateway path prefixes. For example, an endpoint routed under `/llm/MODEL_NAME` may need `https://HOST/llm/MODEL_NAME/v1` as its base URL.

For MaaS, use your deployment's OpenAI-compatible API base URL and a real MaaS API key. The wizard lists ready models and preserves the selected ID verbatim. An ID may look like `publishers/llm/models/meta-llama/Llama-3.1-8B-Instruct`; the short deployment name may produce HTTP 404. If model listing is unavailable, enter the exact ID supplied by your serving administrator.

## Register a PostgreSQL source manually

Skip this section if the wizard already saved a working connection. Configure the DCH gateway, tenant, and token first. If your cluster accepts your OpenShift login token for DCH, obtain one after `oc login` with:

```sh
export DCH_TOKEN="$(oc whoami -t)"
```

The wizard can discover the standard `data-science-gateway` route in `openshift-ingress` when the host is missing. Otherwise, obtain the gateway address from your DCH administrator. Cluster login alone does not establish DCH tenant access or service access to credential Secrets.

List connection types:

```sh
uv run dch-nlsql connection-types
```

Set the source URI in `.env`, with username and password percent-encoded as needed:

```dotenv
DCH_CONNECTION_TYPE_NAME=Postgres
POSTGRES_URI='postgresql://USERNAME:PASSWORD@POSTGRES_HOST:5432/DATABASE?sslmode=require'
```

Choose `sslmode` and an optional `POSTGRES_CA_CERT` appropriate for your PostgreSQL deployment. The wizard can build the URI from individual fields and encode credentials for you. No separate PostgreSQL schema field is needed: DCH discovers schemas and tables in the selected database.

```sh
uv run dch-nlsql create-connection \
  --name nlsql-demo-postgres --connection-type Postgres
```

The command resolves the type ID using an exact, case-insensitive name match, checks its PostgreSQL provider and Flight readiness, tests inline credentials, creates the connection and credential Secret, and checks readiness. If the type is missing, an administrator must provision it. For duplicate type names, use `--connection-type-id ID` instead of `--connection-type`.

Copy the printed `DCH_CONNECTION_ID=...` into `.env`; this manual command does not edit `.env`. Creation persists even if readiness fails. Reuse the printed ID when troubleshooting. Repeating an existing connection name is rejected with its ID.

To reference an existing credential Secret in the tenant instead of uploading `POSTGRES_URI`:

```sh
uv run dch-nlsql create-connection --name nlsql-demo-postgres \
  --connection-type Postgres --credentials-secret YOUR_SECRET_NAME
```

The Secret must contain `URI` and, when required, `CA_CERT`. This mode skips inline credential validation and checks the saved connection's readiness. The DCH services need appropriate tenant Secret permissions.

For either mode, the source user needs access to the demo tables. If you use an existing read-only role, the database owner can grant access in a SQL console:

```sql
GRANT USAGE ON SCHEMA nlsql_demo TO your_existing_dch_role;
GRANT SELECT ON ALL TABLES IN SCHEMA nlsql_demo TO your_existing_dch_role;
```

The demo seed does not create roles or grant permissions automatically.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| DCH TLS verification fails | Configure `DCH_CA_CERT` with the trusted gateway CA. Use the gateway hostname matching its certificate. |
| DCH authentication or tenant access fails | Refresh `DCH_TOKEN` if expired and confirm access to `DCH_TENANT_ID`. |
| Readiness returns HTTP 502 / `connection_check_failed` | Check source connectivity and Flight's access to the saved credential Secret. See below. |
| Schema discovery fails while the connection is marked ready | Readiness is saved REST status; a live Flight check can still fail. Retry database checks for an interruption or unavailable service. For persistent errors, check the reported Flight error category, gateway routing, and Flight logs. Reuse the saved connection. |
| Demo tables are missing | Load the seed into the same database selected by the DCH connection, then retry setup. |
| Model returns HTTP 401 or 403 | Verify the real model API key and model permissions. A gateway may reject auth before checking the URL path. |
| Model returns HTTP 404 | Check the API base URL, gateway prefix, and exact discovered model ID. |
| Model TLS verification fails | Configure `SSL_CERT_FILE` with a trusted model CA bundle. |
| SQL fails after correction | Inspect the printed SQL, live `schema` output, and question. Clarify ambiguous business definitions in the question or supply optional context. |
| Changing `.env` has no effect | Update or unset the corresponding exported shell variable. |

### DCH gateway certificate

For a gateway using the cluster's default ingress certificate, retrieve its public trust bundle through your authenticated OpenShift context:

```sh
mkdir -p certs
oc get configmap default-ingress-cert -n openshift-config-managed \
  -o jsonpath='{.data.ca-bundle\.crt}' > certs/dch-ingress-ca.pem
```

Set `DCH_CA_CERT` to the file's absolute path and rerun setup. A custom gateway certificate may require a different CA from its administrator. TLS verification remains enabled. Local `.pem` files are ignored by Git.

### Credential Secret access

Flight may report `Secret not found: Failed to obtain credentials` even when the Secret exists. An administrator can check its actual service account against that exact Secret:

```sh
oc auth can-i get secret/YOUR_CREDENTIAL_SECRET -n YOUR_TENANT \
  --as=system:serviceaccount:DCH_SERVICE_NAMESPACE:FLIGHT_SERVICE_ACCOUNT
```

If access is denied, review and apply the tenant-specific Role and RoleBinding commands printed by the wizard. The Flight grant is scoped to the selected connection's Secret. A successful inline credential test does not exercise Flight's ability to read a saved Secret.
