"""Workout recommendations for the next 7 days, from health data, shoe condition and memory.

  health data (load, sleep, resting HR) + shoes + Tony's memory facts (fixed slots)
  → code builds the week: usual pattern, taper, readiness, shoe rules, load cap, busy times
  → SIE Qwen writes one reason per session (it cannot change the sessions)
  → guardrails → AgentActions the app puts on the calendar (+ SingleStore audit record)

Hard decisions stay in code so the plan is safe and repeatable; the model only explains."""
from __future__ import annotations

import concurrent.futures as cf
import re
import statistics as st
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

from pydantic import BaseModel, Field

from harness import data, store
from harness.clients import sie
from harness.models import ShoeState
from harness.recommend.condition import condition
from harness.web import workout_ideas

BUDGET_S = 5.0
DAYS = 7
LOAD_GROWTH = 1.10       # ≤110% of the larger of last week and the 4-week average (design doc D15)
ACWR_MAX = 1.30          # 7-day load ≤ 1.3 × 28-day weekly average (Gabbett "sweet spot")
SLEEP_DROP_MIN = 30      # 7-day sleep this far under the 28-day average → ease off
RHR_RISE = 3             # 7-day resting HR this far over the 28-day average → ease off
TAPER_DAYS = 10          # race within this many days → taper
WORN_RUN_MAX_MIN = 45    # worn-out shoes: runs longer than this move to the bike/elliptical
BREAK_IN_MIN = 30
MIN_SESSION = 20
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
NOUN = {"Running": "run", "Cycling": "bike ride", "Elliptical": "elliptical session", "Walking": "walk",
        "Yoga": "yoga", "FunctionalStrengthTraining": "strength session", "TraditionalStrengthTraining": "strength session"}
_pool = cf.ThreadPoolExecutor(max_workers=8)


class Busy(BaseModel):
    start: datetime
    end: datetime


class WorkoutsRequest(BaseModel):
    persona: str
    now: datetime
    shoes: Optional[ShoeState] = None
    newPairArrives: Optional[date] = None       # set once a shoe order is placed
    raceDate: Optional[date] = None
    busy: list[Busy] = Field(default_factory=list)


def effort(w: dict) -> float:
    """HealthKit effort if present, else an estimate from average heart rate (1–10)."""
    if w.get("effort"):
        return float(w["effort"])
    hr = w.get("avgHeartRate")
    return max(1.0, min(10.0, 1 + (hr - 90) / 10)) if hr else 5.0


def signals(ws: list[dict], days: list[dict], now: datetime) -> dict:
    last7 = [w for w in ws if w["start"] >= now - timedelta(days=7)]
    last28 = [w for w in ws if w["start"] >= now - timedelta(days=28)]
    load7 = sum(effort(w) * w["durationMin"] for w in last7)
    avg28 = sum(effort(w) * w["durationMin"] for w in last28) / 4
    cap = min(LOAD_GROWTH * max(load7, avg28), ACWR_MAX * avg28)
    def avg(rows, key):
        vals = [r[key] for r in rows if r.get(key)]
        return round(st.mean(vals), 1) if vals else None
    d7 = [d for d in days if d["date"] >= now.date() - timedelta(days=7)]
    d28 = [d for d in days if d["date"] >= now.date() - timedelta(days=28)]
    sig = {"load7": round(load7), "loadWeeklyAvg28": round(avg28), "loadRatio": round(load7 / avg28, 2) if avg28 else None,
           "loadCap": round(cap), "sleep7Min": avg(d7, "sleepMin"), "sleep28Min": avg(d28, "sleepMin"),
           "rhr7": avg(d7, "restingHeartRate"), "rhr28": avg(d28, "restingHeartRate")}
    tired = []
    if sig["sleep7Min"] and sig["sleep28Min"] and sig["sleep7Min"] < sig["sleep28Min"] - SLEEP_DROP_MIN:
        tired.append(f"sleep down {round(sig['sleep28Min'] - sig['sleep7Min'])} min/night")
    if sig["rhr7"] and sig["rhr28"] and sig["rhr7"] > sig["rhr28"] + RHR_RISE:
        tired.append(f"resting HR up {round(sig['rhr7'] - sig['rhr28'], 1)} bpm")
    sig["readiness"] = "ease off: " + ", ".join(tired) if tired else "normal"
    return sig


