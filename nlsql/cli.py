import argparse
import json
import os
import sys
from pathlib import Path

import pyarrow as pa
from data_connect_hub import (
    DataConnectClient,
    DCHError,
)
from dotenv import load_dotenv
from openai import APIError, OpenAI
from sqlglot.errors import SqlglotError

from .connections import create_postgres_connection
from .diagnostics import error_hint
from .model import model_auth_options
from .schema import discover_tables
from .service import run_sql
from .setup import run_setup
from .validation import validate_sql
from .workflow import QueryContext, run_question


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in your environment or .env")
    return value


def build_parser():
    parser = argparse.ArgumentParser(description="Natural-language SQL through Data Connect Hub")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "setup", help="Interactive setup: DCH, connection, demo checks, and end-to-end query"
    )
    commands.add_parser("connections", help="List connections visible to the DCH token and tenant")
    commands.add_parser("connection-types", help="List DCH connection types and providers")
    create = commands.add_parser("create-connection", help="Register a PostgreSQL database in DCH")
    create.add_argument("--name", default="nlsql-demo-postgres", help="Connection display name")
    type_choice = create.add_mutually_exclusive_group()
    type_choice.add_argument(
        "--connection-type",
        help="Type name to look up; defaults to DCH_CONNECTION_TYPE_NAME or Postgres",
    )
    type_choice.add_argument("--connection-type-id", help="Select a type by ID instead of name")
    create.add_argument(
        "--credentials-secret", help="Use an existing tenant Secret instead of POSTGRES_URI"
    )
    for name in ("schema", "ask", "query"):
        command = commands.add_parser(name)
        command.add_argument("--connection-id", help="Defaults to DCH_CONNECTION_ID")
        command.add_argument(
            "--table", action="append", help="Exact schema.table label; repeatable"
        )
        if name != "schema":
            command.add_argument(
                "text", help="Natural-language question" if name == "ask" else "SQL"
            )
            command.add_argument(
                "--dialect",
                default=os.getenv("NLSQL_DIALECT", "postgres"),
                choices=["postgres", "sqlite", "duckdb"],
            )
            command.add_argument("--max-rows", type=int, default=os.getenv("NLSQL_MAX_ROWS", "100"))
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="Discover schema and validate; skip execution",
            )
        if name == "ask":
            command.add_argument(
                "--max-corrections",
                type=int,
                choices=[0, 1],
                default=1,
                help="SQL correction attempts after a recognized source query error (default: 1)",
            )
            command.add_argument(
                "--context", type=Path, help="Optional business descriptions (UTF-8 text)"
            )
    return parser


def main(argv=None):
    load_dotenv(Path.cwd() / ".env", override=False)
    args = build_parser().parse_args(argv)
    if args.command == "setup":
        return run_setup()
    try:
        if args.command in {"ask", "query"} and not 1 <= args.max_rows <= 10000:
            raise ValueError("--max-rows must be between 1 and 10000")
        with DataConnectClient(
            endpoint=required("DCH_HOST"),
            token=required("DCH_TOKEN"),
            tenant_id=required("DCH_TENANT_ID"),
            ca_cert=os.getenv("DCH_CA_CERT") or None,
            flight_timeout=float(os.getenv("DCH_FLIGHT_TIMEOUT", "30")),
        ) as dch:
            if args.command == "connection-types":
                print(
                    json.dumps(
                        [
                            {
                                "id": t.id,
                                "name": t.name,
                                "provider": t.provider,
                                "flight_ready": t.status.flight_ready,
                            }
                            for t in dch.list_connection_types()
                        ],
                        indent=2,
                    )
                )
                return 0
            if args.command == "create-connection":
                type_name = args.connection_type
                if args.connection_type_id is None and type_name is None:
                    type_name = os.getenv("DCH_CONNECTION_TYPE_NAME", "Postgres")
                uri, ca_pem = None, None
                if not args.credentials_secret:
                    uri = required("POSTGRES_URI")
                    if os.getenv("POSTGRES_CA_CERT"):
                        ca_pem = Path(os.environ["POSTGRES_CA_CERT"]).read_text(encoding="utf-8")
                connection = create_postgres_connection(
                    dch,
                    name=args.name,
                    connection_type_name=type_name,
                    connection_type_id=args.connection_type_id,
                    uri=uri,
                    existing_secret=args.credentials_secret,
                    ca_pem=ca_pem,
                )
                # Print recovery information before checking readiness: creation has persisted.
                print(f"Created DCH connection: {connection.id}", flush=True)
                print(f"DCH_CONNECTION_ID={connection.id}", flush=True)
                print(
                    "Save this ID in .env. The connection and credential Secret remain in DCH.",
                    flush=True,
                )
                dch.check_connection_readiness(connection.id)
                saved = dch.get_connection(connection.id)
                print(f"Readiness: {saved.status.state}")
                if saved.status.state != "ready":
                    raise ValueError(
                        "Connection was created but is not ready. Check source access, "
                        "credential Secret permissions, and DCH service logs"
                    )
                return 0
            if args.command == "connections":
                print(
                    json.dumps(
                        [
                            {
                                "id": c.id,
                                "name": c.name,
                                "format": c.format,
                                "state": c.status.state,
                            }
                            for c in dch.list_connections()
                        ],
                        indent=2,
                    )
                )
                return 0
            connection_id = args.connection_id or required("DCH_CONNECTION_ID")
            if args.command == "schema":
                tables = discover_tables(dch, connection_id, args.table)
                print(json.dumps([t.prompt_dict() for t in tables], indent=2))
                return 0
            if args.command == "ask":
                with OpenAI(
                    base_url=required("LLM_BASE_URL"),
                    **model_auth_options(os.getenv("LLM_API_KEY", "")),
                    timeout=float(os.getenv("LLM_TIMEOUT", "60")),
                    max_retries=0,
                ) as llm:
                    outcome = run_question(
                        args.text,
                        QueryContext(
                            dch=dch,
                            llm=llm,
                            model=required("LLM_MODEL"),
                            connection_id=connection_id,
                            dialect=args.dialect,
                            max_rows=args.max_rows,
                            selected_tables=tuple(args.table or ()),
                            business_context=args.context.read_text(encoding="utf-8")
                            if args.context
                            else "",
                            dry_run=args.dry_run,
                            max_corrections=args.max_corrections,
                        ),
                    )
                sql = outcome["sql"]
                frame = outcome.get("result")
                if outcome["attempts"] > 1:
                    print("SQL corrected once after a source query error.", flush=True)
            else:
                tables = discover_tables(dch, connection_id, args.table)
                sql = validate_sql(args.text, tables, args.dialect, args.max_rows)
                if not args.dry_run:
                    _, frame = run_sql(dch, connection_id, sql, tables, args.dialect, args.max_rows)
            print(f"SQL ({connection_id}):\n{sql}", flush=True)
            if not args.dry_run:
                print(frame.to_string(index=False))
                print(f"\n{len(frame)} row(s); result limit {args.max_rows}.")
        return 0
    except (DCHError, APIError, OSError, ValueError, SqlglotError, pa.ArrowException) as exc:
        print(error_hint(exc), file=sys.stderr)
        return 1
