"""Interactive, resumable setup for the DCH PostgreSQL demo."""

import getpass
import hashlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from data_connect_hub import (
    DCHConnectionError,
    DCHError,
    DCHNotFoundError,
)
from openai import APIError, OpenAI

from .connections import create_postgres_connection
from .dch import DataConnectClient
from .diagnostics import (
    connection_error_hint,
    dch_error_code,
    error_hint,
)
from .model import available_models, model_auth_options
from .output import print_query_output, print_table, query_progress
from .schema import discover_tables
from .service import run_sql
from .setup_ui import Settings, certificate_file, prompt_value, step, yes
from .workflow import QueryContext, run_question

DEMO_TABLES = ("nlsql_demo.customers", "nlsql_demo.orders", "nlsql_demo.order_items")
DEFAULT_QUESTION = "What is total completed revenue?"


def oc_value(*args: str) -> str:
    if not shutil.which("oc"):
        return ""
    try:
        result = subprocess.run(
            ["oc", *args], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def access_help(tenant: str, secret: str | None = None):
    """Print reviewable admin commands, discovering public service-account names if possible."""
    rows = oc_value(
        "get",
        "deployments",
        "-A",
        "-l",
        "app.kubernetes.io/part-of=data-connect-hub",
        "-o",
        'jsonpath={range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}{.spec.template.spec.serviceAccountName}{"\\n"}{end}',
    )
    deployments = [line.split() for line in rows.splitlines() if len(line.split()) == 3]
    rest = [row for row in deployments if "rest" in row[1]]
    flight = [row for row in deployments if "flight" in row[1]]
    rest_sa = (
        f"{rest[0][0]}:{rest[0][2]}" if len(rest) == 1 else "DCH_NAMESPACE:REST_SERVICE_ACCOUNT"
    )
    flight_sa = (
        f"{flight[0][0]}:{flight[0][2]}"
        if len(flight) == 1
        else "DCH_NAMESPACE:FLIGHT_SERVICE_ACCOUNT"
    )
    print(
        "\nFor credential Secret access problems, an OpenShift administrator can review these commands."
    )
    if len(rest) != 1 or len(flight) != 1:
        print("Replace the service-account placeholders using:")
        print(
            "oc get deployments -A -l app.kubernetes.io/part-of=data-connect-hub "
            "-o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,SA:.spec.template.spec.serviceAccountName"
        )
    ns = shlex.quote(tenant)
    print(
        f"oc create rolebinding nlsql-rest-secret-access -n {ns} "
        f"--clusterrole=dch-rest-service-secret-access --serviceaccount={shlex.quote(rest_sa)} "
        "--dry-run=client -o yaml | oc apply -f -"
    )
    if secret:
        # A distinct role for each Secret avoids replacing another connection's grant.
        role = "nlsql-flight-" + hashlib.sha256(secret.encode()).hexdigest()[:12]
        print(
            f"oc create role {role} -n {ns} --verb=get --resource=secrets "
            f"--resource-name={shlex.quote(secret)} --dry-run=client -o yaml | oc apply -f -"
        )
        print(
            f"oc create rolebinding {role} -n {ns} --role={role} "
            f"--serviceaccount={shlex.quote(flight_sa)} --dry-run=client -o yaml | oc apply -f -"
        )
    else:
        print(
            "After the connection is created, rerun setup to get Flight commands scoped to its Secret."
        )
    print(
        "Reference: https://github.com/opendatahub-io/data-connect-hub/blob/main/docs/user-guide/auth.md"
    )


def configure_gateway(settings: Settings):
    step(1, "Connect to DCH")
    if settings.reuse(
        {
            "DCH_HOST": "Gateway",
            "DCH_TENANT_ID": "Tenant namespace",
            "DCH_TOKEN": "Bearer token",
            "DCH_CA_CERT": "Gateway CA certificate",
        },
        required=("DCH_HOST", "DCH_TENANT_ID", "DCH_TOKEN"),
        secrets=("DCH_TOKEN",),
        files=("DCH_CA_CERT",),
    ):
        return
    if (
        (not settings.get("DCH_HOST") or not settings.get("DCH_TOKEN"))
        and shutil.which("oc")
        and yes("Use your current oc login for missing gateway/token defaults?", True)
    ):
        if not settings.get("DCH_HOST"):
            host = oc_value(
                "get",
                "route",
                "data-science-gateway",
                "-n",
                "openshift-ingress",
                "-o",
                "jsonpath={.spec.host}",
            )
            if host:
                settings.save("DCH_HOST", host + ":443")
        if not settings.get("DCH_TOKEN"):
            token = oc_value("whoami", "-t")
            if token:
                settings.save("DCH_TOKEN", token)
    settings.prompt("DCH_HOST", "DCH gateway host:port")
    settings.prompt("DCH_TENANT_ID", "Tenant namespace")
    settings.prompt("DCH_TOKEN", "DCH bearer token", secret=True)
    settings.prompt(
        "DCH_CA_CERT", "Gateway CA certificate file", optional=True, validate=certificate_file
    )


def postgres_uri(settings: Settings) -> str:
    uri = settings.prompt(
        "POSTGRES_URI",
        "PostgreSQL URI (hidden; '-' to build from fields)",
        secret=True,
        optional=True,
    )
    if uri:
        return uri
    host = prompt_value("PostgreSQL host (without port)")
    port = prompt_value("PostgreSQL port", default="5432", validate=postgres_port)
    database = prompt_value("Database name")
    username = prompt_value("PostgreSQL username")
    password = getpass.getpass("PostgreSQL password (optional; Enter for no password): ")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    sslmode = prompt_value("PostgreSQL SSL mode", default="require", validate=postgres_sslmode)
    uri = (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{quote(database, safe='')}?sslmode={sslmode}"
    )
    settings.save("POSTGRES_URI", uri)
    return uri


def postgres_port(value: str) -> str:
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError("Enter a port between 1 and 65535.")
    return value


def postgres_sslmode(value: str) -> str:
    if value not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("Choose disable, allow, prefer, require, verify-ca, or verify-full.")
    return value


def choose_connection(client, settings: Settings):
    step(2, "Choose a PostgreSQL connection")
    connections = client.list_connections()
    kinds = {kind.id: kind for kind in client.list_connection_types()}
    connections = [
        c
        for c in connections
        if c.format == "tabular"
        and c.data_connection_type_id in kinds
        and kinds[c.data_connection_type_id].provider == "postgres"
    ]
    saved = settings.get("DCH_CONNECTION_ID")
    for number, connection in enumerate(connections, 1):
        marker = "; saved" if connection.id == saved else ""
        print(f"  {number}. {connection.name} [{connection.status.state}{marker}]")
        print(f"     {connection.id}")
    if not connections:
        print("No PostgreSQL connections found. Press Enter to create one.")
    default = next((str(i) for i, c in enumerate(connections, 1) if c.id == saved), "new")
    while True:
        choice = input(f"Choose number, connection ID, or 'new' [{default}]: ").strip() or default
        if choice.lower() == "new":
            connection = create_demo_connection(client, settings, connections)
        else:
            if choice.isdigit():
                if not 1 <= int(choice) <= len(connections):
                    print("Choose a listed number, paste a connection ID, or enter 'new'.")
                    continue
                choice = connections[int(choice) - 1].id
            try:
                connection = client.get_connection(choice)
            except DCHNotFoundError:
                print("Connection not found in this tenant. Choose again.")
                continue
        kind = kinds.get(connection.data_connection_type_id)
        if kind is None:
            kind = client.get_connection_type(connection.data_connection_type_id)
        if connection.format == "tabular" and kind.provider == "postgres":
            break
        print("This demo requires a PostgreSQL connection. Choose again.")
    settings.save("DCH_CONNECTION_ID", connection.id)
    settings.save("NLSQL_DIALECT", "postgres")
    print(f"Using {connection.name}; saved DCH_CONNECTION_ID={connection.id}")
    return connection


def create_demo_connection(client, settings: Settings, connections):
    name = prompt_value("New connection name", default="nlsql-demo-postgres")
    # Recover creation interrupted before its ID was saved.
    matches = [c for c in connections if c.name == name]
    if len(matches) > 1:
        raise ValueError("Several connections share that name. Select one by ID.")
    if matches:
        print("Reusing the existing connection with that name.")
        return matches[0]
    type_name = settings.prompt(
        "DCH_CONNECTION_TYPE_NAME", "Connection type name", default="Postgres"
    )
    secret = prompt_value(
        "Existing credential Secret name (empty to enter database credentials)", optional=True
    )
    uri, ca_pem = None, None
    if not secret:
        uri = postgres_uri(settings)
        ca = settings.prompt(
            "POSTGRES_CA_CERT",
            "PostgreSQL CA certificate file",
            optional=True,
            validate=certificate_file,
        )
        ca_pem = Path(ca).read_text(encoding="utf-8") if ca else None
    print("Registering the PostgreSQL connection with DCH...")
    try:
        return create_postgres_connection(
            client,
            name=name,
            connection_type_name=type_name,
            uri=uri,
            existing_secret=secret or None,
            ca_pem=ca_pem,
        )
    except ValueError as exc:
        if "cannot_create_secret" in str(exc):
            access_help(settings.get("DCH_TENANT_ID"))
        raise


def verify_tables(client, connection, settings: Settings):
    step(3, "Check database access and demo tables")
    while True:
        stage = "Connection readiness"
        try:
            if connection.status.state != "ready":
                print("Checking saved connection readiness...")
                client.check_connection_readiness(connection.id)
            stage = "Schema discovery"
            print("Discovering demo tables through DCH Flight...")
            tables = discover_tables(client, connection.id, list(DEMO_TABLES))
            stage = "SQL execution"
            print("Running the read-only customer count through DCH...")
            _, frame = run_sql(
                client,
                connection.id,
                "SELECT COUNT(*) AS customers FROM nlsql_demo.customers",
                tables,
                "postgres",
                100,
            )
            print("DCH discovered: " + ", ".join(t.label for t in tables))
            print_table(frame)
            print("Database checks passed. An unmodified demo has 6 customers.")
            return True
        except (DCHError, ValueError) as exc:
            print(f"{stage} failed.")
            print(error_hint(exc))
            if isinstance(exc, ValueError) and "Tables absent" in str(exc):
                print("Load examples/demo_db.sql in your PostgreSQL SQL editor, or run:")
                print("bash scripts/setup_demo_db.sh -h POSTGRES_HOST -p 5432 -U USER -d DATABASE")
                print(
                    "The seed needs a database user allowed to create the demo schema and tables."
                )
            elif dch_error_code(exc) in {"invalid_secret", "connection_check_failed"} or (
                isinstance(exc, DCHConnectionError)
                and "credential Secret" in connection_error_hint(exc)
            ):
                access_help(settings.get("DCH_TENANT_ID"), connection.credentials_ref.secret)
            if not yes("Retry the database checks now?"):
                return False


def choose_model(client, settings: Settings):
    print("Discovering available model IDs...")
    models = available_models(client)
    if models is None:
        print("This endpoint does not allow model listing; enter the exact deployed model ID.")
        return settings.prompt("LLM_MODEL", "Deployed model ID")
    if not models:
        raise ValueError(
            "No ready models were returned for this API key. Check MaaS model access and readiness."
        )
    print("Available model IDs (choose a text-generation model):")
    for number, model in enumerate(models, 1):
        print(f"  {number}. {model}")
    saved = settings.get("LLM_MODEL")
    default = str(models.index(saved) + 1) if saved in models else "1" if len(models) == 1 else ""
    if saved and saved not in models:
        print("The saved model ID is not listed; select its exact ID below.")
    while True:
        answer = (
            input(f"Choose model number or exact ID{f' [{default}]' if default else ''}: ").strip()
            or default
        )
        if answer.isdigit() and 1 <= int(answer) <= len(models):
            answer = models[int(answer) - 1]
        if answer in models:
            settings.save("LLM_MODEL", answer)
            return answer
        print("Choose a listed number or exact model ID.")


def configure_model(settings: Settings) -> bool:
    step(4, "Connect a language model")
    if not yes("Configure the model and run an end-to-end question now?", True):
        print("DCH SQL access verified. Model setup and the end-to-end question were skipped.")
        return False
    if settings.reuse(
        {
            "LLM_BASE_URL": "API base URL",
            "LLM_MODEL": "Model ID",
            "LLM_API_KEY": "API key",
            "SSL_CERT_FILE": "Model CA certificate",
        },
        required=("LLM_BASE_URL",),
        secrets=("LLM_API_KEY",),
        files=("SSL_CERT_FILE",),
    ):
        return True
    print("Use the API root, including /v1 and any gateway path prefix required by your endpoint.")
    print("For MaaS, use its API base URL and a real MaaS API key.")
    settings.prompt("LLM_BASE_URL", "Model API base URL")
    settings.prompt(
        "LLM_API_KEY",
        "Model API key (empty for an unauthenticated endpoint)",
        secret=True,
        optional=True,
    )
    settings.prompt(
        "SSL_CERT_FILE", "Model CA bundle file", optional=True, validate=certificate_file
    )
    return True


def run_demo_question(client, connection, settings: Settings):
    context_file = Path(__file__).resolve().parent.parent / "examples/demo_context.txt"
    context = (
        context_file.read_text(encoding="utf-8")
        if context_file.is_file()
        else (
            "Revenue is SUM(quantity * unit_price) from completed orders only; all amounts are USD. "
            "customers.customer_id = orders.customer_id; orders.order_id = order_items.order_id."
        )
    )
    with OpenAI(
        base_url=settings.get("LLM_BASE_URL"),
        **model_auth_options(settings.get("LLM_API_KEY")),
        timeout=float(settings.get("LLM_TIMEOUT", "60")),
        max_retries=0,
    ) as llm:
        choose_model(llm, settings)
        step(5, "Ask a question")
        print("Using the included demo business definitions for this setup check.")
        print("Try the default question, or ask 'Which three customers spent the most?'.")
        question = prompt_value("Question", default=DEFAULT_QUESTION)
        with query_progress() as progress:
            result = run_question(
                question,
                QueryContext(
                    dch=client,
                    llm=llm,
                    model=settings.get("LLM_MODEL"),
                    connection_id=connection.id,
                    selected_tables=DEMO_TABLES,
                    business_context=context,
                    on_progress=progress,
                ),
            )
    print()
    print_query_output(
        result["sql"],
        result["result"],
        connection_id=connection.id,
        question=question,
        corrected=result.get("attempts", 1) > 1,
    )
    print("\nEnd-to-end query succeeded.")
    if question == DEFAULT_QUESTION:
        print("Reference for unmodified demo data: completed revenue is USD 5510.00.")
    print("\nTry another question:")
    print(
        shlex.join(
            [
                "uv",
                "run",
                "dch-nlsql",
                "ask",
                "Which three customers spent the most on completed orders? Show their total spending.",
            ]
        )
    )


def run_setup(env_file: Path | None = None) -> int:
    if not sys.stdin.isatty():
        print(
            "Run 'uv run dch-nlsql setup' in an interactive terminal so credentials can be entered without echo."
        )
        return 2
    settings = Settings(env_file or Path.cwd() / ".env")
    print("Guided DCH + PostgreSQL + LangGraph setup")
    print(
        f"Progress is saved to {settings.path}; tokens, passwords, and PostgreSQL URIs are hidden."
    )
    print(
        "Enter keeps the displayed default. Optional values can be empty; '-' clears a saved value."
    )
    if settings.overrides:
        print("Shell values override .env: " + ", ".join(settings.overrides))
        print("If you edit these here, also update their shell exports before the next run.")
    try:
        configure_gateway(settings)
        with DataConnectClient(
            endpoint=settings.get("DCH_HOST"),
            token=settings.get("DCH_TOKEN"),
            tenant_id=settings.get("DCH_TENANT_ID"),
            ca_cert=settings.get("DCH_CA_CERT") or None,
            max_retries=0,
            rest_timeout=20,
            flight_timeout=float(settings.get("DCH_FLIGHT_TIMEOUT", "30")),
        ) as client:
            connection = choose_connection(client, settings)
            if not verify_tables(client, connection, settings):
                print("Progress saved. Rerun 'uv run dch-nlsql setup' to resume.")
                return 1
            if not configure_model(settings):
                return 0
            run_demo_question(client, connection, settings)
        return 0
    except (EOFError, KeyboardInterrupt):
        print(
            "\nSetup stopped. Saved settings remain available; rerun setup in an interactive terminal to resume."
        )
        return 130
    except (DCHError, APIError, OSError, ValueError) as exc:
        print(error_hint(exc))
        if isinstance(exc, DCHConnectionError) and "TLS" in connection_error_hint(exc):
            print("For a gateway using the current cluster's default ingress certificate:")
            print("mkdir -p certs")
            print(
                "oc get configmap default-ingress-cert -n openshift-config-managed "
                "-o jsonpath='{.data.ca-bundle\\.crt}' > certs/dch-ingress-ca.pem"
            )
            print("Rerun setup and enter the absolute path of that trusted CA file.")
        print("Progress saved. Correct the failing step and rerun 'uv run dch-nlsql setup'.")
        return 1