def memory_slots(mem: dict | None) -> dict[tuple[str, str], int]:
    """{(weekday, activity): hour} from 'Fixed slot ... running on Tue between 18:00 and 19:00 ...' facts."""
    slots = {}
    for f in (mem or {}).get("facts", []):
        m = re.search(r"Fixed slot among recognised activities: (\w[\w ]*?) on (\w{3}) between (\d\d):00", f["text"])
        if m:
            act = {"running": "Running", "walking": "Walking", "cycling": "Cycling", "yoga": "Yoga",
                   "strength training": "FunctionalStrengthTraining"}.get(m.group(1).lower(), m.group(1))
            slots[(m.group(2), act)] = int(m.group(3))
    return slots


def usual_week(ws: list[dict], now: datetime) -> dict[int, dict]:
    """Per weekday: the usual activity, minutes and start hour over the last 4 weeks (days with ≥2 weeks of use)."""
    by_day: dict[int, list[dict]] = defaultdict(list)
    for w in ws:
        if w["start"] >= now - timedelta(days=28):
            by_day[w["start"].weekday()].append(w)
    week = {}
    for wd, rows in by_day.items():
        if len({r["start"].date() for r in rows}) < 2:
            continue
        act = Counter(r["activityType"] for r in rows).most_common(1)[0][0]
        same = [r for r in rows if r["activityType"] == act]
        week[wd] = {"activityType": act, "minutes": round(st.median(r["durationMin"] for r in same)),
                    "hour": round(st.median(r["start"].hour for r in same)),
                    "effort": st.mean(effort(r) for r in same), "label": same[-1].get("label", "")}
    return week


