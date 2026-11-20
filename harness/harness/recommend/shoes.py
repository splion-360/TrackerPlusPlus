"""Shoe recommendation workflow.

  ① fake data → ② profile + ③ wear (code) → ④ hard filters (code) + SIE rerank
  → ⑤ SIE chat decides and explains (JSON schema) → ⑥ guardrails (code) → ⑦ Purchase

Any SIE failure, timeout or guardrail failure falls back to the deterministic choice, so the
route always answers within its budget."""
from __future__ import annotations

import concurrent.futures as cf
import time
from datetime import datetime, timedelta

from harness import data, store
from harness.web import research
from harness.clients import sie
from harness.evidence import senso
from harness.models import Evidence, Mandate, Option, Purchase, Rule, ShoesRequest, ShoesResponse
from harness.recommend.condition import condition, profile

BUDGET_S = 5.0           # whole route; the phone's RecommendClient waits 6 s
BREAK_IN_DAYS = 3        # a new pair must arrive at least this long before race day
RACE_WINDOW_DAYS = 14
LONG_RUN_KM = 15         # long runs at or above this need real cushioning
DECIDE_TOP = 4           # shortlist size the model chooses from (keeps its answer short and fast)
MERCHANT = "Demo store, no charge"
_pool = cf.ThreadPoolExecutor(max_workers=4)

RERANK_INSTRUCTION = "Given a runner's training profile and needs, score how well this running shoe fits them."


def _money(cents: int) -> str:
    return f"${cents / 100:.0f}"


def candidates(persona: str) -> tuple[list[dict], str]:
    """Web research from SingleStore, else the deployed snapshot, else the fake catalog."""
    rows = store.cached_candidates(persona)          # memory only: the database is never on the request path
    if rows:
        return rows, "web-singlestore"
    store.refresh_candidates_async(persona)
    snap = research.load_snapshot(persona)
    if snap:
        return snap["candidates"], "web-snapshot"
    return data.catalog(), "catalog"


def _filter(shoes: list[dict], fit: dict, day: datetime, race: datetime, long_run_km: float = 0) -> tuple[list[dict], list[dict]]:
    """Hard rules stay in code. Returns (eligible, rejected with lostBecause)."""
    latest = race - timedelta(days=BREAK_IN_DAYS)
    ok, lost = [], []
    for s in shoes:
        arrives = day + timedelta(days=s["arrivesInDays"])
        s = {**s, "arrives": arrives}
        if fit.get("wideToeBox") and s["wideToeBox"] is None:
            s["lostBecause"] = "Reviews don't confirm a wide toe box (your fit notes say wide)"
        elif fit.get("wideToeBox") and not s["wideToeBox"]:
            s["lostBecause"] = "Narrow toe box (your fit notes say wide)"
        elif fit.get("carbonPlate") is False and s["carbonPlate"]:
            s["lostBecause"] = "Carbon plate (your fit notes say no plate)"
        elif fit.get("surface") and s["surface"] != fit["surface"]:
            s["lostBecause"] = f"Built for {s['surface']}; you run on {fit['surface']}"
        elif long_run_km >= LONG_RUN_KM and s["cushion"] == "low":
            s["lostBecause"] = f"Low cushioning for your {long_run_km} km long runs"
        elif long_run_km >= LONG_RUN_KM and (s.get("dropMm") or 8) <= 2:
            s["lostBecause"] = "Zero-drop: too big a change right before a race"
        elif arrives > latest:
            days = (race.date() - arrives.date()).days
            s["lostBecause"] = f"Arrives {arrives:%A}, {days} days before the race: no time to break in"
        (lost if "lostBecause" in s else ok).append(s)
    return ok, lost


def _need(p, cond, fit, days_to_race) -> str:
    return (f"Runner does {p.weeklyKm} km a week over {p.runsPerWeek} runs, longest run {p.longestRunKm} km, "
            f"average pace {p.avgPaceMinPerKm} min/km, race in {days_to_race} days. Current pair at "
            f"{cond.percentLife}% of its life. Fit notes: {fit.get('text')}")


