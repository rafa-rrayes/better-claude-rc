"""Apply pending platform migrations (plain SQL, filename order).

Usage:
    uv run python platform/migrations/migrate.py [dsn]

The DSN defaults to PLATFORM_DATABASE_URL / DATABASE_URL. Applying is
idempotent — each file runs once, tracked in schema_migrations.
"""

from __future__ import annotations

import asyncio
import os
import sys


async def main() -> None:
    import asyncpg

    from app.db import apply_migrations

    dsn = (sys.argv[1] if len(sys.argv) > 1 else None) \
        or os.environ.get("PLATFORM_DATABASE_URL") \
        or os.environ.get("DATABASE_URL")
    if not dsn:
        print("usage: migrate.py <dsn>  (or set PLATFORM_DATABASE_URL)")
        sys.exit(2)
    conn = await asyncpg.connect(dsn)
    try:
        applied = await apply_migrations(conn)
    finally:
        await conn.close()
    print(f"applied: {applied or 'nothing (up to date)'}")


if __name__ == "__main__":
    asyncio.run(main())
