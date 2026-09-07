# HAAZIR API

**One of two repositories.** The API is here; the Next.js web app is at
<https://github.com/AmmarKamran2005/haazir-frontend>. Running:
<https://haazir-frontend.vercel.app>, against <https://haazir-backend.fly.dev>.

FastAPI + Neon Postgres. Built from a written backend plan, which stays the specification:
this README covers how to run it, not why it is shaped this way.

**Status: Phases 1 to 10 complete, less venue claims and offers. The web app runs against
it.** Every surface in the
prototype has a real API behind it, the concierge answers in Roman Urdu with or without an
LLM, enforcement records are ingested and human-reviewed before publication, and there is a
[`RUNBOOK.md`](RUNBOOK.md) for when something goes wrong. What is not built is listed at the
bottom of this file, honestly.

---

## Run it

You need a Postgres with **PostGIS and pgvector**. Neon has both; a stock local Postgres has
neither, which is why the plan puts the development database on a Neon branch rather than on
your laptop.

```bash
cd api
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # Windows
# .venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux
cp .env.example .env
```

Fill in `.env`. The order matters, because the migration creates the role the API runs as:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET
python -c "import secrets; print(secrets.token_urlsafe(24))"   # APP_DB_PASSWORD
```

1. `DATABASE_URL_DIRECT`: Neon's **direct** string for the owner role, the host without
   `-pooler`. Paste it exactly as Neon gives it, `sslmode` and all; `config.py` strips the
   parameters asyncpg cannot accept.
2. `APP_DB_PASSWORD`: the value you just generated.
3. Run the migrations. `0012` creates `haazir_app` with that password:

```bash
.venv/Scripts/alembic upgrade head
```

4. `DATABASE_URL`: Neon's **pooled** string (host with `-pooler`), with the user and password
   replaced by `haazir_app:<APP_DB_PASSWORD>`.

Why two roles: Neon's default role is a member of `neon_superuser` and carries `BYPASSRLS`,
which outranks `FORCE ROW LEVEL SECURITY` and makes every policy in the schema inert. The
first run of this suite against Neon failed nineteen tests for exactly that reason. The API
connects as `haazir_app`, which owns nothing, has `NOBYPASSRLS`, and holds only DML
privileges; the owner role runs migrations and nothing else.

```bash
.venv/Scripts/uvicorn haazir.main:app --reload --port 8000
```

`http://localhost:8000/health/db` should return `status: ok` and list four extension versions.
If it returns `degraded`, the branch is missing an extension and migration `0001` did not run.

Interactive docs at `/docs` in dev. They are switched off in production.

---

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

Without `TEST_DATABASE_URL` the 17 unit tests run and the rest skip with a visible reason.
With it, all 84 run. The suite **truncates every table**, so it reads `TEST_DATABASE_URL` and
deliberately refuses to fall back to `DATABASE_URL`: a stray `pytest` cannot wipe the branch
your scraped data lives on. Make a throwaway branch, migrate it the same way as above, and put
its two strings in `TEST_DATABASE_URL` and `TEST_DATABASE_URL_DIRECT`.

```bash
neon branches create --name test --project-id <project>
```

The tests that decide whether this is finished:

| Test | Proves |
|---|---|
| `test_rls_group_privacy.py::test_group_creator_cannot_read_another_members_constraint` | The dignity guarantee. §14 rule 5 |
| `test_rls_group_privacy.py::test_the_connection_role_cannot_bypass_rls` | The API is not connecting as a role that skips every policy |
| `test_rls_group_privacy.py::test_rls_is_forced_not_merely_enabled` | The policies are not decoration |
| `test_auth_flow.py::test_a_replayed_refresh_token_revokes_the_whole_family` | §5 rule 6 |
| `test_staff_device.py::test_a_post_from_outside_the_geofence_returns_409` | Phase 2 acceptance |
| `test_rls_venue.py::test_an_owner_cannot_change_a_protected_column` | Phase 7 acceptance, enforced early |
| `test_schema.py` | Phase 1 acceptance |
| `test_normalise.py` | The price-comparison join key actually collapses variants |
| `test_ingest.py::test_every_venue_gets_exactly_168_priors` | Phase 3 acceptance |
| `test_ingest.py::test_review_prose_is_never_stored` | §3.7 |
| `test_estimator_golden.py` | Phase 4 acceptance: the port reproduces the prototype |
| `test_search.py::test_a_nut_allergy_query_never_returns_a_venue_without_the_flag` | Phase 4 acceptance: constraints are filters |
| `test_live.py::test_a_reconnect_replays_only_what_was_missed` | Phase 5 acceptance: SSE resume |
| `test_live.py::test_the_tap_path_stays_within_its_round_trip_budget` | Phase 5 acceptance: the latency budget, in a portable unit |
| `test_group.py::test_six_members_with_mixed_constraints_get_one_feasible_answer` | Phase 6 acceptance |
| `test_group.py::test_no_response_on_this_surface_contains_a_members_inputs` | The dignity guarantee, at the API |
| `test_owner.py::test_price_position_reports_the_area_median` | Phase 7 acceptance |