def _fallback_pick(eligible: list[dict], p) -> tuple[dict, dict[str, str]]:
    """Deterministic: long runs ≥15 km want high cushion; then cheapest."""
    want = "high" if p.longestRunKm >= 15 else None
    pool = [s for s in eligible if s["cushion"] == want] or eligible
    chosen = min(pool, key=lambda s: s["amountCents"])
    lost = {}
    for s in eligible:
        if s is chosen:
            continue
        if want and s["cushion"] != want:
            lost[s["id"]] = f"{s['cushion'].capitalize()} cushioning for {p.longestRunKm} km long runs"
        else:
            lost[s["id"]] = f"Costs more than {chosen['name']}"
    return chosen, lost


def _decide_schema(ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "chosenId": {"type": "string", "enum": ids},
            "whyChosen": {"type": "string"},
            "lost": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string", "enum": ids}, "lostBecause": {"type": "string"}},
                "required": ["id", "lostBecause"], "additionalProperties": False}},
        },
        "required": ["chosenId", "whyChosen", "lost"],
        "additionalProperties": False,
    }


def _decide_prompt(need: str, eligible: list[dict], scores: dict[str, float]) -> str:
    rows = "\n".join(
        f"- id={s['id']} name={s['name']} price={_money(s['amountCents'])} cushion={s['cushion']} drop={s['dropMm']}mm "
        f"life={s['lifeKm']}km arrives={s['arrives']:%a %b %d} fitScore={scores.get(s['id'], 0):.2f} :: {s['blurb']}"
        for s in eligible)
    return (
        "You pick one replacement running shoe for a runner. Every shoe below already passed the hard rules "
        "(fit notes, arrives in time to break in before the race). Choose the shoe that best fits how they train: "
        "cushioning and durability for their weekly distance and long runs come first, a familiar daily-trainer feel "
        "before a race second, price last (only to break a near tie). For every other shoe give one short reason it "
        "lost (under 10 words, factual, no second person pronoun except 'your'). whyChosen: one sentence under 25 words, speaking to the runner as 'your' (never 'the runner').\n\n"
        f"Runner: {need}\n\nShoes:\n{rows}\n\nAnswer in JSON."
    )


def _guard(answer: dict, eligible: list[dict]) -> dict[str, str]:
    """Reject anything off the shortlist, missing a reason, or too long. Returns {id: lostBecause}."""
    ids = {s["id"] for s in eligible}
    if answer.get("chosenId") not in ids:
        raise ValueError("chosen shoe is not on the shortlist")
    lost = {l["id"]: l["lostBecause"].strip() for l in answer.get("lost", []) if l.get("id") in ids}
    lost.pop(answer["chosenId"], None)
    if set(lost) != ids - {answer["chosenId"]}:
        raise ValueError("a shortlisted shoe has no reason")
    if any(not r or len(r) > 120 for r in lost.values()) or not 0 < len(answer.get("whyChosen", "")) <= 220:
        raise ValueError("reason empty or too long")
    return lost


