"""Fake data: persona fixtures (trimmed from MockData), shoe catalog, fit notes, cached passages."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent


@lru_cache
def _load(name: str):
    return json.loads((DIR / name).read_text())


def catalog() -> list[dict]:
    return _load("catalog.json")


def fit_notes(persona: str) -> dict:
    return _load("fit_notes.json").get(persona, {"wideToeBox": False, "carbonPlate": None, "surface": "road", "text": "No fit notes."})


def passages(kind: str) -> list[dict]:
    return _load("passages.json").get(kind, [])


def _shift(d: dict, now: datetime, keep_weekdays: bool) -> timedelta:
    days = (now.date() - datetime.fromisoformat(d["exportDate"]).date()).days
    return timedelta(days=(days // 7) * 7 if keep_weekdays else days)


def workouts(persona: str, now: datetime, keep_weekdays: bool = False) -> list[dict]:
    """The persona's workouts, re-anchored like the app does: the history ends the day before `now`.
    Each workout gets parsed `start`/`end` datetimes in now's offset; workouts after `now` are dropped."""
    path = DIR / "personas" / f"{persona}.json"
    if not path.exists():
        raise KeyError(persona)
    d = _load(f"personas/{persona}.json")
    shift = _shift(d, now, keep_weekdays)
    out = []
    for w in d["workouts"]:
        start = datetime.fromisoformat(w["start"]) + shift
        end = datetime.fromisoformat(w["end"]) + shift
        if end <= now:
            out.append({**w, "start": start, "end": end})
    return out


def daily(persona: str, now: datetime, keep_weekdays: bool = False) -> list[dict]:
    """Daily summaries (sleep minutes, resting HR), re-anchored like workouts; days before now only."""
    d = _load(f"personas/{persona}.json")
    shift = _shift(d, now, keep_weekdays)
    out = []
    for s in d.get("dailySummaries", []):
        day = datetime.fromisoformat(s["date"]).date() + shift
        if day < now.date():
            out.append({**s, "date": day})
    return out


def memory(persona: str) -> dict | None:
    """Facts and planner text from Tony's memory agent (scripts/build_memory.mjs)."""
    try:
        return _load(f"memory/{persona}.json")
    except FileNotFoundError:
        return None