---

## Layout

```
src/haazir/
  config.py        every environment variable, and the Neon URL rewrite
  db.py            engine, pool settings, and the RLS claim contract
  models/          SQLAlchemy models, one module per domain in §3
  auth/            jwt, tokens, magic_link, refresh, device, group, deps, ratelimit,
                   throttle (the blanket per-IP write ceiling, as middleware)
  estimator/       fusion, queueing, scoring, group, trust, travel
  routers/         health, auth, admin, staff, diner, live, venues, dishes, city,
                   ingest, search, group, owner, ask
  schemas/         pydantic request and response models
  services/        normalise, ingest_venues, ingest_sfa, recompute, realtime, clock,
                   mail, lookup (slug-or-uuid), intent (Roman-Urdu parser),
                   llm (cascade + budget guard)
  workers/         scheduler
alembic/versions/  0001 .. 0013: §3 step by step, the Karachi seed, the app role, dish family
scripts/           ingest.py, index_review.py, rotate_app_password.py,
                   rotate_owner_password.py
tests/golden/      generate_golden.js + engine_golden.json (see Estimator, below)
tests/
RUNBOOK.md         what to do when it is broken
```

### The three things worth knowing before you change anything

**Claims, not roles.** RLS is driven by `SET LOCAL app.*` written per transaction, because one
pooled connection serves an anonymous reader, a diner, a staff tablet and a group guest within
the same second. `auth/deps.py:get_ctx` is the only place claims are ever written.

**A role without BYPASSRLS, and FORCE on top.** `BYPASSRLS` is a role attribute that skips
every policy, and Neon's default role has it. The API connects as `haazir_app` instead, which
does not. `FORCE ROW LEVEL SECURITY` stays on every protected table as the second layer, so
that anything which ever does connect as the owner is still subject to the policies. Both
have a test, because the failure mode is a database that looks secured in `\d` and is not.

**Two flags no request can set.** `app.service` and `app.solver` are the service and solver
escape hatches. A `Claims` object cannot express them, `apply_claims` writes both as empty
strings unless asked by keyword, and no JWT role maps to either, so they are reachable only
from `service_session()` and `solver_session()`. `group_constraint` has no service policy at
all.

---

## Deploy

Singapore, because Neon is in Singapore. One request is one hop from the user to the API and
many queries from the API to the database; co-located those cost about 1 ms each, split across
regions about 80 ms each. Never move one without the other.

```bash
# From api/. Schema first, from your machine — see the note below on why not the container.
alembic upgrade head

fly launch --no-deploy --region sin --name haazir-backend

# Reads api/.env, sends the values over stdin, prints only the key names. It derives
# CORS_ORIGINS and WEB_BASE_URL from the frontend URL rather than copying the localhost ones,
# which is the mistake that makes every magic-link email point at a machine nobody else has.
.venv/Scripts/python scripts/fly_secrets.py --web-url https://<your-app>.vercel.app

fly deploy
fly logs                     # "database ok" and "scheduler started" mean it is up
curl -s https://haazir-backend.fly.dev/health/db
```

**`COOKIE_SAMESITE=none` is set in `fly.toml`, and it matters.** The web app is on
`*.vercel.app` and the API on `*.fly.dev` — different sites, so the refresh cookie is not sent
cross-site under the `lax` default, and a session dies when the thirty-minute access token
expires. Development never shows this: `localhost:3000` and `localhost:8000` are the same
site. `config.py` refuses `none` without `Secure`, because browsers discard that combination
silently.

**There is no dev magic link in production.** `APP_ENV=prod` makes `/v1/auth/request-link`
return `dev_link: null`, by design — returning it would hand a session to anyone who can POST
an address. So sign-in works only if `RESEND_API_KEY` is set **and** `MAIL_FROM` sits on a
domain verified in Resend:

```bash
curl -s https://api.resend.com/domains -H "Authorization: Bearer $RESEND_API_KEY"
```

