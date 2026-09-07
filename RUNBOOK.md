# HAAZIR API — runbook

What to do when something is wrong, written for whoever is on the laptop at the time.
Companion to [`README.md`](README.md), which covers how to run it normally.

---

## Is it up?

```bash
curl -s https://haazir-api.fly.dev/health
curl -s https://haazir-api.fly.dev/health/db
```

`/health` answers without touching the database, so it stays green through a database blip
and the platform does not kill a process that is working. `/health/db` is the readiness probe
and reports the extension versions.

| `/health/db` says | Meaning | Do |
|---|---|---|
| `status: ok` | Everything reachable | — |
| `status: degraded` | Connected, an extension is missing | The branch never ran migration `0001`. `alembic upgrade head` |
| `status: down` | Cannot connect | See **Database unreachable** |
| `status: unconfigured` | `DATABASE_URL` unset | The secret is missing on the machine |

---

## Database unreachable

Neon suspends compute after five minutes idle and the first connection after that takes a
second or two. A single slow request right after a quiet night is normal and not an incident.

If it persists:

```bash
neon branches list --project-id morning-wind-72754130
fly logs -a haazir-api | grep -i "database\|asyncpg"
```

**Connection pool exhausted.** `pool_size=5, max_overflow=5` per instance. If requests hang
rather than error, check for a long-running transaction holding a connection:

```sql
SELECT pid, state, now() - xact_start AS age, left(query, 80)
  FROM pg_stat_activity
 WHERE datname = current_database() AND state <> 'idle'
 ORDER BY age DESC;
```

An `idle in transaction` backend older than a minute is a bug, not load. Terminate it and
find out which code path left it open:

```sql
SELECT pg_terminate_backend(<pid>);
```

Two signatures seen in development, both from a test run killed mid-flight:

**`LockNotAvailableError` on `TRUNCATE`.** Two backends were left contending on `group_token`.
This is `clean_db`'s ten-second `lock_timeout` working correctly; without it the statement
would have waited indefinitely and looked like a hang.

**A backend `active` for minutes on `SELECT set_config('app.role', ...)`.** That statement is
`apply_claims` and takes microseconds, so any age on it means the client is gone and the
server has not noticed. It holds a pooled connection, and with `pool_size=5, max_overflow=5`
a few of them starve everything else: the visible symptom was a test suite slowing to three
tests a minute and erroring in clusters, which reads like a code bug and is not one. Look for
a *long-running* statement that has no business being long, not only for `idle in transaction`.
Terminating it restored full speed immediately.

---

## The live feed stopped updating

The order to check, cheapest first.

1. **Is the scheduler running?** `refresh_live_state` fires every 60 s and only one instance
   may run it. `fly logs | grep scheduler` should show it starting once. If `RUN_SCHEDULER`
   is true on two machines, both are refreshing and the compute bill is doubling.
2. **Are observations arriving?**
   ```sql
   SELECT source, count(*), max(observed_at)
     FROM observation WHERE observed_at > now() - interval '1 hour' GROUP BY source;
   ```
   Only `prior` rows means no staff console or check-in traffic, which is a product problem
   and not an outage. The estimates are still correct; they are just baselines, and every
   response says so.
3. **Are SSE clients connected?** The hub is in-process. A deploy drops every subscriber, and
   browsers reconnect on their own with `Last-Event-ID`. Nothing to do.
4. **Did a venue's estimate freeze?** Check `live_state.updated_at`. If it is old while
   observations are arriving, the refresh job is erroring on that venue; the log names it.

---

## The LLM budget tripped

Expected behaviour, not an incident. At 90% of `LLM_MONTHLY_CEILING_USD` the product stops
calling Gemini and explanations come from templates. Nothing breaks and no error reaches a
user, and `/v1/search` is unaffected because it never calls a model in the first place.

```bash
curl -s https://haazir-api.fly.dev/v1/llm/status
```

