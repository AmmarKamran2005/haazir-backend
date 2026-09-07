"""Rotate the `haazir_app` password on every branch named in `api/.env`, and rewrite the file.

    api/.venv/Scripts/python scripts/rotate_app_password.py

Why this exists as a script rather than a README paragraph: the password lives in four places
that must agree (the role on the production branch, the role on the test branch,
`DATABASE_URL`, `TEST_DATABASE_URL`) plus `APP_DB_PASSWORD` itself, and editing connection
strings by hand is how a colon ends up in the wrong place at eleven at night. Run this after
any credential has been exposed, on a schedule if you like, and after creating a new branch
that was forked before migration 0012 ran.

Existing connections survive a password change; only new ones need the new value. So do not
run this while the test suite is mid-run, because its pool recycles connections every 280
seconds and the recycled ones will fail to authenticate.

Nothing here prints a secret.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import secrets
import sys
from urllib.parse import quote

from sqlalchemy import text

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from haazir.config import _to_asyncpg  # noqa: E402
from haazir.db import make_engine  # noqa: E402

ENV = ROOT / ".env"
APP_ROLE = "haazir_app"


def _read_env() -> tuple[list[str], dict[str, str]]:
    lines = ENV.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip()
    return lines, values


def _with_userinfo(url: str, user: str, password: str) -> str:
    return re.sub(r"^(\w+)://[^@]+@", rf"\1://{user}:{quote(password, safe='')}@", url)


async def _set_password(owner_url: str, password: str) -> str:
    # Same engine factory as the app: pool settings, SSL decision and the statement-cache
    # workaround for PgBouncer all come from one place.
    engine = make_engine(_to_asyncpg(owner_url))
    try:
        async with engine.begin() as conn:
            # Utility statements take no bind parameters; Postgres quotes the literal itself.
            stmt = await conn.scalar(
                text(f"SELECT format('ALTER ROLE {APP_ROLE} PASSWORD %L', CAST(:pw AS text))"),
                {"pw": password},
            )
            await conn.exec_driver_sql(stmt)
            host = await conn.scalar(text("SELECT inet_server_addr()::text"))
        return host or "?"
    finally:
        await engine.dispose()


async def main() -> int:
    lines, values = _read_env()
    targets = [
        ("DATABASE_URL", "DATABASE_URL_DIRECT"),
        ("TEST_DATABASE_URL", "TEST_DATABASE_URL_DIRECT"),
    ]
    targets = [(app, owner) for app, owner in targets if values.get(owner)]
    if not targets:
        print("no *_DIRECT owner URLs in api/.env; nothing to rotate", file=sys.stderr)
        return 1

    new_password = secrets.token_urlsafe(24)

    for _app_key, owner_key in targets:
        await _set_password(values[owner_key], new_password)
        branch_host = re.sub(r".*@([^/]+)/.*", r"\1", values[owner_key]).split(".")[0]
        print(f"rotated {APP_ROLE} on {branch_host}")

    out = []
    for line in lines:
        k, _, v = line.partition("=")
        k = k.strip()
        if k == "APP_DB_PASSWORD":
            out.append(f"APP_DB_PASSWORD={new_password}")
        elif k in {app for app, _ in targets} and v.strip():
            out.append(f"{k}={_with_userinfo(v.strip(), APP_ROLE, new_password)}")
        else:
            out.append(line)
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("api/.env rewritten: APP_DB_PASSWORD and the app connection strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
