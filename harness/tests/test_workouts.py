from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.index import app
from harness.clients import sie
from harness.recommend import workouts as W

NOW = "2026-11-20T09:00:00-08:00"


def req(**over):
    b = {"persona": "jordan", "now": NOW, "shoes": {"boughtOn": "2026-10-22", "startMiles": 267},
         "raceDate": "2026-11-29", "newPairArrives": "2026-11-23"}
    b.update(over)
    return W.WorkoutsRequest(**b)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(sie, "chat_json", lambda *a, **k: (_ for _ in ()).throw(sie.SIEUnavailable("off")))


def by_day(plan):
    return {s["date"]: s for s in plan["sessions"]}


def test_worn_shoes_move_long_run_off_road():
    s = by_day(W.build(req()))[date(2026, 11, 22)]           # Sunday: long run in Tony's memory slot
    assert s["activityType"] in ("Elliptical", "Cycling") and any("off the road" in t for t in s["tags"])
    assert any("(memory)" in t for t in s["tags"]) and s["hour"] == 8


def test_break_in_runs_after_arrival_then_normal():
    runs = [s for s in W.build(req())["sessions"] if any("break-in" in t for t in s["tags"])]
    assert len(runs) == 2 and all(s["date"] > date(2026, 11, 23) and s["minutes"] <= 30 for s in runs)


def test_no_new_pair_means_no_break_in_and_runs_capped():
    plan = W.build(req(newPairArrives=None))
    assert not any("break-in" in t for s in plan["sessions"] for t in s["tags"])
    assert all(s["minutes"] <= W.WORN_RUN_MAX_MIN for s in plan["sessions"] if s["activityType"] == "Running")


def test_race_day_and_rest_before():
    plan = W.build(req(now="2026-11-24T09:00:00-08:00", raceDate="2026-11-29"))
    days = by_day(plan)
    assert days[date(2026, 11, 29)].get("race") and date(2026, 11, 28) not in days


def test_planned_load_within_cap():
    sig = W.build(req(raceDate=None, shoes=None))["signals"]
    assert sig["plannedLoad"] <= sig["loadCap"] * 1.05


def test_busy_time_moves_session():
    busy = [{"start": "2026-11-24T17:30:00-08:00", "end": "2026-11-24T19:00:00-08:00"}]
    s = by_day(W.build(req(busy=busy)))[date(2026, 11, 24)]
    assert s["start"].hour == 19 and any("calendar conflict" in t for t in s["tags"])


def test_readiness_eases_first_hard_session(monkeypatch):
    real = W.signals
    def tired(*a):
        sig = real(*a)
        return {**sig, "readiness": "ease off: sleep down 45 min/night"}
    monkeypatch.setattr(W, "signals", tired)
    eased = [s for s in W.build(req(raceDate=None))["sessions"] if any("ease off" in t for t in s["tags"])]
    assert len(eased) == 1


def test_route_offline_uses_templates_and_shapes_actions(offline):
    r = TestClient(app).post("/workouts/recommend", json=req().model_dump(mode="json")).json()
    assert r["source"] == "rules" and r["actions"] and r["memoryUsed"]
    a = r["actions"][0]
    assert a["kind"] == "addWorkout" and a["after"]["start"].startswith("2026-11-21") and a["reason"]
    assert len({x["id"] for x in r["actions"]}) == len(r["actions"])


def test_route_ids_stable(offline):
    c = TestClient(app)
    body = req().model_dump(mode="json")
    assert [a["id"] for a in c.post("/workouts/recommend", json=body).json()["actions"]] == \
           [a["id"] for a in c.post("/workouts/recommend", json=body).json()["actions"]]