A 503 from Gemini, or a response slower than the 8 s timeout, lands in the same place: the
template. Both are logged at WARNING and neither is an outage. Seeing a run of
`Gemini unreachable, using the template` means the answers are less fluent, not that anything
is broken.

To restore fluency, raise the ceiling and redeploy. The counter is in memory and resets on
deploy, which is deliberate: a ceiling that undercounts after a restart is a smaller problem
than a database round trip on every explanation.

---

## Someone reports a wrong enforcement record

The most serious thing that can go wrong here, because it is a public claim about a named
business.

1. Find it: `GET /v1/admin/ingest/review-queue`, or query `regulatory_event` by
   `raw_venue_name`.
2. **Unpublish immediately** if the match looks wrong. It disappears from every diner-facing
   response the moment `published` is false; RLS does that, not the endpoint.
   ```bash
   curl -X POST .../v1/admin/ingest/review/<id>/decide \
        -H "Authorization: Bearer <admin>" -d '{"publish": false}'
   ```
3. Then work out whether the matcher was wrong or the source was. `match_confidence`,
   `raw_venue_name` and `source_url` are all on the row.
4. The venue always has the right of reply (`regulatory_reply`). There is no endpoint that
   deletes a record, deliberately — but there is no obligation to keep a wrong one published
   while you investigate.

---

## Rolling back

### Code

```bash
fly releases -a haazir-api
fly deploy --image <previous image>
```

### Schema

Migrations are forward-only in practice. Every one has a `downgrade()` and they are tested to
the extent that they run, but a downgrade that drops a column loses its data. Prefer a Neon
branch restore.

### Data — Neon point-in-time restore

Neon keeps history and a branch is a copy-on-write fork, so a restore is a branch and takes
seconds rather than the length of a `pg_restore`.

```bash
# 1. Branch from a moment before the damage
neon branches create --name recover-$(date +%s) \
  --project-id morning-wind-72754130 \
  --parent production --timestamp 2026-09-04T10:00:00Z

# 2. Wait for `current_state: ready`. The create command returns before the branch is usable.
neon branches list --project-id morning-wind-72754130

# 3. --role-name is REQUIRED. Since migration 0012 every branch carries two roles and the CLI
#    will not guess between them: without it you get an empty string, which surfaces later as
#    a misleading "DATABASE_URL is not set".
neon connection-string recover-... --project-id morning-wind-72754130 \
  --role-name hazir_owner --pooled

# 4. Verify before promoting anything
psql "<that string>" -c "SELECT version_num FROM alembic_version;"
```

**Rehearsed 2026-09-04 against the production branch of this project:**

| | |
|---|---|
| Branch created and `ready` | 10 s |
| Connection string, query, schema verified | 20 s total |

The restored branch carried all 32 tables, migration `0013`, the 24 seeded areas, and
`haazir_app` still with `rolbypassrls = false`. The drill branch was deleted afterwards.

Both notes above — the wait, and `--role-name` — came out of the rehearsal failing twice
before it worked. That is the argument for rehearsing it.

---

## Credentials

Two roles, and they are not interchangeable:

| Role | Used by | Has |
|---|---|---|
| `haazir_app` | The API | `NOBYPASSRLS`, DML only |
| `hazir_owner` | Migrations only | Owns the schema, **has `BYPASSRLS`** |

If the API is ever pointed at `hazir_owner`, every row-level security policy in the database
silently stops applying and nothing looks wrong. `test_the_connection_role_cannot_bypass_rls`
exists to catch it.

Rotate after any exposure — a terminal, a log, a screenshot:

```bash
.venv/Scripts/python scripts/rotate_app_password.py     # haazir_app, every branch
.venv/Scripts/python scripts/rotate_owner_password.py   # hazir_owner, via the Neon API
```

**`MAIL_FROM` must sit on a domain verified in Resend**, or every magic link is rejected at
the provider and nobody can sign in — while `/v1/auth/request-link` still answers 202, because
it is required to answer identically regardless. The failure is visible only as
`resend rejected the message` in the log. Check with:

```bash
curl -s https://api.resend.com/domains -H "Authorization: Bearer $RESEND_API_KEY"
```