**One machine, deliberately.** `min_machines_running = 1` and a single uvicorn worker: the SSE
hub, the scheduler and the rate limits are all in-process. Scaling past one means moving the
hub to Redis first (§8), not changing this number.

### Rotating credentials

Two scripts, because the two roles are managed differently and their passwords live in
several places that must agree:

```bash
.venv/Scripts/python scripts/rotate_app_password.py     # haazir_app: SQL on every branch, rewrites DATABASE_URL / TEST_DATABASE_URL
.venv/Scripts/python scripts/rotate_owner_password.py   # hazir_owner: Neon API on every branch, rewrites the *_DIRECT URLs
```

Neither prints a secret. Run them after any credential has been shown in a terminal, a log or
a screenshot, and not while the test suite is running: its pool recycles connections every
280 seconds and a recycled connection authenticates with whatever is in `.env` at that moment.

**Migrations run from your machine against `DATABASE_URL_DIRECT`, never from the container.**

Not because of concurrency — Fly's `release_command` runs once, in one ephemeral machine, so
that concern does not apply. The reason is `DATABASE_URL_DIRECT` is `hazir_owner`, and that
role has `BYPASSRLS`. Putting it in `fly secrets` would leave a credential on the API's host
that skips every row-level security policy in the database, to save one command a human runs
a few times a month. The API only ever needs `haazir_app`, which cannot.

The cost of that choice is that nothing stops a deploy shipping code ahead of its schema. So
before `fly deploy`, from your machine:

```bash
alembic upgrade head
alembic current          # should print the same revision the code expects
```

---

## Loading the scraped dataset

```bash
.venv/Scripts/python scripts/ingest.py ../scraper/out
```

Idempotent, and about a minute for the full Karachi set. A re-run corrects rather than
duplicating: it upserts on `place_id`, then removes menu lines it did not write this time,
because re-normalising a dish name changes its identity and the old row would otherwise stay
behind and make one physical menu item count twice in every price comparison.

### What the current data can and cannot support

| | |
|---|---|
| Venues loaded | 1,695, none rejected |
| Occupancy priors | 168 per venue, all of them. 184 venues from Google `popular_times`, 1,511 from a category archetype |
| Menus | 68 venues of 1,695. This is the binding constraint on price comparison |
| `bihari boti` price comparison | 10 venues. Phase 3's criterion asks for 20, and no amount of normalisation gets there: the ceiling is menu coverage |

`live_fraction` is `0.0` on `/city/pulse` and `/city/stats` and the API says so, because
nothing has posted an observation yet. Every occupancy number in the product today is a
prior, and every response labels it as one.

---

## The estimator

`estimator/` is a port of `app/assets/js/engine.js`, and it is checked against the original
rather than against my reading of it. `tests/golden/generate_golden.js` runs the prototype
under Node and dumps what it says; `test_estimator_golden.py` asserts the Python agrees to
three decimal places, across 303 points of the wait curve, every fusion weight, and the band
thresholds.

After any deliberate change to the model:

```bash
node tests/golden/generate_golden.js > tests/golden/engine_golden.json
```

and read the diff. Every line that moves is a change to what the product tells people.

### What the numbers do

A venue nobody has reported on still gets an estimate: its occupancy prior for this hour,
fused as a first-class observation, with `confidence` of exactly zero. That is not a bug being
tolerated. Confidence is measured against the prior's own spread, so knowing nothing new
scores nothing, and the interface can say "we are guessing" without a second code path.

One staff tap changes that:

| | occupancy | confidence | band | sources |
|---|---|---|---|---|
| prior only | 0.357 | 0.000 | free | prior 1.00 |
| after one staff tap | 0.857 | 0.595 | busy | staff 0.84, prior 0.16 |

### Performance

Search is one query plus arithmetic. Measured against the full Karachi dataset:

```
Execution Time: 11.5 ms      (server side, EXPLAIN ANALYZE)
```

Round-trip from a laptop in Pakistan is ~350 ms, almost all of it network. The plan puts the
API in Singapore next to Neon precisely so that this is not the number production pays; the
11.5 ms is what matters against Phase 4's 400 ms p95 criterion.

---

## The live loop

A staff tablet taps a state; a diner's screen moves. That path is:

```
POST /v1/staff/state
  -> one query for geofence + distance + capacity
  -> INSERT observation, commit
  -> refresh_live_state: write this hour's prior, fuse, upsert live_state
  -> hub.publish  ->  every SSE subscriber on that venue
```

