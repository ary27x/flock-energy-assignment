import os, time, hmac, hashlib, threading
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, Query

BASE = "https://urja-ops.flockenergy.tech"
LEVELS = ["zone", "circle", "division", "subdivision", "substation", "feeder", "dt"]
IST = timezone(timedelta(hours=5, minutes=30))
CACHE_SECONDS = 600  # snapshot lifetime (10 min)


# ---------------------------------------------------------------- portal client
class Portal:
    def __init__(self):
        self.c = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)
        self.lock = threading.Lock()  # one login at a time

    def login(self):
        r = self.c.post(
            "/login",
            data={"email": os.environ.get("PORTAL_USER", ""),
                  "password": os.environ.get("PORTAL_PASS", "")},
            headers={"accept": "application/json",
                     "x-sveltekit-action": "true", "origin": BASE},
        )
        try:
            ok = r.json().get("type") == "redirect"
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(401, "Portal login failed (check PORTAL_USER / PORTAL_PASS)")

    def get(self, path, params=None, headers=None, retry=True):
        try:
            r = self.c.get(path, params=params, headers=headers)
            data = r.json() if r.status_code == 200 else None
        except httpx.HTTPError:
            raise HTTPException(502, "Portal unreachable")
        except ValueError:
            data = None  # HTML instead of JSON -> probably session expired
        if data is None:
            if not retry:
                raise HTTPException(502, "Portal returned an unexpected response")
            with self.lock:
                self.login()
            return self.get(path, params, headers, retry=False)
        return data

    def export_all(self):
        secret = self.get("/portal/keys")["data"]["signingSecret"]
        ts = str(int(time.time()))
        msg = "\n".join(["GET", "/portal/export", "page=1", ts])
        sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        body = self.get("/portal/export", {"page": 1},
                        {"x-timestamp": ts, "x-signature": sig})
        return body["data"]

    def all_dts(self):
        out, page = [], 1
        while True:
            b = self.get("/portal/dts", {"page": page})
            out += b["data"]
            if len(out) >= b["total"] or not b["data"]:
                return out
            page += 1


portal = Portal()


# ---------------------------------------------------------------- snapshot (cache)
snap = {"time": 0, "meters": {}, "dts": {}, "flags": Counter()}


def build_snapshot():
    raw = portal.export_all()
    dts_master = {d["code"]: d for d in portal.all_dts()}

    # vote for each DT's ancestors, ignoring blanks (blueprint Part 8.2)
    votes = defaultdict(lambda: defaultdict(Counter))
    for m in raw:
        for lv in LEVELS[:-1]:
            h = (m.get("hierarchy") or {}).get(lv) or {}
            if h.get("code"):
                votes[m["dtCode"]][lv][(h["code"], h.get("name") or "")] += 1
    paths = {}
    for dt, lvls in votes.items():
        paths[dt] = {}
        for lv, cnt in lvls.items():
            code = cnt.most_common(1)[0][0][0]
            name = next((n for (c, n) in cnt if c == code and n), "")
            paths[dt][lv] = {"code": code, "name": name}

    flags, meters = Counter(), {}
    for m in raw:
        h = m.get("hierarchy") or {}
        hier, repaired = [], False
        for lv in LEVELS:
            node = h.get(lv) or {}
            if lv != "dt" and not (node.get("code") and node.get("name")):
                fix = paths.get(m["dtCode"], {}).get(lv)
                if fix:
                    node, repaired = fix, True
            hier.append({"level": lv, "code": node.get("code"), "name": node.get("name")})
        quality = []
        if repaired:
            quality.append("hierarchy_repaired_from_siblings"); flags["hierarchy_repaired"] += 1
        master = dts_master.get(m["dtCode"])
        if master and hier[-1]["name"] and hier[-1]["name"] != master["name"]:
            quality.append("dt_name_differs_from_master"); flags["dt_name_alias"] += 1
        if master:
            hier[-1]["name"] = master["name"]
        geo = m.get("geo") or {}
        meters[m["meterId"]] = {
            "meterId": m["meterId"],
            "serialNo": m.get("serialNo"),
            "make": m.get("make"),
            "phase": m.get("phaseType"),
            "status": (m.get("installStatus") or "").lower(),
            "connectionType": (m.get("installType") or "").lower().replace(" ", "_"),
            "recordGeneration": m.get("build"),
            "dtCode": m.get("dtCode"),
            "hierarchy": hier,
            "pathKey": "/".join(str(x["code"]) for x in hier[:-1]),
            "location": {"lat": float(geo["lat"]) if geo.get("lat") not in (None, "") else None,
                         "lng": float(geo["lng"]) if geo.get("lng") not in (None, "") else None},
            "qualityFlags": quality,
        }
    snap.update(time=time.time(), meters=meters, dts=dts_master, flags=flags)


