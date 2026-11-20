# Connecting the app to the harness (for Jonathan)

The harness picks Jordan's shoes from real web research and returns the app's own `Purchase` shape. The app stays the agent's hands: Swift keeps approval (Order it / Not now / Cancel order), action ids, folding, break-in runs and the calendar. About 30 minutes of Swift.

**Base URL:** `https://trackerplusplus-harness.vercel.app`
**Shared secret:** Surya sends it to you privately. Never commit it.

## 1. Settings (Settings.bundle/Root.plist)

Add two text fields next to `evidence_url`:

| Key | Value |
|---|---|
| `recommend_url` | `https://trackerplusplus-harness.vercel.app` |
| `harness_secret` | the secret from Surya |

Leave `harness_url` empty: it swaps the *whole* agent. Set `evidence_url` to the same base URL to get live Senso passages in the day-pass beat too (`/evidence` needs no secret).

## 2. RecommendClient.swift (new, mirrors EvidenceClient)

```swift
struct RecommendClient: Sendable {
    let baseURL: URL
    let secret: String
    static let timeout: TimeInterval = 6   // the harness answers within 5 s or falls back itself

    static func configured() -> RecommendClient? {
        let d = UserDefaults.standard
        let raw = d.string(forKey: "recommend_url")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let secret = d.string(forKey: "harness_secret")?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let url = URL(string: raw), url.scheme?.hasPrefix("http") == true, !secret.isEmpty else { return nil }
        return RecommendClient(baseURL: url, secret: secret)
    }

    private struct Request: Encodable {
        struct Shoes: Encodable { var boughtOn: String; var startMiles: Int }
        var persona: String; var now: Date; var shoes: Shoes; var raceDate: String; var mandate: [Mandate]
    }
    struct Response: Decodable { var purchase: Purchase?; var live: Bool; var source: String }

    /// The harness's pick, or nil on any failure or timeout (the caller keeps its own pick).
    func shoes(persona: String, now: Date, boughtOn: Date, startMiles: Int, raceDate: Date,
               mandate: [Mandate], cal: Calendar) async -> Purchase? {
        func day(_ d: Date) -> String {
            let c = cal.dateComponents([.year, .month, .day], from: d)
            return String(format: "%04d-%02d-%02d", c.year!, c.month!, c.day!)
        }
        var request = URLRequest(url: baseURL.appending(path: "shoes/recommend"), timeoutInterval: Self.timeout)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(secret, forHTTPHeaderField: "X-Harness-Secret")
        do {
            request.httpBody = try JSONEncoder.contract.encode(Request(
                persona: persona, now: now, shoes: .init(boughtOn: day(boughtOn), startMiles: startMiles),
                raceDate: day(raceDate), mandate: mandate))
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return nil }
            return try JSONDecoder.contract.decode(Response.self, from: data).purchase
        } catch {
            return nil
        }
    }
}
```

`boughtOn` and `raceDate` are local calendar days (`YYYY-MM-DD`); `now` is the demo clock as ISO 8601 (`JSONEncoder.contract` already does that). Use the person's calendar, not UTC, for the days.

## 3. Hook it into `shoesPurchase` (StandInAgent+Purchases.swift)

At the top of `shoesPurchase(...)`, before the hard-coded listings:

```swift
if let client = RecommendClient.configured(),
   var picked = await client.shoes(persona: s.persona, now: s.now, boughtOn: boughtOn,
                                   startMiles: demo.shoesStartMiles ?? 0, raceDate: raceDate,
                                   mandate: mandate, cal: cal) {
    picked.id = id                 // keep Swift's stable id: shoes-<boughtOn>
    return picked                  // status already follows the mandate ($ over limit → awaitingApproval)
}
// …otherwise fall through to the current hard-coded pick
```

`shoesPurchase` needs `s.persona`, `boughtOn`, `raceDate` and `demo.shoesStartMiles` passed in; they're all available in `shoesActions`. Everything after this stays as it is: the `purchase-<id>` action, Order it → bought + break-in runs (they use `options.first?.arrives`, which the harness sets), Not now, Cancel order.

