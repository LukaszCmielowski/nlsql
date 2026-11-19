#!/usr/bin/env bash
# Forward standard libpq connection arguments to psql; never print credentials.
set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    cat <<'HELP'
Create the nlsql_demo schema and seed three related tables in an existing PostgreSQL database.

Usage:
  bash scripts/setup_demo_db.sh -h HOST -p 5432 -U USER -d DATABASE

Accepts standard psql connection arguments and PGHOST, PGPORT, PGUSER,
PGDATABASE, PGPASSFILE, and PGSSLMODE environment variables. psql prompts for
a password when required. The database user needs permission to create the
schema and tables. The seed runs in one transaction and preserves existing
rows with matching primary keys when rerun.
HELP
    exit 0
fi

if ! command -v psql >/dev/null 2>&1; then
    echo "psql is required. Alternatively, run examples/demo_db.sql in your PostgreSQL client's SQL editor." >&2
    exit 1
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec psql "$@" -X --set=ON_ERROR_STOP=1 --file="$script_dir/../examples/demo_db.sql"