`haazir.pk` is **not** verified today; the sender is on a domain that is. Verify `haazir.pk`
before pointing `MAIL_FROM` at it.

**`GEMINI_API_KEY`** is not rotated by either script — it is a Google Cloud key, rotated in
the Google AI Studio console and pasted into `api/.env`. Losing it costs fluency and nothing
else: every explanation falls back to its template and no endpoint fails.

Neither script prints a secret. Do not run either while the test suite is running: its pool recycles
connections every 280 seconds and a recycled one authenticates with whatever is in `.env`
at that moment.

---

## Rate limiting

Two layers. If a legitimate client is being throttled, check which one.

- **`auth/throttle.py`** — blanket per-IP per-minute ceiling on every write. `RATE_LIMIT_ENABLED`
  turns it off. Limits are in `RULES`.
- **Per-endpoint** — semantic limits: one check-in per person per venue per 45 minutes, 20
  staff updates per venue per hour, three magic links per email per 15 minutes. These are
  statements about what the action means and should not be raised to fix load.

Both are in-process, so they reset on deploy and are per-instance. Moving to more than one
machine means moving them to Redis.

---

## Known gaps

Stated here rather than discovered at three in the morning.

- **The scheduler is single-instance.** `RUN_SCHEDULER=true` on two machines doubles the
  compute bill. There is no lock preventing it.
- **The SSE hub is in-process.** More than one instance means subscribers on machine A never
  see events published on machine B. Redis pub/sub first (§8), about twenty lines.
- **Rate limits are per-instance.** Same reason.
- **RLS makes the geospatial index unreachable, and this is not a bug anybody introduced.**
  PostgreSQL will not evaluate a non-leakproof qual ahead of an RLS policy qual, and an index
  condition is by definition evaluated first. The `&&` operator behind `ST_DWithin`
  (`geography_overlaps`) is not leakproof, so `venue_geom_gix` cannot be reached from
  `haazir_app` — search does a sequential scan of `venue` with no error and no warning.

  Measured on production, 1,695 venues, a 300 m radius matching 4 rows:

  | Connected as | Plan | Cost | Heap blocks |
  |---|---|---|---|
  | `hazir_owner` (BYPASSRLS) | Bitmap Index Scan on `venue_geom_gix` | 101 | 4 |
  | `haazir_app` (RLS) | Seq Scan on `venue` | 21,584 | 338 |

  Wall clock is about 3-4 ms either way at this size, so it does not matter yet, and it is
  linear in the table: at 30k venues it is tens of milliseconds, at 100k it is a problem.
  `scripts/index_review.py` checks for it by name so it cannot quietly regress.

  `ALTER FUNCTION geography_overlaps(geography, geography) LEAKPROOF` is the one-line fix and
  requires a real superuser, which Neon's owner role is not — the same wall that migration
  0012 hit with `NOSUPERUSER`. The fix that works here is a leakproof pre-filter column: a
  grid cell or `area_id` with a btree index, narrowed first, with `ST_DWithin` applied to what
  survives. That is a migration and a query rewrite, not an index.
- **`recompute_trust` is an N+1 and only looks fine when co-located.** It loops over every
  active venue and issues several statements each. On the server, next to the database, 1,695
  venues take a few seconds. Run from a laptop in Karachi, where one round trip to Singapore
  is about 170 ms, the same job took **25 minutes**. It is a nightly job so this is not a
  production problem, but do not run it by hand from a laptop and conclude something is wrong.
  `refresh_live_state`, which batches, does 1,695 venues in 8.5 s over the same link.
- **The LLM spend counter is in memory.** Resets on deploy, so the monthly ceiling
  undercounts across frequent deploys.
- **No load test has been run.** Phase 10 asks for p95 under 400 ms at 50 rps and that number
  is unverified. What is measured is server-side query time on the hot paths (11.5 ms for
  search against the full dataset) and the statement count on the write paths. Running 50 rps
  from Karachi against Singapore would measure the link, not the API.
