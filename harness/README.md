# TrackerPlusPlus harness

The stateless backend that recommends running shoes from the fake persona data. The app stays the agent's hands: it applies what comes back and writes the calendar. Design: [docs/designs/trackerplusplus-recommendation-harness.md](../docs/designs/trackerplusplus-recommendation-harness.md).

## Run

Needs [uv](https://docs.astral.sh/uv/). Keys go in `harness/.env` (git-ignored by `harness/.gitignore`): `SUPERLINKED_API_KEY` (or `SIE_API_KEY`), and optionally `SENSO_API_KEY` and `HARNESS_SECRET`.

```sh
cd harness
uv sync
PYTHONPATH=. uv run python scripts/warm.py      # loads both SIE models; also the smoke test
PYTHONPATH=. uv run python scripts/sample.py    # Jordan's demo preset, live (add --offline for the fallback)
uv run pytest -q                                # 33 tests; the live SIE test skips without a key
PYTHONPATH=. uv run uvicorn api.index:app --reload --port 8000
```

## Routes

| Route | What it does |
|---|---|
| `GET /health` | Which keys are configured (never their values) |
| `POST /warm` | Loads the two SIE models before the demo, since cold starts can take minutes |
| `POST /evidence` | `{purchaseKind, query}` → `{evidence, live}`, the shape `EvidenceClient.swift` expects. Cached passages until Senso is wired. Open even when a secret is set, because the app sends no header. |
| `POST /workouts/recommend` | The next 7 days from health data, shoe condition and memory: `{persona, now, shoes?, newPairArrives?, raceDate?, busy[]}` → `{summary, signals, actions[], memoryUsed, source, auditId}`. Needs the secret. |
| `POST /shoes/prefetch?persona=jordan` | Researches shoes on the web now (Nimble + SIE, about 30 s) and stores them in SingleStore when configured. Needs the secret. |
| `GET /audit/recent?persona=jordan&limit=20` | The latest recommendations from SingleStore. Needs the secret; 503 when SingleStore is off. |
| `GET /audit/{auditId}` | One full audit record. Needs the secret. |
| `POST /shoes/recommend` | `{persona, now, shoes:{boughtOn,startMiles}, raceDate, mandate}` → `{condition, profile, purchase?, live, source}`. `purchase` uses the Swift `Purchase` shape. |

```sh
curl -s localhost:8000/shoes/recommend -H 'content-type: application/json' -d '{
  "persona":"jordan","now":"2026-11-20T09:00:00-08:00",
  "shoes":{"boughtOn":"2026-10-22","startMiles":267},"raceDate":"2026-11-29",
  "mandate":[{"id":"ask-first","text":"Ask me before buying anything else.","category":"any","maxAutoCents":0}]}'
```

## How it decides

**Where the shoes come from:** web research, not a fixed list. `scripts/prefetch_web.py jordan` searches expert review sites with **Nimble** (2 queries, about 12 pages), and **SIE Qwen3.8** extracts each recommended model (price, toe box, plate, cushion, drop, a quote and the URL). The result is saved to `harness/data/web_candidates/jordan.json`, committed and deployed. At request time the harness reads SingleStore if `SINGLESTORE_URL` is set, else that snapshot, else the fake catalog. `candidatesFrom` in the response says which. Delivery is simulated (Demo store, ships in 3 days), because reviews carry no arrival dates.

To connect the app, see [INTEGRATION.md](INTEGRATION.md).


1. Load the persona's workouts, shifted so the history ends the day before `now` (as the app does).
2. Compute wear from the real `distanceKm` of runs since `boughtOn`, plus `startMiles`: ≥85% of a 650 km life means replace. No purchase if the pair is fresh or the race is more than 14 days away.
3. Hard filters in code: fit notes (toe box must be confirmed wide, no carbon plate, surface), real cushioning and no zero-drop for long runs of 15 km or more, and arrival at least 3 days before the race.
4. **Superlinked SIE:** `Qwen/Qwen3-Reranker-4B` scores the shortlist against the runner's profile, and the top 4 go to `Qwen/Qwen3.8-27B-FP8`, which picks one and writes why each other shoe lost, constrained to a JSON schema.
5. Guardrails: the pick must be on the shortlist, and every other shoe needs a short reason. Otherwise, or on any SIE error or timeout, a deterministic pick: high cushion for long runs ≥15 km, then cheapest. `source` says which path answered.
6. Audit: with `SINGLESTORE_URL` set, every recommendation (inputs, shortlist, rejections, pick, citations, timing) is written to a `recommendations` table, and the response carries its `auditId`.
7. Mandate: over the `any` line's `maxAutoCents` means `awaitingApproval`. Approval, cancellation, action ids and break-in runs stay in Swift.

The whole route has a 5 s budget. Measured on Jordan with web candidates: about 3.5–4 s live and about 0.3 s on the fallback.

## Workout recommendations

`POST /workouts/recommend` plans the next 7 days. Code decides the sessions; the model only explains them.

- **Usual week:** per weekday, the activity, minutes and start hour Jordan actually did in the last 4 weeks. **Tony's memory agent** supplies the fixed slots (e.g. "running on Tue between 18:00 and 19:00") and they set the start time. The history is shifted by whole weeks here so weekdays match the memory.
- **Health data:** training load (effort × minutes; effort estimated from average heart rate when missing) for the last 7 days vs. the 4-week weekly average. The week must stay ≤ min(110% of the larger of the two, 1.3 × the average). Sleep down 30+ min/night or resting HR up 3+ bpm → the first hard session becomes easy.
- **Shoes:** worn out (≥85%) and no new pair yet → runs longer than 45 min move to the bike or elliptical. After `newPairArrives`, the first two runs are 30-minute break-in runs.
- **Race:** taper in the last 10 days (long run 60%, other runs 80%), rest the day before, and race day as a note.
- **Calendar:** sessions overlapping `busy` move to right after the event.
- **Web suggestions (optional):** `scripts/prefetch_ideas.py` has Nimble search expert guidance (taper, race-week drills, cross-training on worn shoes, breaking in new shoes) and Qwen extracts small session ideas with their source into `harness/data/workout_ideas/running.json`. The recommender offers at most 2 as `suggestions` (never booked): only low-risk kinds (strides, shakeout, mobility, drills, walk), matching the week's phase, within the load headroom, no extra running on worn shoes before the new pair, and running add-ons attached to a planned run (`attachTo`).
- **Citations:** each session's `evidence` cites a web source behind its rule (taper, worn shoes, break-in) when one was found.
- **Reasons:** SIE Qwen3.8 writes one sentence per session, all calls in parallel (about 1.5 s). A template is the fallback. Every plan is logged to SingleStore.

Actions use the app's `AgentAction` shape (`addWorkout` with an `after` slot; ids are stable as `wk-<date>-<activity>`), so the app can put them on the calendar. Memory facts are rebuilt with `node harness/scripts/build_memory.mjs` (from the repo root).

## SingleStore (audit log and candidate store)

Off until `SINGLESTORE_URL` is set; the harness works the same without it.

1. In the SingleStore portal, create a free starter workspace and a database (e.g. `trackerplusplus`).
2. Workspace → **Connect** → Python (or "Your app"): build the URL as `user:password@host:3306/trackerplusplus`. The `singlestoredb://` prefix is optional.
3. Put it in `harness/.env` as `SINGLESTORE_URL=...`.
4. `PYTHONPATH=. uv run python scripts/singlestore_check.py jordan`. This creates the two tables, loads the web snapshot into `shoe_candidates`, writes and reads one audit record, and lists recent ones.
5. Add it to Vercel and redeploy:
   `uv run python -c "from harness import store; print(store.url(), end='')" | npx vercel env add SINGLESTORE_URL production`
   `npx vercel deploy --prod --yes`. Then `/health` shows `"singlestore": true`.

What gets stored:
- `shoe_candidates`: one row per shoe from the web research (`scripts/prefetch_web.py` or `POST /shoes/prefetch` writes it). Recommendations use it from memory only (loaded by `/warm` or in the background after the first request, cached 5 minutes), so the database is never on the request path; until then they use the snapshot.
- `recommendations`: one row per pick, holding the request, wear, profile, where the candidates came from, the shortlist, every rejection and its reason, the final `Purchase` with citations, the source (`sie` or `rules`) and latency. The write waits at most 0.4 s, so a slow database never delays the answer. If it finishes, the response carries `auditId`.

Try it for the demo: `curl -s "$URL/audit/recent?persona=jordan&limit=1" -H "X-Harness-Secret: $SECRET"`.

## Deploy (Vercel)

Live at **https://trackerplusplus-harness.vercel.app** (personal Vercel project `trackerplusplus-harness`, production, so no preview protection).

- Entry point: `main.py` exposes the FastAPI `app` (Vercel's zero-config FastAPI support); `vercel.json` stays empty. A rewrite to `api/index.py` dropped the request path.
- Dependencies come from `requirements.txt`. `.vercelignore` keeps `.env`, tests and scripts out of the upload.
- Environment variables (production): `SUPERLINKED_API_KEY`, `SENSO_API_KEY`, `NIMBLE_API_KEY`, `HARNESS_SECRET`.
- `HARNESS_SECRET` is on: `/shoes/recommend` and `/warm` need `X-Harness-Secret: <value from harness/.env>`; `/health` and `/evidence` stay open.
- Redeploy: `cd harness && npx vercel deploy --prod --yes`. Before the demo: `POST /warm` with the header.
