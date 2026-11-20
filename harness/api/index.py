"""TrackerPlusPlus harness: stateless FastAPI app (Vercel entry point)."""
from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, HTTPException

from harness import config, store
from harness.clients import sie
from harness.evidence import senso
from harness.models import EvidenceRequest, EvidenceResponse, ShoesRequest, ShoesResponse
from harness.recommend import shoes, workouts

app = FastAPI(title="TrackerPlusPlus harness", version="0.1.0")


def require_secret(x_harness_secret: str = Header(default="")) -> None:
    """Only enforced when HARNESS_SECRET is set. /evidence stays open: EvidenceClient sends no header."""
    if config.HARNESS_SECRET and not hmac.compare_digest(x_harness_secret, config.HARNESS_SECRET):
        raise HTTPException(status_code=401, detail="bad secret")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "sie": bool(config.SIE_API_KEY), "senso": bool(config.SENSO_API_KEY),
            "nimble": bool(__import__("os").environ.get("NIMBLE_API_KEY")), "singlestore": store.enabled(),
            "secret": bool(config.HARNESS_SECRET), "version": app.version}


@app.post("/warm", dependencies=[Depends(require_secret)])
def warm() -> dict:
    return {**sie.warm(provision_timeout_s=240), **store.warm()}


@app.post("/workouts/recommend", dependencies=[Depends(require_secret)])
def workouts_recommend(req: workouts.WorkoutsRequest) -> dict:
    """The next 7 days: health data (load, sleep, resting HR) + shoe condition + memory → calendar-ready actions."""
    try:
        return workouts.recommend(req)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown persona {req.persona}")


@app.post("/shoes/prefetch", dependencies=[Depends(require_secret)])
def shoes_prefetch(persona: str = "jordan") -> dict:
    """Research shoes on the web now (Nimble + SIE, ~30 s) and store them in SingleStore.
    Vercel can't write files, so without SINGLESTORE_URL this only reports what it found;
    the deployed snapshot (scripts/prefetch_web.py, committed) stays the source."""
    from harness.web import research
    snap = research.research(persona)
    return {"persona": persona, "pages": len(snap["pages"]), "candidates": len(snap["candidates"]),
            "fetchedAt": snap["fetchedAt"], "singlestore": store.save_candidates(snap)}


@app.get("/audit/recent", dependencies=[Depends(require_secret)])
def audit_recent(persona: str | None = None, limit: int = 20) -> dict:
    """The latest recommendations from SingleStore: what the agent decided, from what, and why."""
    if not store.enabled():
        raise HTTPException(status_code=503, detail="audit log off (no SINGLESTORE_URL)")
    try:
        return {"recommendations": store.recent_recommendations(persona, limit)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"singlestore: {type(exc).__name__}")


@app.get("/audit/{rid}", dependencies=[Depends(require_secret)])
def audit_get(rid: str) -> dict:
    """One full audit record by the auditId a recommendation returned."""
    if not store.enabled():
        raise HTTPException(status_code=503, detail="audit log off (no SINGLESTORE_URL)")
    try:
        rec = store.get_recommendation(rid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"singlestore: {type(exc).__name__}")
    if not rec:
        raise HTTPException(status_code=404, detail="no such recommendation")
    return rec


@app.post("/evidence", response_model=EvidenceResponse)
def evidence(req: EvidenceRequest) -> EvidenceResponse:
    items, live = senso.search(req.purchaseKind, req.query)
    return EvidenceResponse(evidence=items, live=live)


@app.post("/shoes/recommend", response_model=ShoesResponse, response_model_exclude_none=True,
          dependencies=[Depends(require_secret)])
def shoes_recommend(req: ShoesRequest) -> ShoesResponse:
    try:
        return shoes.recommend(req)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown persona {req.persona}")


@app.api_route("/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
def not_found(rest: str):
    raise HTTPException(status_code=404, detail=f"no route /{rest}")