def build(req: WorkoutsRequest) -> dict:
    now = req.now
    # Whole-week shift so weekdays match the history (and Tony's memory slots, computed on the same data).
    ws = data.workouts(req.persona, now, keep_weekdays=True)
    sig = signals(ws, data.daily(req.persona, now, keep_weekdays=True), now)
    mem = data.memory(req.persona)
    slots = memory_slots(mem)
    week = usual_week(ws, now)
    cond = condition(ws, req.shoes, now) if req.shoes else None
    worn = bool(cond and cond.status == "replace")
    long_day = max((wd for wd, s in week.items() if s["activityType"] == "Running"), key=lambda wd: week[wd]["minutes"], default=None)
    has_bike = any(w["activityType"] == "Cycling" for w in ws)
    eased, broken_in = False, 0
    sessions = []
    for i in range(1, DAYS + 1):
        day = (now + timedelta(days=i)).date()
        wd = day.weekday()
        if req.raceDate and day == req.raceDate:
            sessions.append({"date": day, "activityType": "Running", "minutes": 0, "hour": 8, "tags": ["race day"], "race": True})
            continue
        if req.raceDate and day == req.raceDate - timedelta(days=1):
            continue                                    # rest the day before the race
        usual = week.get(wd)
        if not usual:
            continue
        s = {"date": day, "activityType": usual["activityType"], "minutes": usual["minutes"], "effort": usual["effort"],
             "hour": slots.get((WEEKDAYS[wd], usual["activityType"]), usual["hour"]), "tags": [], "usual": usual["label"]}
        if (WEEKDAYS[wd], usual["activityType"]) in slots:
            s["tags"].append(f"your usual {WEEKDAYS[wd]} {s['hour']:02d}:00 slot (memory)")
        if wd == long_day:
            s["tags"].append("long run")
        days_to_race = (req.raceDate - day).days if req.raceDate else None
        if days_to_race is not None and 0 < days_to_race <= TAPER_DAYS and s["activityType"] == "Running":
            f = 0.6 if wd == long_day else 0.8
            s["minutes"] = max(MIN_SESSION, round(s["minutes"] * f))
            s["tags"].append(f"taper: race in {days_to_race} days")
        if sig["readiness"] != "normal" and not eased and s["effort"] >= 6:
            s["minutes"] = max(MIN_SESSION, round(s["minutes"] * 0.8))
            s["effort"] = min(s["effort"], 4.0)
            s["tags"].append(sig["readiness"] + " → easy")
            eased = True
        if s["activityType"] == "Running":
            arrived = req.newPairArrives and day > req.newPairArrives
            if worn and not arrived and s["minutes"] > WORN_RUN_MAX_MIN:
                s["activityType"] = "Cycling" if has_bike else "Elliptical"
                s["tags"].append(f"shoes at {cond.percentLife}% → off the road")
            elif arrived and broken_in < 2:
                s["minutes"] = min(s["minutes"], BREAK_IN_MIN)
                s["effort"] = min(s["effort"], 4.0)
                s["tags"].append("break-in run in the new pair")
                broken_in += 1
        sessions.append(s)

    # Load cap: shrink the non-long sessions proportionally until the week fits.
    planned = sum(s["effort"] * s["minutes"] for s in sessions if not s.get("race"))
    if planned > sig["loadCap"] > 0:
        flex = [s for s in sessions if not s.get("race") and "long run" not in s["tags"]]
        excess = planned - sig["loadCap"]
        flex_load = sum(s["effort"] * s["minutes"] for s in flex) or 1
        for s in flex:
            cut = s["minutes"] * min(0.5, excess / flex_load)
            if cut >= 1:
                s["minutes"] = max(MIN_SESSION, round(s["minutes"] - cut))
                s["tags"].append("trimmed to stay under your weekly load cap")
    sig["plannedLoad"] = round(sum(s["effort"] * s["minutes"] for s in sessions if not s.get("race")))

    # Busy times: move a session to just after an overlapping event (same day, before 21:00).
    tz = now.tzinfo
    for s in sessions:
        start = datetime.combine(s["date"], datetime.min.time(), tzinfo=tz).replace(hour=s["hour"])
        end = start + timedelta(minutes=max(s["minutes"], 1))
        for b in req.busy:
            if b.start < end and start < b.end:
                start = b.end.astimezone(tz)
                end = start + timedelta(minutes=max(s["minutes"], 1))
                s["tags"].append("moved after a calendar conflict")
        if end.hour >= 21 and not s.get("race"):
            s["tags"].append("no free slot that day")
        s["start"], s["end"] = start, end
    return {"sessions": sessions, "signals": sig, "condition": cond, "memory": mem}


def _template(s: dict) -> str:
    if s.get("race"):
        return "Race day. Rest and fuel."
    noun = NOUN.get(s["activityType"], s["activityType"].lower())
    why = "; ".join(s["tags"]) if s["tags"] else f"your usual {WEEKDAYS[s['date'].weekday()]} {noun}"
    return f"{s['minutes']} min {noun}: {why}."[:160]


ONE_REASON = {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"],
              "additionalProperties": False}


def _context(plan: dict) -> str:
    sig = plan["signals"]
    mem = "\n".join(f"- {f['text']}" for f in (plan["memory"] or {}).get("facts", [])
                    if f.get("kind") in ("regularity", "load", "body"))[:1000]
    return (f"Load last 7 days {sig['load7']} vs 4-week weekly average {sig['loadWeeklyAvg28']} (ratio {sig['loadRatio']}); "
            f"weekly cap {sig['loadCap']}, planned {sig['plannedLoad']}. Sleep {sig['sleep7Min']} vs {sig['sleep28Min']} min; "
            f"resting HR {sig['rhr7']} vs {sig['rhr28']}; readiness {sig['readiness']}."
            + (f" Shoes at {plan['condition'].percentLife}% of their life." if plan["condition"] else "")
            + f"\nMemory:\n{mem}")


