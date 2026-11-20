"""Web workout ideas: Nimble searches expert guidance, SIE Qwen extracts small session ideas with sources.

Prefetched like the shoe candidates (scripts/prefetch_ideas.py), never fetched per request. The workout
recommender filters them with code rules and offers at most two as optional suggestions."""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from harness.clients import sie
from harness.web.research import nimble_search

SNAPSHOT_DIR = Path(__file__).resolve().parents[1] / "data" / "workout_ideas"
QUERIES = [
    "half marathon taper week workouts strides shakeout run",
    "running drills and mobility routine before a race",
    "cross training for runners when running shoes are worn out",
    "how to break in new running shoes before a race",
]
KINDS = ["strides", "shakeout", "mobility", "drills", "easy run", "walk", "cross-training", "strength", "rest", "other"]
EXTRACT = {"type": "object", "properties": {"ideas": {"type": "array", "items": {"type": "object", "properties": {
    "title": {"type": "string"},
    "kind": {"type": "string", "enum": KINDS},
    "activityType": {"type": "string", "enum": ["Running", "Walking", "Cycling", "Elliptical", "Yoga", "FunctionalStrengthTraining", "Other"]},
    "minutes": {"type": ["number", "null"]},
    "intensity": {"type": "string", "enum": ["very easy", "easy", "moderate", "hard"]},
    "phase": {"type": "string", "enum": ["taper", "race week", "any", "worn shoes", "new shoes"]},
    "when": {"type": "string"},
    "quote": {"type": "string"}},
    "required": ["title", "kind", "activityType", "minutes", "intensity", "phase", "when", "quote"],
    "additionalProperties": False}}}, "required": ["ideas"], "additionalProperties": False}


def _extract(page: dict) -> list[dict]:
    text = (page.get("description") or page.get("content") or "")[:4000]
    if len(text) < 200:
        return []
    prompt = (
        "From this running article, list up to 4 concrete, small workout ideas a runner could add to a week "
        "(e.g. strides after an easy run, a 15-minute mobility routine, a shakeout jog). Only use what the page says; "
        "minutes null if not stated. 'when' = when to do it, as the page says (e.g. 'after an easy run', '2 days before the race'). "
        "quote: one short sentence copied from the page (under 30 words).\n\n"
        f"Title: {page.get('title')}\n\n{text}\n\nAnswer in JSON."
    )
    out = sie.chat_json(prompt, EXTRACT, "workout_ideas", 900, provision_timeout_s=60)
    host = urlparse(page.get("url", "")).netloc.removeprefix("www.")
    return [{**i, "sourceTitle": page.get("title", host), "sourceHost": host, "url": page.get("url", "")} for i in out["ideas"]]


def research() -> dict:
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        pages = [p for g in pool.map(lambda q: nimble_search(q, 4), QUERIES) for p in g]
        found = list(pool.map(lambda p: _safe(_extract, p), pages))
    seen, ideas = set(), []
    for group in found:
        for i in group:
            key = re.sub(r"[^a-z0-9]+", "-", i["title"].lower()).strip("-")
            if key and key not in seen:
                seen.add(key)
                ideas.append({"id": f"idea-{key}"[:80], **i})
    return {"fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "queries": QUERIES,
            "pages": [{"title": p.get("title"), "url": p.get("url")} for p in pages], "ideas": ideas}


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return []


def save(snap: dict) -> Path:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / "running.json"
    path.write_text(json.dumps(snap, indent=1))
    return path


def load() -> list[dict]:
    try:
        return json.loads((SNAPSHOT_DIR / "running.json").read_text()).get("ideas", [])
    except Exception:
        return []
