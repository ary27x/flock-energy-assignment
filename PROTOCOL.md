# PROTOCOL.md - How the Urja portal talks

Base URL: `https://urja-ops.flockenergy.tech`

The portal was explored on 28 October 2026, between 05:47 and 05:49 UTC, using Playwright to interact with the actual browser UI. During this process, a HAR file and network traffic log captured 105 requests, which were used to understand how the portal works without accessing its server-side code.


## 1. Big picture

SvelteKit app, three screens after login: Meters, Meter detail,
Transformers. Data comes in on two channels, both riding the same cookie:
plain JSON under `/portal/*`, and SvelteKit's own compact "devalue" JSON at
`/meters/__data.json` / `/meters/{id}/__data.json`. No CORS
headers, no `server` header - this is meant to be called from a backend,
not a browser on some other site.

## 2. Auth

1. `GET /` with no cookie -> `302` to `/login`.
2. `POST /login` (form-urlencoded `email`/`password`, header
   `x-sveltekit-action: true`) -> `200` + `Set-Cookie` - **not** a 303.
   Success body: `{"type":"redirect","status":303,"location":"/meters"}`.
   Check `type == "redirect"`, not the HTTP status - that header is what
   makes SvelteKit hand back JSON instead of a real redirect. Failure body
   was never captured [UNKNOWN].
3. Cookie rides every later request.
4. `POST /api/auth/sign-out` (no body) ends it, clears three cookies.

**Cookie:** `__Secure-better-auth.session_token`, `Max-Age=3600; HttpOnly;
Secure; SameSite=Lax`. Nothing in the capture ever refreshed it, so treat
it as a hard 1-hour expiry - re-login a bit before 55 minutes, and again on
any failed call. Whether it slides with activity is [UNKNOWN], and so is
what a no-cookie/expired call actually returns - which is exactly why
`app.py`'s expiry check is just "not 200 or not JSON -> re-login once."

## 3. Endpoints that matter

| Call | What it's for |
|---|---|
| `GET /meters/__data.json?...=010` | who's logged in - cheap probe |
| `GET /meters/{id}/__data.json?...=001` | one meter's detail + hierarchy |
| `GET /portal/meters/search?q=&page=` | paged search, 20/page |
| `GET /portal/meters/{id}/geo` | lat/lng, **as strings** |
| `GET /portal/meters/{id}/energy` | readings |
| `GET /portal/dts?page=` | transformer master list |
| `GET /portal/keys` | the export-signing secret |
| `GET /portal/export?page=1` | all 403 meters at once, signed |

**Meter detail** - attributes come back as a name/value list, not a dict.
Hierarchy values look like `"Name (CODE)"`, and the level called
`Sub Station` here is `substation` in the export - same thing, different
spelling. Newer meters (`build: v2`) apparently read a `detail.classData`
JSON string instead; never saw one, so its shape is [UNKNOWN].

**Search** - 1-based `page`, fixed size 20, ordered by meterId. No match
gives `"data":[],"total":0`. Test queries (`hp65` -> 2 hits, `j1003` -> the
100 J100300-range meters) look like "starts with" on meterId/serial, but
"contains" isn't ruled out [UNKNOWN]. Only returns 6 of the 10 meter fields
- no hierarchy, install type, or geo.

**Energy** - `kwh`/`kvah` are cumulative registers, not consumption, and
come back as strings with no timezone (`dd/mm/yyyy HH:mm`). One meter logs
every 30 min, another only daily - why they differ is [UNKNOWN]. Only
R-phase voltage exists, even on three-phase meters. Latest reading anywhere
in the dataset is 30 Jun 2026 - this is not live data.

**Transformers** - 40 of them, 2 pages, the only place capacity (kVA)
shows up.

**Signing secret** - handed to the same logged-in session it's meant to
restrict, then used to sign the export. Worth flagging to the utility: it
slows casual scraping but does nothing once someone's already
authenticated.

**Export** - needs `x-timestamp` + `x-signature` (section 4). `page=1`
already returns all 403 rows in one shot, ~230 KB in ~190ms; `page=2` was
never tried [UNKNOWN]. Each object carries its full hierarchy and *real*
numeric coordinates (unlike the geo endpoint's strings) - best single
source of truth, ~2 calls vs. the ~830 it'd take paging search + geo +
detail per meter.

## 4. The signed export (HMAC-SHA256)

Pulled from the Transformers page JS, then double-checked: recomputing it
against the capture reproduced the exact signature the browser sent. [SEEN]

```
secret    = GET /portal/keys -> data.signingSecret
timestamp = current unix time, seconds, as a decimal string
message   = "GET\n/portal/export\npage=1\n<timestamp>"
signature = lowercase hex HMAC-SHA256(key=secret, data=message)
```

Single `\n` between parts, no trailing newline, secret used as plain UTF-8
text (not base64-decoded). Added query params need signing in the exact
string/order sent. Clock-skew tolerance and the bad-signature response are
both [UNKNOWN].

## 5. devalue format (the `__data.json` channel)

Flat array where objects/lists point at each other by index; index 0 is
the root, and a `skip` node means "unchanged, you already have this." A
short recursive resolver (walk each index, inline lists/dicts, return
primitives as-is) handles every shape that actually showed up. SvelteKit's
real format also has markers for dates/maps/undefined, but none appeared
here. `x-sveltekit-invalidated=010` vs `001` picks which of the three nodes
(root layout / portal layout / page) gets re-run; `001` is the page node
[GUESS].

## 6. Data shape (checked against the export)

403 meters, `J100000`-`J100402`, no gaps. 40 transformers, 10-13 meters
each. Hierarchy is zone > circle > division > subdivision > substation >
feeder > dt (3/6/10/14/18/28/40 distinct nodes at each level). Codes aren't
globally unique - 10 division codes cover 30 divisions - so a node needs
its full path to identify it, not just its code.

## 7. Quirks worth remembering

- Login "succeeds" with a 200, not a 303 - check the body, not the status.
- Numbers are strings from `/geo` and `/energy`, real numbers in the export.
- Readings keep going for Decommissioned meters - don't infer availability
  from status.
- 22 meters have blank hierarchy fields in the export; fill from siblings
  on the same DT.
- 12 feeder codes are reused across two different DTs - treat as separate
  nodes.
- DT-007's name disagrees between the master list and 3 meter records (old
  name) - master list wins, flag the mismatch.
- Meters on the same DT can be tens of km apart - show coordinates as-is,
  don't use them to sanity-check grouping.
- No cache headers anywhere on `/portal/*` - the 10-min cache is ours, not
  the portal's.