def _reasons(plan: dict, ids: list[str], timeout: float) -> dict[str, str]:
    """One small model call per session, all in parallel (one long answer is too slow)."""
    ctx = _context(plan)

    def one(s: dict) -> str:
        prompt = (
            "Write one sentence under 15 words telling a runner why this planned session looks the way it does. "
            "Speak to them as 'your'. Use only the rules and numbers given; the session is fixed.\n\n"
            f"{ctx}\n\nSession: {s['date']:%a %b %d} {s['start']:%H:%M}, {s['activityType']}, {s['minutes']} min. "
            f"Rules applied: {', '.join(s['tags']) or 'their usual session for this weekday'}.\n\nAnswer in JSON."
        )
        r = sie.chat_json(prompt, ONE_REASON, "session_reason", 80)["reason"].strip()
        if not r or len(r) > 160:
            raise ValueError("bad reason")
        return r

    todo = [(i, s) for i, s in zip(ids, plan["sessions"]) if not s.get("race")]
    futures = {i: _pool.submit(one, s) for i, s in todo}
    deadline = time.monotonic() + timeout
    out = {i: f.result(timeout=max(0.1, deadline - time.monotonic())) for i, f in futures.items()}
    out.update({i: _template(s) for i, s in zip(ids, plan["sessions"]) if s.get("race")})
    return out


SAFE_KINDS = {"strides", "shakeout", "mobility", "drills", "walk"}
DEFAULT_MIN = {"strides": 6, "shakeout": 15, "mobility": 10, "drills": 10, "walk": 15}
ADDON_EFFORT = 3.0
MAX_SUGGESTIONS = 2


def _phases(req: WorkoutsRequest, plan: dict) -> set[str]:
    ph = {"any"}
    first = (req.now + timedelta(days=1)).date()
    if req.raceDate and 0 < (req.raceDate - first).days <= TAPER_DAYS:
        ph |= {"taper", "race week"}
    if plan["condition"] and plan["condition"].status == "replace":
        ph.add("worn shoes")
    if req.newPairArrives:
        ph.add("new shoes")
    return ph


def suggestions(req: WorkoutsRequest, plan: dict, ids: list[str]) -> list[dict]:
    """Optional extras from the web ideas, filtered by code rules, at most two, never booked automatically."""
    sig, sessions = plan["signals"], plan["sessions"]
    headroom = sig["loadCap"] - sig["plannedLoad"]
    phases = _phases(req, plan)
    worn = bool(plan["condition"] and plan["condition"].status == "replace")
    runs = [(i, s) for i, s in zip(ids, sessions) if s["activityType"] == "Running" and not s.get("race")]
    out, used_kinds = [], set()
    for idea in sorted(workout_ideas.load(), key=lambda x: (x["phase"] == "any", x["kind"] == "mobility")):
        if len(out) >= MAX_SUGGESTIONS:
            break
        kind = idea["kind"]
        if kind not in SAFE_KINDS or idea["intensity"] == "hard" or idea["phase"] not in phases or kind in used_kinds:
            continue
        minutes = int(idea["minutes"] or DEFAULT_MIN[kind])
        if minutes > 30 or ADDON_EFFORT * minutes > headroom:
            continue
        attach = None
        if kind in ("strides", "shakeout", "drills"):
            ok = [(i, s) for i, s in runs if not (worn and req.newPairArrives and s["date"] <= req.newPairArrives)
                  and not (worn and not req.newPairArrives)]
            if kind == "shakeout" and req.raceDate:
                ok = [(i, s) for i, s in ok if s["date"] == req.raceDate - timedelta(days=2)] or []
            if not ok:
                continue
            attach = ok[-1] if kind == "strides" else ok[0]
        when = f"after your {attach[1]['date']:%a} run" if attach else idea["when"]
        out.append({"id": idea["id"], "title": idea["title"], "kind": kind, "activityType": idea["activityType"],
                    "minutes": minutes, "when": when, "attachTo": attach[0] if attach else None,
                    "why": _why(idea, req),
                    "source": {"title": idea["sourceTitle"], "host": idea["sourceHost"], "url": idea["url"]},
                    "quote": idea["quote"], "optional": True})
        used_kinds.add(kind)
        headroom -= ADDON_EFFORT * minutes
    return out