The trigger stays Swift's (`shoeMiles ≥ 375` and race within 14 days). If the harness says the pair is fresh it returns no `purchase`, and the client returns nil, so Swift's own pick runs. That's fine for the demo preset (the harness also reads 390 miles there).

## 4. What comes back

```json
{
  "condition": {"km": 628.4, "miles": 390, "percentLife": 97, "status": "replace", "why": "..."},
  "purchase": {
    "id": "shoes-2026-10-22",
    "item": "New Balance Fresh Foam X 1080 v15",
    "merchant": "Demo store, no charge",
    "amountCents": 17000,
    "status": "awaitingApproval",
    "rule": {"id": "ask-first", "text": "Ask me before buying anything else."},
    "noticed": "Your shoes hit 390 miles (97% of their life). Race day is in 9 days. Your high mileage ...",
    "evidence": [{"source": "Running shoe care guide", "passage": "..."},
                 {"source": "Jordan's fit notes", "passage": "..."},
                 {"source": "The Best Running Shoes ... (marathonhandbook.com)", "passage": "..."}],
    "evidenceLive": true,
    "options": [{"name": "New Balance Fresh Foam X 1080 v15", "amountCents": 17000, "arrives": "2026-11-23T00:00:00-08:00"},
                {"name": "Altra Paradigm 8", "amountCents": 10300, "arrives": "...", "lostBecause": "..."}]
  },
  "live": true, "source": "sie", "candidatesFrom": "web-snapshot"
}
```

- **Shoes:** real models from expert reviews found with Nimble this morning; the review page is the last citation. Prices are list prices from the reviews. Delivery is simulated: every pick ships from the "Demo store, no charge" in 3 days.
- **The Why screen:** expect about 15–25 options: the chosen one, about 3 the model compared, and the rest with rule reasons (toe box not confirmed, low cushioning for long runs, carbon plate). Consider showing the first 4–5 and collapsing the rest.
- Unknown JSON keys (`condition`, `profile`, `source`, ...) are fine to ignore; only `purchase` matters.

## 5. Check it

```sh
curl -s https://trackerplusplus-harness.vercel.app/health      # all true
curl -s -X POST https://trackerplusplus-harness.vercel.app/warm -H "X-Harness-Secret: $SECRET"
```

In the Simulator: Demo → "Jordan: shoes near 400 mi". The card should show the harness's shoe, and Why this pair should show live citations including a review site. Turn Wi-Fi off and repeat: Swift's own Pacer 9 should appear, so the fallback works.

**Before going on stage:** `POST /warm` with the secret, so the first request doesn't wait for a cold model.


## 6. Workout recommendations (optional, after the shoes)

`POST /workouts/recommend` with the same header returns Jordan's next 7 days as `AgentAction`s you can append like any other action:

```json
{"persona": "jordan", "now": "<demo clock ISO 8601>",
 "shoes": {"boughtOn": "2026-10-22", "startMiles": 267},
 "raceDate": "2026-11-29",
 "newPairArrives": "2026-11-23",
 "busy": [{"start": "...", "end": "..."}]}
```

- `newPairArrives`: send the chosen option's `arrives` date once Order it was tapped; leave it out before.
- `busy`: the same busy times the snapshot already carries.
- Response `actions[]`: `{id, kind: "addWorkout" | "note", title, planItemId, after: {start, end, activityType}, reason, points, createdAt, rules}`. `rules` is an extra field you can ignore or show on the Why screen. Ids are stable (`wk-<date>-<activity>`), so repeat calls don't duplicate.
- `actions[].evidence`: web citations `{source, passage, url}` for the session's rule; show them on the Why screen.
- `suggestions[]`: up to 2 optional extras from expert guidance, e.g. `{title: "Relaxed strides", minutes: 6, when: "after your Thu run", attachTo: "wk-…", why, source: {title, host, url}, quote, optional: true}`. Show them as "Suggested" (don't book automatically). Fine to ignore for the demo.
- About 1.5 s; `source` is `sie` (model-written reasons) or `rules` (template reasons).
