"""Push the deployment secrets from `api/.env` to Fly, without any of them being displayed.

`fly secrets set K=V ...` on a command line puts every value into shell history, into the
terminal scrollback, and into a screenshot if anybody takes one. This reads the local `.env`,
sends the values to `fly secrets import` over stdin, and prints only the key names.

Deliberately not everything in `.env` goes:

  * `DATABASE_URL_DIRECT` is `hazir_owner`, which has BYPASSRLS. The API never needs it —
    migrations are run from a laptop — and a credential that skips every RLS policy has no
    business sitting on a public host. See README.md "Deploy".
  * `TEST_DATABASE_URL*` point at the branch the suite truncates.
  * `APP_DB_PASSWORD` is only used by the rotation script.

Usage:
    .venv/Scripts/python scripts/fly_secrets.py --web-url https://haazir-web.vercel.app
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"

# What the API needs in production, and nothing more.
REQUIRED = ["DATABASE_URL", "JWT_SECRET"]
OPTIONAL = [
    "GEMINI_API_KEY",  # explanations; templates without it
    "RESEND_API_KEY",  # magic links. In prod there is no dev link, so this is the only way in
    "MAIL_FROM",
    "ADMIN_EMAILS",
    "SENTRY_DSN",
    "LLM_MONTHLY_CEILING_USD",
    "OSRM_URL",
]


def read_env() -> dict[str, str]:
    if not ENV_FILE.exists():
        sys.exit(f"no {ENV_FILE}")
    out: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        if v:
            out[k.strip()] = v
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--web-url",
        required=True,
        help="Where the frontend is deployed. Sets CORS_ORIGINS and WEB_BASE_URL, which the "
        "magic-link email uses — get it wrong and every link points at localhost.",
    )
    ap.add_argument("--app", default="haazir-api")
    ap.add_argument("--dry-run", action="store_true", help="List the keys and send nothing.")
    args = ap.parse_args()

    env = read_env()
    web = args.web_url.rstrip("/")

    payload: dict[str, str] = {}
    missing: list[str] = []
    for key in REQUIRED:
        if key in env:
            payload[key] = env[key]
        else:
            missing.append(key)
    for key in OPTIONAL:
        if key in env:
            payload[key] = env[key]

    if missing:
        sys.exit(f"missing from .env and required: {', '.join(missing)}")

    # Derived from the deployment, not copied from a local file where they mean localhost.
    payload["CORS_ORIGINS"] = web
    payload["WEB_BASE_URL"] = web

    print(f"app {args.app}, frontend {web}")
    print(f"{len(payload)} secrets:")
    for k in sorted(payload):
        print(f"  {k}")
    for k in OPTIONAL:
        if k not in payload:
            print(f"  ({k} not set locally — skipped)")

    if args.dry_run:
        print("\ndry run, nothing sent")
        return 0

    if not shutil.which("fly") and not shutil.which("flyctl"):
        sys.exit("\nfly CLI not found. Install it and `fly auth login` first.")

    fly = shutil.which("fly") or shutil.which("flyctl")
    body = "\n".join(f"{k}={v}" for k, v in payload.items())
    # `import` reads KEY=VALUE from stdin, so no value ever appears in a process argument.
    result = subprocess.run(
        [fly, "secrets", "import", "--app", args.app],
        input=body,
        text=True,
    )
    if result.returncode == 0:
        print("\nsent. `fly secrets list` shows names and digests, never values.")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