def _why(idea: dict, req: WorkoutsRequest) -> str:
    lead = {"taper": f"Your race is {(req.raceDate - req.now.date()).days} days out" if req.raceDate else "You're tapering",
            "race week": f"Race week: your race is {(req.raceDate - req.now.date()).days} days out" if req.raceDate else "Race week",
            "new shoes": f"Your new pair arrives {req.newPairArrives:%a}" if req.newPairArrives else "New shoes",
            "worn shoes": "Your shoes are worn out", "any": "An easy add-on"}[idea["phase"]]
    return f"{lead}; {idea['sourceHost']} suggests it {idea['when'].rstrip('.')}. Optional, and it fits your weekly load."


def citations(session: dict) -> list[dict]:
    """A web source behind a rule the session applied (taper, worn shoes, break-in), when one was found."""
    want = []
    tags = " ".join(session["tags"])
    if "taper" in tags:
        want.append("taper")
    if "off the road" in tags:
        want.append("worn shoes")
    if "break-in" in tags:
        want.append("new shoes")
    ideas = workout_ideas.load()
    out = []
    for phase in want:
        hit = next((i for i in ideas if i["phase"] == phase or (phase == "worn shoes" and i["kind"] == "cross-training")), None)
        if hit:
            out.append({"source": f"{hit['sourceTitle']} ({hit['sourceHost']})", "passage": hit["quote"], "url": hit["url"]})
    return out


def recommend(req: WorkoutsRequest) -> dict:
    t0 = time.monotonic()
    plan = build(req)
    sessions, sig = plan["sessions"], plan["signals"]
    ids = [f"wk-{s['date'].isoformat()}-{s['activityType'].lower()}" for s in sessions]
    source = "rules"
    try:
        reasons = _reasons(plan, ids, max(0.5, BUDGET_S - (time.monotonic() - t0)))
        source = "sie"
    except Exception:
        reasons = {i: _template(s) for i, s in zip(ids, sessions)}
    swaps = sum(1 for s in sessions if any("off the road" in t for t in s["tags"]))
    summary = (f"Your last 7 days carried {sig['loadRatio']}× your usual training load, so this week is planned at "
               f"{sig['plannedLoad']} (cap {sig['loadCap']}). Readiness: {sig['readiness']}."
               + (f" {swaps} long session{'s' if swaps > 1 else ''} moved off the road while your shoes are worn." if swaps else ""))
    actions = []
    for i, s in zip(ids, sessions):
        noun = NOUN.get(s["activityType"], s["activityType"].lower())
        title = "Race day" if s.get("race") else f"Planned a {s['minutes']}-minute {noun}"
        actions.append({"id": i, "kind": "note" if s.get("race") else "addWorkout", "title": title, "planItemId": i,
                        "after": None if s.get("race") else {"start": s["start"].isoformat(), "end": s["end"].isoformat(),
                                                             "activityType": s["activityType"]},
                        "reason": reasons[i], "points": None if s.get("race") else 10,
                        "createdAt": req.now.isoformat(), "rules": s["tags"], "evidence": citations(s)})
    mem = plan["memory"] or {}
    used = [f["text"] for f in mem.get("facts", []) if f.get("kind") in ("regularity", "load", "body")]
    result = {"summary": summary, "signals": sig, "actions": actions, "suggestions": suggestions(req, plan, ids),
              "memoryUsed": used,
              "memoryAsOf": mem.get("asOf"), "live": source == "sie", "source": source,
              "condition": plan["condition"].model_dump() if plan["condition"] else None}
    result["auditId"] = store.log_recommendation(req.persona, source, "week plan", int((time.monotonic() - t0) * 1000),
                                                 {"kind": "workouts", "request": req.model_dump(mode="json"), **result})
    return result
