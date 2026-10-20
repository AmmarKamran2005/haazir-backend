"""Camera logic that needs no database: the table map, the occupancy figure, the fusion entry."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from haazir.estimator import fusion
from haazir.routers.camera import (
    ZonesDoc,
    estimated_tables,
    merge_tables,
    occupancy_of,
    snapshot_url,
)

ZONES = [
    {"table_id": "a", "label": "Window", "seats": 4, "polygon": [[0, 0], [1, 0], [1, 1]]},
    {"table_id": "b", "label": None, "seats": 2, "polygon": [[0, 0], [1, 0], [1, 1]]},
    {"table_id": "c", "seats": 6, "polygon": [[0, 0], [1, 0], [1, 1]]},
]


def test_every_zone_appears_and_unknown_readings_are_kept():
    tables = merge_tables(
        ZONES,
        [{"table_id": "a", "occupied": True, "persons": 3},
         {"table_id": "z", "occupied": True, "persons": 1}],
    )
    assert [t["table_id"] for t in tables] == ["a", "b", "c", "z"]
    assert tables[0] == {"table_id": "a", "label": "Window", "seats": 4,
                         "occupied": True, "persons": 3}
    assert tables[1]["label"] == "b" and tables[1]["occupied"] is False


def test_occupancy_is_occupied_over_total():
    tables = merge_tables(ZONES, [{"table_id": "a", "occupied": True, "persons": 2},
                                  {"table_id": "c", "occupied": True, "persons": 5}])
    assert occupancy_of(tables) == pytest.approx(2 / 3)
    assert occupancy_of([]) is None


def test_estimated_map_is_stable_and_fills_monotonically():
    vid = uuid.uuid4()
    low = estimated_tables(vid, 0.25, [], 40)
    high = estimated_tables(vid, 0.75, [], 40)
    assert len(low) == 10
    assert estimated_tables(vid, 0.25, [], 40) == low
    low_ids = {t["table_id"] for t in low if t["occupied"]}
    high_ids = {t["table_id"] for t in high if t["occupied"]}
    assert low_ids < high_ids
    assert sum(t["occupied"] for t in high) == 8


def test_zones_must_have_unique_ids_and_xy_points():
    with pytest.raises(ValidationError):
        ZonesDoc(frame_w=640, frame_h=480, zones=[ZONES[0], ZONES[0]])
    with pytest.raises(ValidationError):
        ZonesDoc(frame_w=640, frame_h=480,
                 zones=[{**ZONES[0], "polygon": [[0, 0, 0], [1, 1], [2, 2]]}])


def test_camera_is_a_fusion_source_that_dominates_the_prior():
    now = dt.datetime.now(dt.UTC)
    fused = fusion.fuse(
        [fusion.Observation("prior", 0.3, 0.215, now),
         fusion.Observation("camera", 0.9, 0.05, now, reporter_trust=0.95)],
        now,
    )
    assert fused.source_weights["camera"] > 0.9
    assert fused.occupancy > 0.85
    stale = fusion.fuse(
        [fusion.Observation("prior", 0.3, 0.215, now),
         fusion.Observation("camera", 0.9, 0.05, now - dt.timedelta(minutes=60))],
        now,
    )
    assert stale.source_weights["camera"] < 0.2


def test_snapshot_url_is_signed():
    url = snapshot_url(uuid.uuid4())
    assert "/snapshot.jpg?exp=" in url and "&sig=" in url


async def test_a_replayed_reading_is_refused_before_the_rate_limit():
    import datetime as dt

    from fastapi import HTTPException

    from haazir.auth.camera import Camera
    from haazir.routers.camera import IngestIn, ingest

    cam = Camera(id=uuid.uuid4(), venue_id=uuid.uuid4(), name="x", kind="webcam")
    old = IngestIn(ts=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5), tables=[])
    with pytest.raises(HTTPException) as exc:
        await ingest(old, cam)
    assert exc.value.status_code == 409