def get_snap(refresh=False):
    if refresh or time.time() - snap["time"] > CACHE_SECONDS:
        build_snapshot()
    return snap


# ---------------------------------------------------------------- API
app = FastAPI(title="Urja Meter Ops - Clean API", version="1.0.0",
              description="Simple clean API on top of the Urja Meter Ops portal.")


def check_key(x_api_key: str = Header(default="")):
    want = os.environ.get("API_KEY", "")
    if want and x_api_key != want:
        raise HTTPException(401, "Missing or wrong x-api-key")


auth = [Depends(check_key)]


@app.get("/health")
def health():
    age = round(time.time() - snap["time"]) if snap["time"] else None
    try:
        portal.login(); up = True
    except HTTPException:
        up = False
    return {"status": "ok", "portalLogin": up, "snapshotAgeSeconds": age}


@app.get("/v1/meters", dependencies=auth)
def list_meters(q: str = "", make: str = "", status: str = "", phase: str = "",
                dt: str = "", page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                refresh: bool = False):
    items = list(get_snap(refresh)["meters"].values())
    ql = q.lower()
    items = [m for m in items
             if (not ql or ql in m["meterId"].lower() or ql in (m["serialNo"] or "").lower())
             and (not make or m["make"].lower() == make.lower())
             and (not status or m["status"] == status.lower())
             and (not phase or m["phase"] == phase.lower())
             and (not dt or m["dtCode"].lower() == dt.lower())]
    start = (page - 1) * pageSize
    return {"total": len(items), "page": page, "pageSize": pageSize,
            "data": items[start:start + pageSize]}


@app.get("/v1/meters/{meter_id}", dependencies=auth)
def get_meter(meter_id: str):
    m = get_snap()["meters"].get(meter_id.upper())
    if not m:
        raise HTTPException(404, "Meter not found")
    return m


def parse_ts(s):
    return datetime.strptime(s, "%d/%m/%Y %H:%M").replace(tzinfo=IST)


@app.get("/v1/meters/{meter_id}/readings", dependencies=auth)
def readings(meter_id: str):
    meter_id = meter_id.upper()
    if meter_id not in get_snap()["meters"]:
        raise HTTPException(404, "Meter not found")
    rows = portal.get(f"/portal/meters/{meter_id}/energy").get("data", [])
    out, prev = [], None
    for r in rows:
        kwh, kvah = float(r["kwh"]), float(r["kvah"])
        out.append({"timestamp": parse_ts(r["timestamp"]).isoformat(),
                    "kwh": kwh, "kvah": kvah,
                    "voltageR": float(r["voltR"]) if r.get("voltR") else None,
                    "consumptionKwh": round(kwh - prev, 3) if prev is not None else None})
        prev = kwh
    interval = None
    if len(out) > 1:
        a = datetime.fromisoformat(out[0]["timestamp"]); b = datetime.fromisoformat(out[1]["timestamp"])
        interval = int((b - a).total_seconds() // 60)
    return {"meterId": meter_id, "intervalMinutes": interval, "timezoneAssumed": "Asia/Kolkata",
            "note": "kwh/kvah are cumulative registers; consumptionKwh = difference to previous row",
            "count": len(out), "data": out}


@app.get("/v1/transformers", dependencies=auth)
def list_transformers():
    s = get_snap()
    counts = Counter(m["dtCode"] for m in s["meters"].values())
    return {"total": len(s["dts"]),
            "data": [{**d, "meterCount": counts[c]} for c, d in s["dts"].items()]}


@app.get("/v1/transformers/{code}", dependencies=auth)
def get_transformer(code: str):
    s = get_snap(); code = code.upper()
    d = s["dts"].get(code)
    if not d:
        raise HTTPException(404, "Transformer not found")
    mine = [m for m in s["meters"].values() if m["dtCode"] == code]
    return {**d, "hierarchy": mine[0]["hierarchy"] if mine else [],
            "meters": [m["meterId"] for m in mine]}


@app.get("/v1/hierarchy", dependencies=auth)
def hierarchy():
    """Simple tree: zone > circle > division > ... > dt, with meter counts."""
    tree = {}
    for m in get_snap()["meters"].values():
        node = tree
        for h in m["hierarchy"]:
            key = f'{h["code"]} {h["name"] or ""}'.strip()
            node = node.setdefault(key, {"_meters": 0, "children": {}})
            node["_meters"] += 1
            node = node["children"]
    return tree


@app.get("/v1/data-quality", dependencies=auth)
def data_quality():
    s = get_snap()
    return {"meters": len(s["meters"]), "transformers": len(s["dts"]),
            "hierarchyRepaired": s["flags"]["hierarchy_repaired"],
            "dtNameAliases": s["flags"]["dt_name_alias"],
            "snapshotAgeSeconds": round(time.time() - s["time"])}