`GET /v1/venues/{id}/live/stream` is the subscriber end. It replays what a reconnecting client
missed using `Last-Event-ID`, sends a keepalive comment every 15 seconds so proxies do not
close an idle connection, and drops a client whose queue fills rather than buffering for it.

### On the 500 ms criterion

Phase 5 asks for the tap to reach a subscriber in under 500 ms. Measured from a laptop in
Karachi it takes about 2.7 s, and that number says nothing about the code: one round trip to
Neon in Singapore is ~170 ms from here and ~1 ms from Fly.io in `sin`, where this deploys. The
path issues **16 statements**, so it is ~16 ms in the deployment the plan specifies.

`test_the_tap_path_stays_within_its_round_trip_budget` therefore asserts the statement count
rather than a stopwatch. That is the quantity that survives moving the test, and it is what
actually regresses when somebody adds a query to the hot path.

### Background jobs

`workers/scheduler.py`, in-process (§11), because the SSE hub lives in this process too:

| Job | Cadence |
|---|---|
| `refresh_live_state` | every 60 s |
| `decay_facts` | 03:10 Karachi |
| `recompute_trust` | 03:30 |
| `purge` | 04:00 |
| `create_partitions` | 1st of the month, 02:00 |

**Exactly one instance may run these.** `RUN_SCHEDULER=false` on any additional machine; two
processes refreshing every minute would double the compute bill on a hundred-hour plan.

---

## Every surface has something behind it

| Prototype surface | Endpoints |
|---|---|
| Diner | search, venue card, dishes, price comparison, check-in, hold, fact verification |
| Venue | live estimate, SSE stream, trust score with the regulatory record |
| City | pulse, stats |
| Staff console | state, today, dish sold-out |
| Group | create, status, constraint, solve, solution |
| Partner | venues, edit, price position, analytics, attribution |
| Concierge | ask (Roman Urdu in, ranked venues out), parse, llm status |
| Admin | venue ingest, SFA ingest, review queue, publish decision |

### The group solver is not OR-Tools, and that is deliberate

The plan names CP-SAT. CP-SAT earns its place when many decision variables constrain one
another; here there is exactly one decision — which venue — over a few hundred candidates,
with each member's feasibility acting as a filter on that domain rather than a relation
between variables. Enumerating the domain is not an approximation of what a solver would
return, it *is* the optimum, in about twenty lines and with no hundred-megabyte dependency in
the image. If a later phase needs to seat a group across several tables, or pick a time slot
and a venue together, that is a real constraint problem and CP-SAT should come back with it.

The objective is unchanged from the plan: `0.72 · min(weighted) + 0.28 · mean`, where the
carry-over weight amplifies a member's **shortfall** rather than their satisfaction. That
direction is the whole point — multiplying satisfaction by 1.4 pushes the compromised member
above everyone else and stops them being the binding minimum.

---

## The concierge answers without an LLM

`POST /v1/ask` takes a sentence — "4 log, 1500 tak, biryani, jaldi chahiye" — and returns
ranked venues with a reason for each. The parse is a deterministic Roman-Urdu regex parser in
`services/intent.py`, not a model call. That is the default path, not a degraded one.

The ordering inside the parser is load-bearing. Party size is read **before** budget, because
`MONEY_RE` matches three-to-six digit numbers and would otherwise swallow the `4` in "4 log".
`HURRY_RE` deliberately excludes "abhi": *abhi* means "right now", which is when every diner is
searching, so treating it as urgency would mark almost every query as a hurry.

The LLM is a layer on top, in `services/llm.py`:

| | Model | Used for |
|---|---|---|
| Intent | `gemini-3.1-flash-lite` | Sentences the regex parser could not resolve |
| Explanation | `gemini-3.1-flash-lite` | Turning a scored result into a sentence |

The model was chosen by measurement, not by version number. Asked for a one-sentence
explanation with `maxOutputTokens: 700`, `gemini-3.6-flash` spent 673 tokens thinking and
returned a truncated half-sentence — 804 tokens for no answer. `gemini-3.1-flash-lite`
returned the complete sentence, in the right Roman-Urdu register, in 180 tokens. The work is
rewording facts that have already been computed; there is nothing to reason about. (The 2.5
family is not an option — the API refuses it for new keys.)

