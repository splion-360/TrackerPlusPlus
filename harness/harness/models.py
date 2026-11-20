"""Pydantic mirror of the Swift contract (app/TrackerPlusPlus/Contract/Contract.swift).
Field names match exactly; dates are ISO 8601 with an offset."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Rule(BaseModel):
    id: str
    text: str


class Evidence(BaseModel):
    source: str
    passage: str


class Option(BaseModel):
    name: str
    amountCents: int
    arrives: Optional[datetime] = None
    lostBecause: Optional[str] = None
    imageUrl: Optional[str] = None      # extra (not in the Swift type yet): product photo from the web


class Purchase(BaseModel):
    id: str
    item: str
    merchant: str
    amountCents: int
    status: Literal["awaitingApproval", "bought", "cancelled"]
    rule: Rule
    noticed: str
    evidence: list[Evidence]
    evidenceLive: bool
    options: list[Option]
    cancelBy: Optional[datetime] = None
    outcome: Optional[str] = None
    imageUrl: Optional[str] = None      # extra: the chosen shoe's photo (same as options[0].imageUrl)


class Mandate(BaseModel):
    id: str
    text: str
    category: Literal["dayPass", "any"]
    maxAutoCents: int


class ShoeState(BaseModel):
    boughtOn: date
    startMiles: float = 0


# --- /evidence (matches EvidenceClient.swift) ---

class EvidenceRequest(BaseModel):
    purchaseKind: str
    query: str


class EvidenceResponse(BaseModel):
    evidence: list[Evidence]
    live: bool


# --- /shoes/recommend ---

class ShoesRequest(BaseModel):
    persona: str
    now: datetime
    shoes: ShoeState
    raceDate: date
    mandate: list[Mandate] = Field(default_factory=list)


class Condition(BaseModel):
    km: float
    miles: int
    lifeKm: int
    percentLife: int
    status: Literal["fresh", "worn", "replace"]
    runsCounted: int
    why: str


class Profile(BaseModel):
    weeklyKm: float
    runsPerWeek: float
    longestRunKm: float
    avgPaceMinPerKm: Optional[float]
    indoorShare: float


class ShoesResponse(BaseModel):
    condition: Condition
    profile: Profile
    purchase: Optional[Purchase]
    live: bool          # true = SIE ranked and decided; false = deterministic fallback
    source: str         # "sie" | "rules" | "fresh"
    candidatesFrom: Optional[str] = None   # "web-singlestore" | "web-snapshot" | "catalog"
    auditId: Optional[str] = None          # SingleStore recommendations row, when logging is on