def recommend(req: ShoesRequest) -> ShoesResponse:
    t0 = time.monotonic()
    left = lambda: max(0.1, BUDGET_S - (time.monotonic() - t0))
    now = req.now
    ws = data.workouts(req.persona, now)
    prof = profile(ws, now)
    cond = condition(ws, req.shoes, now)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    race = datetime.combine(req.raceDate, datetime.min.time(), tzinfo=now.tzinfo)
    days_to_race = (req.raceDate - now.date()).days

    if cond.status != "replace" or not 0 <= days_to_race <= RACE_WINDOW_DAYS:
        return ShoesResponse(condition=cond, profile=prof, purchase=None, live=False, source="fresh")

    fit = data.fit_notes(req.persona)
    pool, cand_source = candidates(req.persona)
    eligible, rejected = _filter(pool, fit, day, race, prof.longestRunKm)
    if not eligible:
        return ShoesResponse(condition=cond, profile=prof, purchase=None, live=False, source="rules")
    need = _need(prof, cond, fit, days_to_race)

    # Guide and fit-note evidence runs alongside the models; the chosen listing is added after.
    ev_future = _pool.submit(senso.search_many, "shoes", [
        ("How many miles before replacing running shoes, and how to break in a new pair before a race", 2),
        (f"{req.persona.capitalize()}'s fit notes: toe box, carbon plate, surface", 1),
    ])

    source, why, lost_map = "rules", None, None
    try:
        scores = _pool.submit(sie.rerank, need, {s["id"]: f"{s['name']}: {s['blurb']}" for s in eligible},
                              RERANK_INSTRUCTION).result(timeout=left())
        ranked = sorted(eligible, key=lambda s: -scores[s["id"]])
        top, rest = ranked[:DECIDE_TOP], ranked[DECIDE_TOP:]
        answer = _pool.submit(sie.chat_json, _decide_prompt(need, top, scores), _decide_schema([s["id"] for s in top]),
                              "shoe_pick", 450).result(timeout=left())
        lost_map = _guard(answer, top)
        lost_map.update({s["id"]: f"Ranked below the top {DECIDE_TOP} for your mileage and long runs" for s in rest})
        chosen = next(s for s in top if s["id"] == answer["chosenId"])
        why, source = answer["whyChosen"].strip(), "sie"
    except Exception:
        chosen, lost_map = _fallback_pick(eligible, prof)

    try:
        evidence, ev_live = ev_future.result(timeout=left())
    except Exception:
        evidence, ev_live = [Evidence(**p) for p in data.passages("shoes")], False
    if chosen.get("web"):
        evidence = evidence + [Evidence(source=f"{chosen['sourceTitle']} ({chosen['sourceHost']})", passage=chosen["quote"])]
    listing = next((e for e in evidence if e.source == f"{chosen['name']} listing"), None)
    evidence = evidence + ([] if listing or chosen.get("web") else [Evidence(source=f"{chosen['name']} listing",
                                    passage=f"{chosen['blurb']} {_money(chosen['amountCents'])}. Arrives {chosen['arrives']:%a %b %d}. Free returns for 30 days.")])

    rule = next((m for m in req.mandate if m.category == "any"), None) or Mandate(
        id="ask-first", text="Ask me before buying anything.", category="any", maxAutoCents=0)
    others = sorted((s for s in eligible if s is not chosen), key=lambda s: s["id"] not in lost_map or "Ranked below" in lost_map[s["id"]])
    options = [Option(name=chosen["name"], amountCents=chosen["amountCents"], arrives=chosen["arrives"], imageUrl=chosen.get("imageUrl"))]
    options += [Option(name=s["name"], amountCents=s["amountCents"], arrives=s["arrives"], lostBecause=lost_map[s["id"]],
                       imageUrl=s.get("imageUrl")) for s in others]
    options += [Option(name=s["name"], amountCents=s["amountCents"], arrives=s["arrives"], lostBecause=s["lostBecause"],
                       imageUrl=s.get("imageUrl")) for s in rejected]

    noticed = f"Your shoes hit {cond.miles} miles ({cond.percentLife}% of their life). Race day is in {days_to_race} days."
    if why:
        noticed += f" {why}"
    purchase = Purchase(
        id=f"shoes-{req.shoes.boughtOn.isoformat()}", item=chosen["name"], merchant=MERCHANT,
        amountCents=chosen["amountCents"],
        status="bought" if chosen["amountCents"] <= rule.maxAutoCents else "awaitingApproval",
        rule=Rule(id=rule.id, text=rule.text), noticed=noticed,
        evidence=evidence, evidenceLive=ev_live, options=options, imageUrl=chosen.get("imageUrl"),
    )
    audit_id = store.log_recommendation(req.persona, source, chosen["name"], int((time.monotonic() - t0) * 1000), {
        "request": req.model_dump(mode="json"), "condition": cond.model_dump(), "profile": prof.model_dump(),
        "candidatesFrom": cand_source, "shortlist": [s["id"] for s in eligible],
        "rejected": {s["id"]: s["lostBecause"] for s in rejected}, "purchase": purchase.model_dump(mode="json"),
        "sources": [{"name": c["name"], "url": c.get("url")} for c in [chosen] if c.get("url")],
    })
    return ShoesResponse(condition=cond, profile=prof, purchase=purchase, live=source == "sie", source=source,
                         candidatesFrom=cand_source, auditId=audit_id)