Three things keep it from being load-bearing. A `Budget` tracks spend against
`LLM_MONTHLY_CEILING_USD`, warns at 70% and **stops calling out at 90%**, after which
explanations come from templates and nothing breaks. Explanations are cached on
`(venue, band, party, hour bucket)`, so a busy Friday evening is a handful of calls rather than
one per result. And if the API key is absent, the whole cascade is skipped rather than erroring.

Only the top six results get a model sentence, and they are fetched concurrently. Awaiting
them in turn made a twenty-result `/v1/ask` take about a hundred seconds; nobody reads the
seventeenth card, so the rest keep the template.

`/v1/search` never calls a model at all — it fills `why` from the template, which is pure
Python. That keeps the endpoint the frontend actually uses fast and deterministic, and it is
why every result explains itself with no key configured.

`GET /v1/llm/status` reports where the budget stands. `GET /v1/ask/parse` returns just the
parse, which is how you see what the product understood without spending anything.

**The suite forces `GEMINI_API_KEY` empty** (`conftest.py`), so a test run cannot make live
billed calls, and the default path under test is the one that ships when the guard trips.

---

## Enforcement records are ingested, then reviewed by a person

Phase 9. `POST /v1/admin/ingest/sfa` takes Sindh Food Authority actions — sealed, fined,
notice, cleared, reopened — and attaches them to venues. This is the only part of the system
that makes a public negative claim about a named business, so it is the only part with a human
in the loop.

Matching is `0.70 · trigram name similarity + 0.30 · place proximity`, and the outcome depends
on the score: at **0.90 or above** the record publishes automatically, and anything below waits
in `GET /v1/admin/ingest/review-queue` for `POST /v1/admin/ingest/review/{id}/decide`. An
unmatched or unpublished record is invisible to every diner-facing endpoint, and RLS enforces
that rather than the endpoint doing so.

Every venue has a right of reply (`regulatory_reply`) that travels with the record. There is
deliberately no endpoint that deletes an event — but unpublishing one takes a single call, and
[`RUNBOOK.md`](RUNBOOK.md) treats a wrong record as the most serious incident in the system.

---

## Hardening

Phase 10, and mostly unglamorous.

**Rate limiting is middleware, not a call in each handler.** `auth/throttle.py` applies a
per-IP per-minute ceiling to every write, so an endpoint added next month is covered without
anybody remembering to cover it. Reads are never limited: browsing is never gated, and a cap
on GETs would hit the city map polling the live feed long before it hit anyone abusing the API.
Semantic limits stay next to their handlers — one check-in per person per venue per 45 minutes
is a statement about what a check-in *is*, not a defence against a flood.

**`RATE_LIMIT_ENABLED` and `RUN_SCHEDULER` exist for the test suite.** A per-minute ceiling
applied to a suite that submits six group constraints a second would fail on how fast the tests
run rather than on what the code does, and a job rewriting `live_state` every sixty seconds
through a fourteen-minute run would fight the tests asserting on it. `test_throttle.py` turns
the throttle back on and tests it directly.

**Restores are rehearsed, not assumed.** A Neon point-in-time branch was taken from production
and verified: ready in 10 s, usable in 20 s. It failed twice first, for reasons now written
down in the runbook.

---

## What is not built yet

**Venue claims and offers**, the remainder of Phase 7. The `Offer` model and
`venue.claimed_by` exist in the schema; no endpoint writes to either. A partner is created by
an admin today.

**A load test.** Phase 10 asks for p95 under 400 ms at 50 rps and that number is unverified.
What is measured instead is server-side query time on the hot paths — 11.5 ms for search
against the full dataset — and the statement count on the write paths. Running 50 rps from
Karachi against Singapore would measure the link, not the API.

**A geospatial index that RLS puts out of reach.** Search sequentially scans `venue` because
PostgreSQL will not let a non-leakproof qual become an index condition ahead of an RLS policy,
and `ST_DWithin`'s underlying operator is not leakproof. It costs 3-4 ms at 1,695 venues and
grows linearly. Diagnosed, measured and given its fix in
[`RUNBOOK.md`](RUNBOOK.md#known-gaps); `scripts/index_review.py` guards against it regressing.

**Three things that only work on one machine**: the scheduler has no lock, the SSE hub is
in-process, and rate limits are per-instance. All three are listed with their fixes in
[`RUNBOOK.md`](RUNBOOK.md#known-gaps).

Two more things are limited by data rather than code. `dish_time_quality` is empty, so the palate
factor falls back to a venue-level mean; it fills in once diners rate dishes. And no scraped
venue reports `capacity_covers`, so the wait model uses its medium-room default and
`idle_seats_now` is null rather than a guess.
