"""Group sessions. Plan §7, §14 rule 5.

*"No group member's budget is returned to anyone, including the creator."*

Every endpoint here is shaped by that sentence. `GET /groups/{id}` says who has responded and
never what they said. `POST /groups/{id}/constraint` accepts a guest token scoped to one slot
and writes only that slot. `POST /groups/{id}/solve` reads every constraint through
`solver_session`, the one path with a policy that returns more than a single row, and returns
a satisfaction vector: a slot, a name, and a number between zero and one.

The database enforces this rather than these handlers. `group_constraint` is FORCE ROW LEVEL
SECURITY with no policy granting the creator, another member, an admin or the service path
read access. If a future endpoint forgets to filter, it gets nothing rather than everything,
and `test_rls_group_privacy.py` is what keeps that true.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from ..auth import group as group_auth
from ..auth.deps import Ctx, CurrentGuest
from ..config import settings
from ..db import solver_session
from ..estimator import group as solver

router = APIRouter(prefix="/v1/groups", tags=["group"])

MAX_MEMBERS = 12
GROUP_TTL_HOURS = 36


class CreateGroupIn(BaseModel):
    title: str = Field(default="Dinner", max_length=80)
    members: list[str] = Field(min_length=2, max_length=MAX_MEMBERS,
                               description="Display names, one per person.")
    from_lat: float | None = Field(default=None, ge=-90, le=90)
    from_lng: float | None = Field(default=None, ge=-180, le=180)
    city: str = "Karachi"


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(body: CreateGroupIn, ctx: Ctx) -> dict:
    """Create a group and mint one invite link per member.

    The creator may be anonymous. Requiring a login to organise dinner for six would put an
    account between five other people and the thing they are trying to do.
    """
    city_id = await ctx.session.scalar(
        text("SELECT id FROM city WHERE name = :n"), {"n": body.city}
    )
    if city_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown city")

    group_id = await ctx.session.scalar(
        text(
            """
            INSERT INTO group_session (creator_id, title, city_id, party_size, expires_at)
            VALUES (:creator, :title, :city, :party, now() + make_interval(hours => :ttl))
         RETURNING id
            """
        ),
        {
            "creator": ctx.user_id, "title": body.title, "city": city_id,
            "party": len(body.members), "ttl": GROUP_TTL_HOURS,
        },
    )

    await ctx.session.execute(
        text(
            "INSERT INTO group_member (group_id, slot, display_name) "
            "VALUES (:g, :slot, :name)"
        ),
        [
            {"g": group_id, "slot": i, "name": name.strip()[:60] or f"Guest {i}"}
            for i, name in enumerate(body.members, start=1)
        ],
    )

    invites = await group_auth.issue_invites(
        ctx.session, group_id, list(range(1, len(body.members) + 1))
    )
    await ctx.session.execute(
        text(
            "UPDATE group_session SET from_area_id = ("
            "  SELECT id FROM area WHERE city_id = :c "
            "   ORDER BY centroid <-> ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography "
            "   LIMIT 1) WHERE id = :g"
        ),
        {"c": city_id, "g": group_id,
         "lat": body.from_lat or 24.8607, "lng": body.from_lng or 67.0011},
    )

    base = settings.web_base_url.rstrip("/")
    return {
        "group_id": str(group_id),
        "title": body.title,
        "expires_at": dt.datetime.now(dt.UTC) + dt.timedelta(hours=GROUP_TTL_HOURS),
        # Returned once, to the creator, to distribute. The tokens are stored hashed.
        "invites": [
            {"slot": slot, "name": body.members[slot - 1],
             "link": f"{base}/g/{group_id}/me?t={token}"}
            for slot, token in sorted(invites.items())
        ],
        "note": (
            "Send each person their own link. What they enter is visible to nobody, "
            "including you."
        ),
    }


@router.get("/{group_id}")
async def group_status(group_id: uuid.UUID, ctx: Ctx) -> dict:
    """Who has responded. Never what they said.

    This is the screen the organiser watches while waiting, and the temptation is to show a
    little of what has come in so it feels alive. The count is the whole of it.
    """
    group = (
        await ctx.session.execute(
            text(
                "SELECT g.id, g.title, g.party_size, g.status, g.expires_at, "
                "       a.name AS from_area "
                "  FROM group_session g LEFT JOIN area a ON a.id = g.from_area_id "
                " WHERE g.id = :g"
            ),
            {"g": group_id},
        )
    ).mappings().first()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such group")

    members = (
        await ctx.session.execute(
            text(
                """
                -- `responded_at` on group_member, NOT a join to group_constraint.
                -- The organiser is an ordinary caller and RLS gives them no rows at all on
                -- that table, so a join there silently reports nobody has answered. The
                -- split is also the right one: the FACT of answering belongs to the group,
                -- the CONTENT belongs to its author.
                SELECT m.slot, m.display_name, m.weight,
                       (m.responded_at IS NOT NULL) AS responded
                  FROM group_member m
                 WHERE m.group_id = :g
                 ORDER BY m.slot
                """
            ),
            {"g": group_id},
        )
    ).mappings().all()

    responded = sum(1 for m in members if m["responded"])
    return {
        "group_id": str(group_id),
        "title": group["title"],
        "from_area": group["from_area"],
        "status": group["status"],
        "expires_at": group["expires_at"],
        "party_size": group["party_size"],
        "responded": responded,
        "members": [
            {
                "slot": m["slot"],
                "name": m["display_name"],
                "responded": m["responded"],
                # Above 1.0 means this person compromised last time and the solver will work
                # harder for them. Public on purpose: the group should be able to see that
                # somebody is owed, without seeing what they asked for.
                "weight": round(float(m["weight"]), 2),
            }
            for m in members
        ],
        "solvable": responded >= 2,
    }


class ConstraintIn(BaseModel):
    budget_pkr: int | None = Field(default=None, ge=50, le=100_000)
    max_travel_min: int | None = Field(default=None, ge=5, le=180)
    diet: list[str] = Field(default_factory=list, max_length=6)
    mood: str | None = Field(default=None, max_length=32)


@router.post("/{group_id}/constraint", status_code=status.HTTP_201_CREATED)
async def submit_constraint(
    group_id: uuid.UUID, body: ConstraintIn, principal: CurrentGuest, ctx: Ctx
) -> dict:
    """Write this guest's own constraint, and only this guest's.

    The slot comes from the token, never from the request body. Even so, RLS checks it again:
    `gc_self_write` requires `group_id` and `member_slot` to match the transaction's claims,
    so a bug here that wrote the wrong slot would be rejected by the database rather than
    quietly corrupting somebody else's answer.
    """
    if principal.group_id != group_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this token is for another group"
        )

    await ctx.session.execute(
        text(
            """
            INSERT INTO group_constraint (group_id, member_slot, budget_pkr,
                                          max_travel_min, diet, mood)
            VALUES (:g, :slot, :budget, :travel, CAST(:diet AS text[]), :mood)
            ON CONFLICT (group_id, member_slot) DO UPDATE SET
                budget_pkr = EXCLUDED.budget_pkr,
                max_travel_min = EXCLUDED.max_travel_min,
                diet = EXCLUDED.diet,
                mood = EXCLUDED.mood,
                submitted_at = now()
            """
        ),
        {
            "g": group_id, "slot": principal.slot, "budget": body.budget_pkr,
            "travel": body.max_travel_min, "diet": body.diet, "mood": body.mood,
        },
    )
    await ctx.session.execute(
        text(
            "UPDATE group_member SET responded_at = now() "
            " WHERE group_id = :g AND slot = :slot"
        ),
        {"g": group_id, "slot": principal.slot},
    )

    return {
        "group_id": str(group_id),
        "slot": principal.slot,
        "recorded": True,
        "note": "Only you can see this. The organiser sees that you answered, not what.",
    }


@router.get("/{group_id}/constraint")
async def my_constraint(group_id: uuid.UUID, principal: CurrentGuest, ctx: Ctx) -> dict:
    """Read back your own answer so the form can be edited.

    RLS returns this row to its author and to nobody else; `gc_self_read` matches on both the
    group and the slot in the transaction's claims.
    """
    if principal.group_id != group_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="wrong group")

    row = (
        await ctx.session.execute(
            text(
                "SELECT budget_pkr, max_travel_min, diet, mood, submitted_at "
                "  FROM group_constraint WHERE group_id = :g AND member_slot = :slot"
            ),
            {"g": group_id, "slot": principal.slot},
        )
    ).mappings().first()
    if row is None:
        return {"group_id": str(group_id), "slot": principal.slot, "submitted": False}
    return {"group_id": str(group_id), "slot": principal.slot, "submitted": True, **dict(row)}


class SolveIn(BaseModel):
    from_lat: float | None = Field(default=None, ge=-90, le=90)
    from_lng: float | None = Field(default=None, ge=-180, le=180)
    limit: int = Field(default=5, ge=1, le=20)


@router.post("/{group_id}/solve")
async def solve_group(group_id: uuid.UUID, body: SolveIn, ctx: Ctx) -> dict:
    """One answer, and how well it serves the person it serves worst.

    The constraints are read inside `solver_session`, they become utilities, and the utilities
    are what leaves this function. No caller under any role receives an input.
    """
    group = (
        await ctx.session.execute(
            text(
                "SELECT g.id, g.title, g.status, c.name AS city, "
                "       ST_Y(a.centroid::geometry) AS lat, ST_X(a.centroid::geometry) AS lng "
                "  FROM group_session g "
                "  JOIN city c ON c.id = g.city_id "
                "  LEFT JOIN area a ON a.id = g.from_area_id "
                " WHERE g.id = :g"
            ),
            {"g": group_id},
        )
    ).mappings().first()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no such group")

    async with solver_session(group_id) as session:
        members = await solver.load_members(session, group_id)

    if len(members) < 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{len(members)} of the group have answered so far; at least 2 are needed.",
        )

    solutions, diagnostics = await solver.solve(
        ctx.session,
        members,
        from_lat=body.from_lat if body.from_lat is not None else (group["lat"] or 24.8607),
        from_lng=body.from_lng if body.from_lng is not None else (group["lng"] or 67.0011),
        city=group["city"],
    )

    if not solutions:
        # An honest nothing. These are hard constraints, and quietly relaxing one to produce
        # an answer would be the single worst thing this surface could do.
        return {
            "group_id": str(group_id),
            "solved": False,
            "detail": (
                "No venue satisfies everyone. These are hard constraints, so nothing has "
                "been relaxed to force an answer."
            ),
            "diagnostics": diagnostics,
        }

    best = solutions[0]
    await ctx.session.execute(
        text(
            """
            INSERT INTO group_solution (group_id, venue_id, objective, min_sat, mean_sat,
                                        satisfaction, rationale)
            VALUES (:g, :v, :obj, :min, :mean, CAST(:sat AS jsonb), :why)
            """
        ),
        {
            "g": group_id, "v": best.venue_id, "obj": best.objective,
            "min": best.min_sat, "mean": best.mean_sat,
            "sat": json.dumps([s.as_dict() for s in best.satisfaction]),
            "why": _rationale(best),
        },
    )
    await ctx.session.execute(
        text("UPDATE group_session SET status = 'solved' WHERE id = :g"), {"g": group_id}
    )

    return {
        "group_id": str(group_id),
        "solved": True,
        "best": _solution_dict(best),
        "runner_up": _solution_dict(solutions[1]) if len(solutions) > 1 else None,
        "alternatives": [_solution_dict(s) for s in solutions[1 : body.limit]],
        "diagnostics": diagnostics,
        "objective": {
            "kind": "max-min",
            "formula": "0.72 * min(weighted satisfaction) + 0.28 * mean(satisfaction)",
            "why": (
                "Maximising the minimum protects whoever the venue suits worst. Maximising "
                "the mean would trade one person's evening for four others'."
            ),
        },
    }


@router.get("/{group_id}/solution")
async def latest_solution(group_id: uuid.UUID, ctx: Ctx) -> dict:
    row = (
        await ctx.session.execute(
            text(
                "SELECT s.venue_id, v.name, v.slug, a.name AS area, s.objective, s.min_sat, "
                "       s.mean_sat, s.satisfaction, s.rationale, s.solved_at "
                "  FROM group_solution s "
                "  JOIN venue v ON v.id = s.venue_id "
                "  LEFT JOIN area a ON a.id = v.area_id "
                " WHERE s.group_id = :g ORDER BY s.solved_at DESC LIMIT 1"
            ),
            {"g": group_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="this group has not been solved yet"
        )
    return {"group_id": str(group_id), **dict(row)}


def _solution_dict(s: solver.Solution) -> dict:
    return {
        "venue_id": str(s.venue_id),
        "venue_name": s.venue_name,
        "area": s.area,
        "objective": round(s.objective, 4),
        # Both are returned. The gap between them is the difference between "everyone is
        # fine" and "four are delighted and one is miserable", and hiding it would hide the
        # only thing the max-min objective exists to surface.
        "min_satisfaction": round(s.min_sat, 4),
        "mean_satisfaction": round(s.mean_sat, 4),
        "satisfaction": [x.as_dict() for x in s.satisfaction],
        "travel_min": round(s.travel_min),
        "live": {"occupancy": round(s.occupancy, 4), "band": s.band,
                 "wait_p50_min": round(s.wait_p50, 1)},
    }


def _rationale(s: solver.Solution) -> str:
    worst = min(s.satisfaction, key=lambda x: x.u)
    return (
        f"{s.venue_name} works for everyone who answered. The tightest fit is {worst.name} "
        f"at {worst.u:.0%}; nobody scores lower."
    )
