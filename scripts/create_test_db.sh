#!/usr/bin/env sh
# Create the test database if it does not exist (used by CI).
# Connection coordinates come from TEST_DB_ADMIN_* vars; adjust as needed.
set -eu

: "${TEST_DB_HOST:=localhost}"
: "${TEST_DB_PORT:=5432}"
: "${TEST_DB_USER:=ioka}"
: "${TEST_DB_PASSWORD:=ioka}"
: "${TEST_DB_ADMIN_NAME:=ioka_travel}"
: "${TEST_DB_NAME:=ioka_travel_test}"

DB_HOST="$TEST_DB_HOST" DB_PORT="$TEST_DB_PORT" DB_USER="$TEST_DB_USER" \
DB_PASSWORD="$TEST_DB_PASSWORD" DB_ADMIN="$TEST_DB_ADMIN_NAME" DB_NAME="$TEST_DB_NAME" \
python - <<'EOF'
import asyncio
import os

import asyncpg


async def main() -> None:
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_ADMIN"],
    )
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_database WHERE datname = $1", os.environ["DB_NAME"]
    )
    if not exists:
        await conn.execute(f'CREATE DATABASE "{os.environ["DB_NAME"]}"')
    await conn.close()
    print(f"test db ready: {os.environ['DB_NAME']}")


asyncio.run(main())
EOF
