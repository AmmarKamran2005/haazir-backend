"""Reset the Neon owner role's password on every branch in `api/.env`, and rewrite the file.

    api/.venv/Scripts/python scripts/rotate_owner_password.py

The owner role (`hazir_owner`) is Neon-managed: Neon stores its password so the console and
`neon connection-string` can show it. Changing it with `ALTER ROLE` in SQL would work for
connections and silently desynchronise everything Neon displays, so the reset goes through
Neon's API instead, via the CLI's `neon api` passthrough. Neon generates the new password and
returns it once; this script reads it from the JSON response and writes it straight into the
`*_DIRECT` URLs without ever printing it.

Run this after the owner credential has been exposed anywhere: a terminal, a log, a
screenshot. Then re-pull the root `.env.local` (`neon link ... -y`), which still holds the
old value.

Requires the `neon` CLI to be installed and logged in. Set NEON_ORG_ID if the CLI would
otherwise prompt for an organisation.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
PROJECT_ID = os.environ.get("NEON_PROJECT_ID", "morning-wind-72754130")
OWNER_ROLE = os.environ.get("NEON_OWNER_ROLE", "hazir_owner")


def _neon() -> str:
    exe = shutil.which("neon") or shutil.which("neon.cmd")
    if not exe:
        fallback = pathlib.Path.home() / "AppData" / "Roaming" / "npm" / "neon.cmd"
        if fallback.exists():
            return str(fallback)
        sys.exit("neon CLI not found on PATH. npm i -g neon@latest && neon login")
    return exe


def _api(path: str, method: str = "GET") -> dict:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    out = subprocess.run(
        [_neon(), "api", path, "-X", method, "-o", "json"],
        capture_output=True, text=True, env=env, check=False,
    )
    if out.returncode != 0:
        sys.exit(f"neon api {method} {path} failed:\n{out.stderr.strip()[:400]}")
    return json.loads(out.stdout)


def _branch_id_for_host(host: str) -> str:
    """Map an endpoint host like `ep-late-tree-b3ww0ey6.c-4...` back to its branch id."""
    endpoint_id = host.split(".")[0].removesuffix("-pooler")
    for ep in _api(f"/projects/{PROJECT_ID}/endpoints").get("endpoints", []):
        if ep.get("id") == endpoint_id:
            return ep["branch_id"]
    sys.exit(f"no endpoint {endpoint_id!r} in project {PROJECT_ID}")


def _with_userinfo(url: str, user: str, password: str) -> str:
    return re.sub(r"^(\w+)://[^@]+@", rf"\1://{user}:{quote(password, safe='')}@", url)


def main() -> int:
    lines = ENV.read_text(encoding="utf-8").splitlines()
    values = {
        k.strip(): v.strip()
        for k, _, v in (line.partition("=") for line in lines if line and not line.startswith("#"))
        if k.strip()
    }
    direct_keys = [k for k in ("DATABASE_URL_DIRECT", "TEST_DATABASE_URL_DIRECT") if values.get(k)]
    if not direct_keys:
        sys.exit("no *_DIRECT URLs in api/.env; nothing to rotate")

    new_passwords: dict[str, str] = {}
    for key in direct_keys:
        host = urlsplit(values[key]).hostname or ""
        branch_id = _branch_id_for_host(host)
        resp = _api(
            f"/projects/{PROJECT_ID}/branches/{branch_id}/roles/{OWNER_ROLE}/reset_password",
            "POST",
        )
        password = (resp.get("role") or {}).get("password")
        if not password:
            sys.exit(f"reset on {branch_id} returned no password; response keys: {list(resp)}")
        new_passwords[key] = password
        print(f"reset {OWNER_ROLE} on branch {branch_id} ({host.split('.')[0]})")

    out = []
    for line in lines:
        k = line.partition("=")[0].strip()
        if k in new_passwords:
            out.append(f"{k}={_with_userinfo(values[k], OWNER_ROLE, new_passwords[k])}")
        else:
            out.append(line)
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("api/.env rewritten: owner connection strings")
    print("next: cd .. && neon link --project-id", PROJECT_ID, "--branch production -y   (refreshes .env.local)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
