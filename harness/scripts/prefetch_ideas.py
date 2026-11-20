"""Research small workout ideas on the web (Nimble + SIE) and save the snapshot.
PYTHONPATH=. uv run python scripts/prefetch_ideas.py"""
import time
from harness.web import workout_ideas as wi
t = time.monotonic()
snap = wi.research()
path = wi.save(snap)
print(f"{len(snap['pages'])} pages → {len(snap['ideas'])} ideas in {time.monotonic()-t:.1f}s → {path}")
for i in snap["ideas"]:
    print(f"- [{i['phase']}/{i['kind']}/{i['intensity']}] {i['title']} ({i['minutes']} min, {i['activityType']}) — {i['when']} ({i['sourceHost']})")
