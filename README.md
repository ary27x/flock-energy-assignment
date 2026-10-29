# Urja Meter Ops - README

A small Python (FastAPI) service that logs into the Urja portal, pulls the signed
bulk export, repairs the hierarchy, and serves a clean JSON API.

## Run
    pip install -r requirements.txt
    set PORTAL_USER=operator@urja.local
    set PORTAL_PASS=urja-ops-2026
    uvicorn app:app --reload
Open http://127.0.0.1:8000/docs to try every endpoint.

## Endpoints
GET /health, /v1/meters (q, make, status, phase, dt, page, pageSize),
/v1/meters/{id}, /v1/meters/{id}/readings, /v1/transformers,
/v1/transformers/{code}, /v1/hierarchy, /v1/data-quality
Optional: set API_KEY and send header `x-api-key`.

## How it works
1. Login: POST /login (form + x-sveltekit-action header), cookie kept by httpx.
2. Export: GET /portal/keys -> HMAC-SHA256 over "GET\n/portal/export\npage=1\n<ts>" -> GET /portal/export.
3. Blank hierarchy values are filled from sibling meters on the same transformer;
   DT names/capacity come from /portal/dts (master wins).
4. Readings: kwh/kvah are cumulative, so consumptionKwh = difference; timestamps become ISO-8601 +05:30 (assumed IST).
5. Snapshot cached 10 min (add ?refresh=true on /v1/meters to force).

## Known limits / assumptions
- Not run against the live portal (only a mocked one); expiry detection = "non-JSON or non-200 -> re-login once".
- Hierarchy key names for substation etc. assumed to match the blueprint.
- Export page=2 never tested; page=1 returns all 403.
- Everything is in memory; fine for 403 meters, not for millions.

## Reflection

**Assumptions**
- Built and tested against the mocked portal only, not the real Urja one - the HMAC scheme over `GET\n/portal/export\npage=1\n<ts>` is assumed to be the actual production scheme, not just something the mock happens to accept.
- Assumed session expiry always shows up as a non-JSON or non-200 response, rather than, say, a 200 with an "expired" flag buried in the body.
- Assumed the hierarchy key names (substation, feeder, DT, etc.) match the blueprint exactly, rather than varying by region or portal version.
- Assumed timestamps coming back from the portal are already in IST, even though nothing in the payload says so explicitly - the +05:30 offset is hardcoded, not derived.

**Most difficult part**
- The hierarchy repair logic took the longest to get right. Filling blank substation/feeder/DT fields from sibling meters on the same transformer is simple in theory, but gets messy when two siblings disagree, or when a DT has no meters at all with a populated field to borrow from.
- Got unstuck by writing a tiny standalone script that printed the raw export grouped by transformer code, so the actual shape of the disagreements was visible instead of guessed from the spec.
- From there, the "master wins" rule for DT names/capacity fell out naturally once DT master records were decided to always override anything inferred from siblings.

**With another day**
- Actually test against page=2 of the export instead of assuming the pagination contract - there's currently no evidence it even works, since the mock always returns everything on page 1.
- Move off the in-memory snapshot to a lightweight persistence layer (even just SQLite), so a restart doesn't mean re-authenticating and re-pulling the full export.
- Add real tests around the consumption-delta calculation, since cumulative-to-delta conversion looks right until you hit a meter rollover or a missing prior reading.

**A mistake I made**
- Early on, `consumptionKwh` was computed as a simple difference between consecutive readings, with no check for counter resets - a meter that rolled over or got replaced mid-period would've produced a negative consumption value.
- Caught it only because one row in the mock data had a lower reading than the one before it, and the API happily returned a negative kWh figure.
- Added a guard for that case afterward, but it's the kind of bug that's easy to miss if your test data happens to be well-behaved.

**If I were reviewing this myself**
- The 10-minute cache and the in-memory store have undocumented behavior under concurrent requests - there's no locking, so two requests hitting a cold cache at the same time could both trigger a full re-fetch.
- The data-quality endpoint could use better error messages instead of just surfacing raw gaps.
- "Page=1 returns all 403 meters" was accepted a bit too readily - convenient for this exercise, but exactly the kind of assumption that breaks quietly the day the real portal's dataset grows past one page.
