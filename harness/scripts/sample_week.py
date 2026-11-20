"""Run /workouts/recommend for Jordan in-process. uv run python scripts/sample_week.py [--offline]"""
import json, sys, time
from fastapi.testclient import TestClient
if "--offline" in sys.argv:
    from harness.clients import sie
    sie.chat_json = lambda *a, **k: (_ for _ in ()).throw(sie.SIEUnavailable("offline"))
from api.index import app
from harness import config
body = {"persona": "jordan", "now": "2026-11-20T09:00:00-08:00", "shoes": {"boughtOn": "2026-10-22", "startMiles": 267},
        "raceDate": "2026-11-29", "newPairArrives": "2026-11-23",
        "busy": [{"start": "2026-11-24T17:30:00-08:00", "end": "2026-11-24T19:00:00-08:00"}]}
t = time.monotonic()
r = TestClient(app).post("/workouts/recommend", json=body, headers={"X-Harness-Secret": config.HARNESS_SECRET})
d = r.json(); print(r.status_code, f"{(time.monotonic()-t)*1000:.0f} ms", d.get("source"))
print("signals:", json.dumps(d["signals"])); print("summary:", d["summary"])
for a in d["actions"]:
    print(f"- {a['id']}: {a['title']} @ {(a['after'] or {}).get('start','')[:16]} | {a['reason']}\n    rules: {a['rules']}")
print("memory used:", len(d["memoryUsed"]), "facts")
