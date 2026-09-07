"""Index review against whatever data the branch actually holds. Plan §12 Phase 10.

Three questions, in the order that matters:

1. Is a hot path doing a sequential scan over a large table? That is the one that hurts.
2. Which indexes has nothing ever used? Each one costs write throughput and disk for nothing.
3. What are the slowest statements the server has seen? `pg_stat_statements` is not enabled on
   Neon's free tier, so this degrades to a note rather than an error.

Run it against a branch with real data. On an empty branch every plan is a sequential scan
because that is genuinely the fastest way to read nothing, and the output means nothing.
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "src")

from sqlalchemy import text  # noqa: E402

from haazir.db import service_session  # noqa: E402

# The paths a diner actually waits on. Each is the shape the router issues, not a synthetic
# query: a plan for a query nobody runs proves nothing.
# `:venue_id` and `:family` are bound from a lookup done before the EXPLAIN. An inline
# `(SELECT id FROM venue LIMIT 1)` would add its own sequential scan to every plan and the
# report would blame the query for the harness.
HOT_PATHS: list[tuple[str, str, dict]] = [
    (
        "search: candidates by distance + open + active",
        """
        SELECT v.id, v.name,
               ST_Distance(v.geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) AS m
          FROM venue v
         WHERE v.status = 'active'
           AND ST_DWithin(v.geom, ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, 8000)
         ORDER BY m
         LIMIT 60
        """,
        {"lat": 24.8615, "lng": 67.0180},
    ),
    (
        "venue card: live state by venue",
        "SELECT * FROM live_state WHERE venue_id = :venue_id",
        {},
    ),
    (
        "dish price comparison: one dish family across venues",
        """
        SELECT vdp.venue_id, vdp.price_pkr
          FROM venue_dish_price vdp
          JOIN dish d ON d.id = vdp.dish_id
         WHERE d.family = :family
        """,
        {},
    ),
    (
        "fusion: recent observations for one venue",
        """
        SELECT source, value, observed_at
          FROM observation
         WHERE venue_id = :venue_id
           AND observed_at > now() - interval '6 hours'
         ORDER BY observed_at DESC
        """,
        {},
    ),
]

# Below this a sequential scan is the right plan and flagging it is noise.
SEQ_SCAN_ROWS_THRESHOLD = 1_000


async def main() -> int:
    async with service_session() as s:
        sizes = {
            r.relname: r.n
            for r in (
                await s.execute(
                    text(
                        "SELECT relname, n_live_tup AS n FROM pg_stat_user_tables"
                        " WHERE n_live_tup > 0 ORDER BY n DESC"
                    )
                )
            ).all()
        }
        print("rows per table (non-empty only)")
        for name, n in list(sizes.items())[:12]:
            print(f"  {name:24} {n:>8,}")
        if not sizes:
            print("  (empty branch — the rest of this report would be meaningless)")
            return 1

        bindings = {
            "venue_id": await s.scalar(text("SELECT id FROM venue LIMIT 1")),
            "family": await s.scalar(text("SELECT family FROM dish LIMIT 1")),
        }

        print("\nhot paths")
        problems = 0
        for label, sql, params in HOT_PATHS:
            plan = "\n".join(
                r[0]
                for r in (
                    await s.execute(
                        text(f"EXPLAIN (ANALYZE, BUFFERS) {sql}"), {**bindings, **params}
                    )
                ).all()
            )
            ms = 0.0
            for line in plan.splitlines():
                if "Execution Time:" in line:
                    ms = float(line.split(":")[1].strip().split()[0])
            # A seq scan is only a problem when it reads a lot of rows.
            bad = [
                ln.strip()
                for ln in plan.splitlines()
                if "Seq Scan" in ln
                and any(
                    sizes.get(t, 0) > SEQ_SCAN_ROWS_THRESHOLD for t in sizes if t in ln
                )
            ]
            mark = "!!" if bad else "ok"
            print(f"  [{mark}] {label:52} {ms:7.2f} ms")
            for ln in bad:
                problems += 1
                print(f"        {ln[:100]}")

        print("\nindexes nothing has ever used")
        unused = (
            await s.execute(
                text(
                    """
                    SELECT s.relname AS tbl, s.indexrelname AS idx,
                           pg_size_pretty(pg_relation_size(s.indexrelid)) AS sz
                      FROM pg_stat_user_indexes s
                      JOIN pg_index i ON i.indexrelid = s.indexrelid
                     WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary
                     ORDER BY pg_relation_size(s.indexrelid) DESC
                    """
                )
            )
        ).all()
        if unused:
            for r in unused:
                # Zero scans on a branch that has served no traffic is expected, so this is
                # information rather than a finding.
                print(f"  {r.tbl:24} {r.idx:44} {r.sz}")
            print(f"  ({len(unused)} — expected on a branch that has served no real traffic)")
        else:
            print("  none")

        # --- the one pathology that is invisible unless you ask for it ------------
        #
        # Under RLS, PostgreSQL will not evaluate a non-leakproof qual ahead of the policy
        # quals, and an index condition is by definition evaluated first. The `&&` operator
        # behind ST_DWithin (`geography_overlaps`) is not leakproof, so on a table with RLS
        # the GiST index cannot be reached at all: no error, no warning, just a sequential
        # scan on the most important query in the product.
        #
        # `ALTER FUNCTION ... LEAKPROOF` is the one-line fix and needs a real superuser,
        # which Neon's owner role is not. So this reports rather than repairs.
        print("")
        print("geospatial index reachability under RLS")
        geo_sql = (
            "EXPLAIN SELECT id FROM venue"
            " WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(67.0180, 24.8615),"
            " 4326)::geography, 300)"
        )
        as_app = chr(10).join(r[0] for r in (await s.execute(text(geo_sql))).all())
        reachable = "venue_geom_gix" in as_app
        print(f"  venue_geom_gix reachable by the app role: {reachable}")
        if not reachable:
            print("    Expected on any RLS table; see RUNBOOK.md 'Known gaps'.")
            print("    Costs a scan of `venue`: fine at city scale, not fine at national")
            print("    scale. The fix is a leakproof pre-filter column (grid cell or")
            print("    area_id), not a different index.")

        print("\nslowest statements")
        try:
            rows = (
                await s.execute(
                    text(
                        "SELECT round(mean_exec_time::numeric, 2) AS ms, calls,"
                        " left(query, 70) AS q FROM pg_stat_statements"
                        " WHERE query NOT LIKE '%pg_stat%' ORDER BY mean_exec_time DESC LIMIT 8"
                    )
                )
            ).all()
            for r in rows:
                print(f"  {r.ms:>9} ms  x{r.calls:<6} {r.q}")
        except Exception:
            print("  pg_stat_statements is not available on this branch (Neon free tier).")
            print("  Use `neon` project metrics, or EXPLAIN the paths above, instead.")

        print(f"\n{problems} hot path(s) doing a large sequential scan")
        return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
