from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.index import app
from harness import data
from harness.clients import sie
from harness.models import ShoeState
from harness.recommend import shoes
from harness.recommend.condition import condition

TZ = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)


def body(**over):
    b = {"persona": "jordan", "now": NOW.isoformat(),
         "shoes": {"boughtOn": "2026-10-22", "startMiles": 267},
         "raceDate": (NOW.date() + timedelta(days=9)).isoformat(),
         "mandate": [{"id": "ask-first", "text": "Ask me.", "category": "any", "maxAutoCents": 0}]}
    b.update(over)
    return b


@pytest.fixture
def no_sie(monkeypatch):
    from harness import config
    monkeypatch.setattr(config, "SENSO_API_KEY", "")
    def boom(*a, **k):
        raise sie.SIEUnavailable("offline")
    monkeypatch.setattr(sie, "rerank", boom)
    monkeypatch.setattr(sie, "chat_json", boom)


def test_wear_uses_real_distance():
    ws = data.workouts("jordan", NOW)
    c = condition(ws, ShoeState(boughtOn=date(2026, 10, 22), startMiles=267), NOW)
    assert c.status == "replace" and 380 <= c.miles <= 400 and c.runsCounted > 0


def test_history_is_reanchored_to_end_before_now():
    ws = data.workouts("jordan", NOW)
    assert max(w["end"] for w in ws) < NOW and max(w["start"] for w in ws).date() >= NOW.date() - timedelta(days=3)


def test_offline_falls_back_to_pacer_9(no_sie):
    r = TestClient(app).post("/shoes/recommend", json=body()).json()
    p = r["purchase"]
    assert r["source"] == "rules" and r["live"] is False
    assert p["item"] == "Pacer 9" and p["status"] == "awaitingApproval" and p["id"] == "shoes-2026-10-22"
    assert p["options"][0]["name"] == "Pacer 9" and "lostBecause" not in p["options"][0]
    assert all(o.get("lostBecause") for o in p["options"][1:])


def test_fresh_pair_no_purchase(no_sie):
    r = TestClient(app).post("/shoes/recommend", json=body(shoes={"boughtOn": "2026-11-15", "startMiles": 0})).json()
    assert r.get("purchase") is None and r["condition"]["status"] == "fresh"


def test_race_too_far_no_purchase(no_sie):
    r = TestClient(app).post("/shoes/recommend", json=body(raceDate="2027-03-01")).json()
    assert r.get("purchase") is None


def test_unknown_persona_404():
    assert TestClient(app).post("/shoes/recommend", json=body(persona="nobody")).status_code == 404


def test_guard_rejects_off_list_and_missing_reasons():
    elig = [{"id": "a"}, {"id": "b"}]
    with pytest.raises(ValueError):
        shoes._guard({"chosenId": "z", "whyChosen": "x", "lost": []}, elig)
    with pytest.raises(ValueError):
        shoes._guard({"chosenId": "a", "whyChosen": "x", "lost": []}, elig)
    assert shoes._guard({"chosenId": "a", "whyChosen": "x", "lost": [{"id": "b", "lostBecause": "pricier"}]}, elig) == {"b": "pricier"}


def test_bad_model_answer_falls_back(monkeypatch):
    monkeypatch.setattr(sie, "rerank", lambda q, d, i, **k: {k2: 0.5 for k2 in d})
    monkeypatch.setattr(sie, "chat_json", lambda *a, **k: {"chosenId": "aero-elite", "whyChosen": "fast", "lost": []})
    r = TestClient(app).post("/shoes/recommend", json=body()).json()
    assert r["source"] == "rules" and r["purchase"]["item"] == "Pacer 9"


def test_evidence_route_cached_without_senso(monkeypatch):
    from harness import config
    monkeypatch.setattr(config, "SENSO_API_KEY", "")
    r = TestClient(app).post("/evidence", json={"purchaseKind": "shoes", "query": "x"}).json()
    assert r["live"] is False and len(r["evidence"]) >= 3


def test_secret_enforced_when_set(monkeypatch, no_sie):
    from harness import config
    monkeypatch.setattr(config, "HARNESS_SECRET", "s3cret-value")
    c = TestClient(app)
    assert c.post("/shoes/recommend", json=body()).status_code == 401
    assert c.post("/shoes/recommend", json=body(), headers={"X-Harness-Secret": "s3cret-value"}).status_code == 200
    assert c.post("/evidence", json={"purchaseKind": "shoes", "query": "x"}).status_code == 200


@pytest.mark.skipif(not sie.config.SIE_API_KEY, reason="no SIE key")
def test_sie_smoke_live():
    w = sie.warm(provision_timeout_s=240)
    assert "rerank_ms" in w and "chat_ms" in w, w


@pytest.mark.web
def test_web_snapshot_candidates_used_and_filtered(no_sie, monkeypatch):
    from harness import store
    monkeypatch.setattr(store, "load_candidates", lambda persona: None)
    monkeypatch.setattr(store, "cached_candidates", lambda persona: None)
    snap = {"candidates": [
        {"id": "a", "name": "Brand A Cushy", "amountCents": 15000, "arrivesInDays": 3, "wideToeBox": True, "carbonPlate": None,
         "surface": "road", "cushion": "high", "dropMm": 8, "lifeKm": 650, "blurb": "q", "quote": "Great for long runs.",
         "url": "https://example.com/r", "sourceTitle": "Review", "sourceHost": "example.com", "web": True},
        {"id": "b", "name": "Brand B Unknown", "amountCents": 9000, "arrivesInDays": 3, "wideToeBox": None, "carbonPlate": None,
         "surface": "road", "cushion": "high", "dropMm": 8, "lifeKm": 650, "blurb": "q", "quote": "q", "url": "", "sourceTitle": "R",
         "sourceHost": "x", "web": True},
        {"id": "c", "name": "Brand C Flat", "amountCents": 4000, "arrivesInDays": 3, "wideToeBox": True, "carbonPlate": None,
         "surface": "road", "cushion": "low", "dropMm": 0, "lifeKm": 650, "blurb": "q", "quote": "q", "url": "", "sourceTitle": "R",
         "sourceHost": "x", "web": True}]}
    from harness.web import research
    monkeypatch.setattr(research, "load_snapshot", lambda persona: snap)
    r = TestClient(app).post("/shoes/recommend", json=body()).json()
    p = r["purchase"]
    assert r["candidatesFrom"] == "web-snapshot" and p["item"] == "Brand A Cushy"
    lost = {o["name"]: o.get("lostBecause") for o in p["options"]}
    assert "confirm a wide toe box" in lost["Brand B Unknown"] and "Low cushioning" in lost["Brand C Flat"]
    assert any(e["source"] == "Review (example.com)" for e in p["evidence"])


@pytest.mark.web
def test_web_pick_carries_image_url(no_sie, monkeypatch):
    from harness import store
    from harness.web import research
    monkeypatch.setattr(store, "cached_candidates", lambda persona: None)
    snap = research.load_snapshot("jordan")
    assert snap and all("imageUrl" in c for c in snap["candidates"])
    monkeypatch.setattr(research, "load_snapshot", lambda persona: snap)
    p = TestClient(app).post("/shoes/recommend", json=body()).json()["purchase"]
    assert p["imageUrl"] and p["imageUrl"] == p["options"][0]["imageUrl"] and p["imageUrl"].startswith("https://")
