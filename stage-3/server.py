"""Tablekeeper HTTP service; all state transitions are serialized transactions."""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

UTC = timezone.utc
LOCK = threading.RLock()
STATE_KEYS = ("users", "tokens", "restaurants", "reservations", "receipts", "policies", "histories", "series", "restaurant_revisions")
STATE = {key: {} for key in STATE_KEYS}


class APIError(Exception):
    def __init__(self, status=422, code="validation_failed"):
        self.status, self.code = status, code


def fail(status=422, code="validation_failed"):
    raise APIError(status, code)


def string(body, key):
    if key not in body:
        fail()
    value = body[key]
    if not isinstance(value, str):
        fail(400, "malformed_request")
    return value


def identifier(body, key):
    value = string(body, key)
    if not 1 <= len(value) <= 64:
        fail()
    return value


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def instant(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def local_time(value, zone):
    naive = datetime.fromisoformat(value)
    aware = naive.replace(tzinfo=zone, fold=0)
    if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        fail(422, "invalid_local_time")
    return aware


def restaurant(rid):
    if rid not in STATE["restaurants"]:
        fail(404, "not_found")
    return STATE["restaurants"][rid]


def fixture_terms(r):
    return {"policy_version": 0, **{k: copy.deepcopy(r[k]) for k in ("slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes", "opening_hours")}, "capacities": {t["id"]: t["capacity"] for t in r["tables"]}}


def policy_for(r, date):
    policies = [p for p in STATE["policies"].get(r["id"], []) if p["effective_from"] <= date]
    if not policies:
        return fixture_terms(r)
    policy = max(policies, key=lambda p: (p["effective_from"], p["policy_version"]))
    return copy.deepcopy({k: v for k, v in policy.items() if k != "effective_from"})


def policy_restaurant(r, terms):
    return {**r, **terms, "tables": [{**t, "capacity": terms["capacities"][t["id"]]} for t in r["tables"]]}


def validate_policy(body, r):
    date = body.get("effective_from")
    if not isinstance(date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", date):
        fail()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        fail()
    for key, low, high in (("slot_minutes", 1, 1440), ("reservation_duration_minutes", 1, 1440), ("cancellation_cutoff_minutes", 0, 10080)):
        if type(body.get(key)) is not int or not low <= body[key] <= high:
            fail()
    hours = body.get("opening_hours")
    if not isinstance(hours, list):
        fail()
    days = set()
    for h in hours:
        if not isinstance(h, dict) or h.get("weekday") not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") or h["weekday"] in days:
            fail()
        days.add(h["weekday"])
        for key in ("opens", "closes"):
            if not isinstance(h.get(key), str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", h[key]):
                fail()
        if h["opens"] >= h["closes"]:
            fail()
    capacities = body.get("capacities")
    if not isinstance(capacities, dict) or set(capacities) != {t["id"] for t in r["tables"]} or any(type(c) is not int or not 1 <= c <= 100 for c in capacities.values()):
        fail()
    policy = {k: copy.deepcopy(body[k]) for k in ("effective_from", "slot_minutes", "reservation_duration_minutes", "cancellation_cutoff_minutes", "capacities")}
    policy["opening_hours"] = [{k: h[k] for k in ("weekday", "opens", "closes")} for h in hours]
    return policy


def changes_between(old, new):
    changes = []
    before, after = table_ids(old) if old else None, table_ids(new)
    if before is None or set(before) != set(after):
        pair = len(after) > 1 or (before is not None and len(before) > 1)
        changes.append({"field": "table_ids" if pair else "table_id", "from": before if pair else (before[0] if before else None), "to": after if pair else after[0]})
    for key in ("starts_at_local", "party_size"):
        if old is None or old[key] != new[key]:
            changes.append({"field": key, "from": old[key] if old else None, "to": new[key]})
    return changes


def record(res, event, old=None, state=None):
    state = STATE if state is None else state
    entries = state["histories"].setdefault(res["reference"], [])
    at = res["created_at"] if event == "created" else datetime.now(UTC).isoformat()
    if entries and instant(at) < instant(entries[-1]["at"]):
        at = entries[-1]["at"]
    entries.append(copy.deepcopy({"seq": len(entries) + 1, "at": at, "event": event, "changes": [] if event == "cancelled" else changes_between(old, res), "revision": res["revision"], "accepted_terms": res["accepted_terms"]}))


def bump_restaurant(rid):
    STATE["restaurant_revisions"][rid] = STATE["restaurant_revisions"].get(rid, 0) + 1


def update_series(changed_refs, exception=True):
    for series in STATE["series"].values():
        touched = False
        for occurrence in series["occurrences"]:
            if occurrence["reference"] in changed_refs:
                touched = True
                if exception:
                    occurrence["exception"] = True
        if touched:
            series["revision"] += 1


def series_response(series):
    return {k: copy.deepcopy(series[k]) for k in ("series_id", "revision", "interval_weeks")} | {"occurrences": [{**o, "reservation": visible(STATE["reservations"][o["reference"]])} for o in series["occurrences"]]}


def new_reservation(fields, uid):
    ref = secrets.token_hex(5).upper()
    while ref in STATE["reservations"]:
        ref = secrets.token_hex(5).upper()
    return {**fields, "reservation_id": secrets.token_hex(16), "reference": ref, "user_id": uid, "status": "confirmed", "created_at": datetime.now(UTC).isoformat(), "revision": 1}


def interval(body):
    rid = identifier(body, "restaurant_id")
    if "table_id" in body and "table_ids" in body:
        fail()
    if "table_ids" in body:
        tids = body["table_ids"]
        if not isinstance(tids, list) or any(not isinstance(t, str) for t in tids):
            fail(400, "malformed_request")
        if not tids or any(not 1 <= len(t) <= 64 for t in tids) or len(set(tids)) != len(tids):
            fail()
        if len(tids) > 2:
            fail(422, "combination_not_allowed")
    else:
        tids = [identifier(body, "table_id")]
    party = body.get("party_size")
    if type(party) is not int or party < 1:
        fail()
    value = string(body, "starts_at_local")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", value):
        fail()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        fail()
    original = restaurant(rid)
    terms = policy_for(original, value[:10])
    r = policy_restaurant(original, terms)
    tables = {t["id"]: t for t in r["tables"]}
    if any(t not in tables for t in tids):
        fail(404, "not_found")
    if len(tids) == 2 and not any(set(pair) == set(tids) for pair in r.get("combinable", [])):
        fail(422, "combination_not_allowed")
    if len(tids) == 2:
        tids = next(list(pair) for pair in r["combinable"] if set(pair) == set(tids))
    if party > sum(tables[t]["capacity"] for t in tids):
        fail(422, "party_exceeds_capacity")
    zone = ZoneInfo(r["timezone"])
    start = local_time(value, zone)
    end = (start.astimezone(UTC) + timedelta(minutes=r["reservation_duration_minutes"])).astimezone(zone)
    hours = next((h for h in r["opening_hours"] if h["weekday"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][start.weekday()]), None)
    if hours is None:
        fail(422, "outside_opening_hours")
    opening = local_time(value[:10] + "T" + hours["opens"], zone)
    closing = local_time(value[:10] + "T" + hours["closes"], zone)
    if start.astimezone(UTC) < opening.astimezone(UTC) or end.astimezone(UTC) > closing.astimezone(UTC):
        fail(422, "outside_opening_hours")
    minutes = (start.replace(tzinfo=None) - opening.replace(tzinfo=None)).total_seconds() / 60
    if minutes % r["slot_minutes"]:
        fail(422, "not_on_slot_grid")
    return {"restaurant_id": rid, "table_ids": tids, **({"table_id": tids[0]} if len(tids) == 1 else {}), "party_size": party, "starts_at_local": value, "starts_at": start.isoformat(), "ends_at": end.isoformat(), "accepted_terms": terms}


def table_ids(res):
    return res["table_ids"] if "table_ids" in res else [res["table_id"]]


def overlaps(a, b):
    return (a["restaurant_id"] == b["restaurant_id"] and bool(set(table_ids(a)) & set(table_ids(b)))
            and instant(a["starts_at"]) < instant(b["ends_at"])
            and instant(b["starts_at"]) < instant(a["ends_at"]))


def check_free(candidates, excluded=()):
    occupied = [r for ref, r in STATE["reservations"].items() if ref not in excluded and r["status"] == "confirmed"]
    for candidate in candidates:
        if any(overlaps(candidate, other) for other in occupied):
            fail(409, "table_unavailable")
        occupied.append(candidate)


def visible(res):
    return {k: v for k, v in res.items() if k != "user_id"}


def owned(ref, uid):
    res = STATE["reservations"].get(ref)
    if res is None or res["user_id"] != uid:
        fail(404, "not_found")
    return res


def cutoff(res):
    r = res["accepted_terms"]
    if datetime.now(UTC) >= instant(res["starts_at"]) - timedelta(minutes=r["cancellation_cutoff_minutes"]):
        fail(409, "cutoff_passed")


def amendment(res, changes):
    if "expected_revision" in changes:
        expected = changes["expected_revision"]
        if type(expected) is not int or expected < 1:
            fail()
        if expected != res["revision"]:
            fail(409, "stale_revision")
    if res["status"] == "cancelled":
        fail(409, "reservation_cancelled")
    cutoff(res)
    fields = {k: changes.get(k, res[k]) for k in ("party_size", "starts_at_local")}
    if "table_id" in changes or "table_ids" in changes:
        fields.update({k: changes[k] for k in ("table_id", "table_ids") if k in changes})
    else:
        fields["table_ids"] = table_ids(res)
    fields["restaurant_id"] = res["restaurant_id"]
    ids = fields.get("table_ids", [fields.get("table_id")])
    if (not ("table_id" in fields and "table_ids" in fields) and isinstance(ids, list)
            and all(isinstance(t, str) for t in ids) and len(ids) == len(set(ids))
            and set(ids) == set(table_ids(res)) and type(fields["party_size"]) is int
            and fields["party_size"] == res["party_size"] and fields["starts_at_local"] == res["starts_at_local"]):
        return copy.deepcopy(res)
    return {**{k: v for k, v in res.items() if k not in ("table_id", "table_ids")}, **interval(fields), "revision": res["revision"] + 1}


def reset(body):
    for collection in ("users", "restaurants", "reservations"):
        if collection in body and not isinstance(body[collection], list):
            fail(400, "malformed_request")
        for item in body.get(collection, []):
            if not isinstance(item, dict):
                fail(400, "malformed_request")
            identifier(item, "id")
    for r in body.get("restaurants", []):
        for table in r["tables"]:
            identifier(table, "id")
    for seed in body.get("reservations", []):
        identifier(seed, "user_id")
        if not re.fullmatch(r"[A-Z0-9]{6,12}", string(seed, "reference")):
            fail()
    new = {key: {} for key in STATE_KEYS}
    for u in body.get("users", []):
        new["users"][u["id"]] = {k: u[k] for k in ("id", "email", "display_name")}
        new["users"][u["id"]]["password_hash"] = password_hash(u["password"])
    new["restaurants"] = {r["id"]: copy.deepcopy(r) for r in body.get("restaurants", [])}
    old = copy.deepcopy(STATE)
    STATE.clear()
    STATE.update(new)
    try:
        for seed in body.get("reservations", []):
            if seed["user_id"] not in STATE["users"] or seed["reference"] in STATE["reservations"]:
                fail()
            res = {**interval(seed), "reservation_id": seed["id"], "reference": seed["reference"], "user_id": seed["user_id"], "status": seed.get("status", "confirmed"), "created_at": seed.get("created_at", datetime.now(UTC).isoformat())}
            if res["status"] == "confirmed":
                check_free([res])
            STATE["reservations"][res["reference"]] = res
            res["revision"] = 1
            record(res, "created")
    except Exception:
        STATE.clear()
        STATE.update(old)
        raise


def validate_import(body):
    try:
        assert body["track"] == "tablekeeper" and type(body["format_version"]) is int and body["format_version"] == 1
        state = body["state"]
        assert set(state) in ({"users", "tokens", "restaurants", "reservations", "receipts"}, set(STATE_KEYS))
        assert all(isinstance(v, dict) for v in state.values())
        for uid, u in state["users"].items():
            assert u["id"] == uid and isinstance(u["email"], str) and isinstance(u["display_name"], str)
            salt, digest = u["password_hash"].split(":")
            assert len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 64
        assert all(uid in state["users"] for uid in state["tokens"].values())
        for rid, r in state["restaurants"].items():
            assert r["id"] == rid and isinstance(r["name"], str)
            ZoneInfo(r["timezone"])
            assert all(type(r[k]) is int and r[k] > 0 for k in ("slot_minutes", "reservation_duration_minutes"))
            assert isinstance(r["opening_hours"], list) and isinstance(r["tables"], list)
        for ref, r in state["reservations"].items():
            assert r["reference"] == ref and r["user_id"] in state["users"] and r["restaurant_id"] in state["restaurants"]
            assert r["status"] in ("confirmed", "cancelled") and instant(r["ends_at"]) > instant(r["starts_at"])
            assert isinstance(r["reservation_id"], str) and isinstance(r["party_size"], int)
            instant(r["created_at"])
        for receipt in state["receipts"].values():
            assert set(receipt) == {"body", "response"} and isinstance(receipt["body"], dict) and isinstance(receipt["response"], dict)
    except (KeyError, TypeError, ValueError, AssertionError):
        fail()
    result = copy.deepcopy(state)
    for key in STATE_KEYS:
        result.setdefault(key, {})
    for res in result["reservations"].values():
        res["table_ids"] = table_ids(res)
        res.setdefault("revision", 1)
        res.setdefault("accepted_terms", fixture_terms(result["restaurants"][res["restaurant_id"]]))
        if res["reference"] not in result["histories"]:
            record(res, "created", state=result)
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def dispatch(self, method, path, query, body):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}
        if method == "POST" and path == "/_test/reset":
            reset(body)
            return 204, None
        if method == "GET" and path == "/_test/export":
            return 200, {"track": "tablekeeper", "format_version": 1, "state": copy.deepcopy(STATE)}
        if method == "POST" and path == "/_test/import":
            state = validate_import(body)
            STATE.clear()
            STATE.update(state)
            return 204, None
        if method == "POST" and path in ("/auth/signup", "/auth/login"):
            email, password = string(body, "email"), string(body, "password")
            user = next((u for u in STATE["users"].values() if u["email"] == email), None)
            if path.endswith("signup"):
                name = string(body, "display_name")
                if not re.fullmatch(r"[^@\s]+@[^@\s]+", email) or len(password) < 8:
                    fail()
                if user:
                    fail(409, "email_taken")
                uid = secrets.token_hex(16)
                user = {"id": uid, "email": email, "display_name": name, "password_hash": password_hash(password)}
                STATE["users"][uid] = user
            elif not user or not hmac.compare_digest(user["password_hash"], password_hash(password, user["password_hash"].split(":")[0])):
                fail(401, "unauthenticated")
            token = secrets.token_urlsafe(32)
            STATE["tokens"][token] = user["id"]
            return (201 if path.endswith("signup") else 200), {"user_id": user["id"], "display_name": user["display_name"], "token": token}
        if method == "GET" and path == "/restaurants":
            return 200, {"restaurants": [{k: r[k] for k in ("id", "name", "timezone")} for r in STATE["restaurants"].values()]}
        if method == "GET" and path.startswith("/restaurants/"):
            if path.endswith("/policies"):
                rid = path.split("/")[2]
                restaurant(rid)
                return 200, {"policies": STATE["policies"].get(rid, [])}
            return 200, restaurant(path.split("/")[-1])
        if method == "GET" and path == "/availability":
            if "explain" in query and query["explain"] != ["true"]:
                fail()
            rid, date, party = (query.get(k, [""])[0] for k in ("restaurant_id", "date", "party_size"))
            if not rid or len(rid) > 64 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not re.fullmatch(r"[0-9]+", party):
                fail()
            try:
                day = datetime.strptime(date, "%Y-%m-%d")
                party = int(party)
            except ValueError:
                fail()
            if party < 1:
                fail()
            original = restaurant(rid)
            terms = policy_for(original, date)
            r = policy_restaurant(original, terms)
            slots = []
            hours = next((h for h in r["opening_hours"] if h["weekday"] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][day.weekday()]), None)
            if hours:
                cursor = datetime.fromisoformat(date + "T" + hours["opens"])
                close = datetime.fromisoformat(date + "T" + hours["closes"])
                while cursor < close:
                    value = cursor.isoformat(timespec="minutes")
                    try:
                        zone = ZoneInfo(r["timezone"])
                        start = local_time(value, zone)
                        end = start.astimezone(UTC) + timedelta(minutes=r["reservation_duration_minutes"])
                        if end <= local_time(date + "T" + hours["closes"], zone).astimezone(UTC):
                            free = []
                            explanations = []
                            for table in r["tables"]:
                                candidate = {"restaurant_id": rid, "table_id": table["id"], "starts_at": start.isoformat(), "ends_at": end.isoformat()}
                                capacity_ok = table["capacity"] >= party
                                overlap_ok = not any(b["status"] == "confirmed" and overlaps(candidate, b) for b in STATE["reservations"].values())
                                explanations.append({"table_id": table["id"], "policy_version": terms["policy_version"], "available": capacity_ok and overlap_ok, "rules": [{"rule": "capacity", "holds": capacity_ok}, {"rule": "no_overlap", "holds": overlap_ok}]})
                                if capacity_ok and overlap_ok:
                                    free.append(table["id"])
                            options = [{"table_ids": [t["id"]], "capacity": t["capacity"]} for t in r["tables"] if t["id"] in free]
                            capacities = {t["id"]: t["capacity"] for t in r["tables"]}
                            for pair in r.get("combinable", []):
                                candidate = {"restaurant_id": rid, "table_ids": pair, "starts_at": start.isoformat(), "ends_at": end.isoformat()}
                                capacity = sum(capacities[t] for t in pair)
                                if capacity >= party and not any(b["status"] == "confirmed" and overlaps(candidate, b) for b in STATE["reservations"].values()):
                                    options.append({"table_ids": pair, "capacity": capacity})
                            slots.append({"starts_at_local": value, "starts_at": start.isoformat(), "available_table_ids": free, "available_options": options})
                            if "explain" in query:
                                slots[-1]["explain"] = explanations
                    except APIError as error:
                        if error.code != "invalid_local_time":
                            raise
                    cursor += timedelta(minutes=r["slot_minutes"])
            return 200, {"restaurant_id": rid, "date": date, "timezone": r["timezone"], "slots": slots}
        auth = self.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([^\s]+)", auth, re.I)
        uid = STATE["tokens"].get(match[1]) if match else None
        if uid is None:
            if method == "GET" and (path.startswith("/series/") or (path.startswith("/reservations/") and path.split("/")[-1] in ("history", "decision"))):
                fail(404, "not_found")
            fail(401, "unauthenticated")
        receipt_key = None
        policy_path = re.fullmatch(r"/restaurants/([^/]+)/policies", path)
        if method == "POST" and (path in ("/reservations", "/reservation-moves", "/series") or policy_path):
            key = self.headers.get("Idempotency-Key", "")
            if not key:
                fail(400, "missing_idempotency_key")
            if len(key) > 255:
                fail()
            receipt_key = json.dumps([uid, method, path, key])
            receipt = STATE["receipts"].get(receipt_key)
            if receipt:
                if json.dumps(receipt["body"], sort_keys=True) != json.dumps(body, sort_keys=True):
                    fail(409, "idempotency_key_reuse")
                return 200, receipt["response"]
        if method == "POST" and policy_path:
            rid = policy_path[1]
            r = restaurant(rid)
            if uid not in r.get("manager_user_ids", []):
                fail(403, "forbidden")
            policy = validate_policy(body, r)
            policies = STATE["policies"].setdefault(rid, [])
            policy["policy_version"] = len(policies) + 1
            policies.append(policy)
            bump_restaurant(rid)
            response = copy.deepcopy(policy)
        elif method == "POST" and path == "/series":
            count, weeks = body.get("count"), body.get("interval_weeks")
            if type(count) is not int or not 2 <= count <= 12 or type(weeks) is not int or not 1 <= weeks <= 4:
                fail()
            anchor = owned(string(body, "anchor_reference"), uid)
            if anchor["status"] == "cancelled":
                fail(409, "reservation_cancelled")
            cutoff(anchor)
            if any(o["reference"] == anchor["reference"] for s in STATE["series"].values() for o in s["occurrences"]):
                fail(409, "already_in_series")
            generated = []
            local = datetime.fromisoformat(anchor["starts_at_local"])
            for i in range(1, count):
                value = (local + timedelta(weeks=i * weeks)).isoformat(timespec="minutes")
                fields = interval({"restaurant_id": anchor["restaurant_id"], "table_ids": table_ids(anchor), "party_size": anchor["party_size"], "starts_at_local": value})
                candidate = new_reservation(fields, uid)
                check_free(generated + [candidate])
                generated.append(candidate)
            sid = secrets.token_hex(16)
            series = {"series_id": sid, "user_id": uid, "revision": 1, "interval_weeks": weeks, "occurrences": [{"index": i, "reference": r["reference"], "exception": False} for i, r in enumerate([anchor] + generated)]}
            for res in generated:
                STATE["reservations"][res["reference"]] = res
                record(res, "created")
            STATE["series"][sid] = series
            bump_restaurant(anchor["restaurant_id"])
            response = series_response(series)
        elif method == "GET" and path.startswith("/series/"):
            series = STATE["series"].get(path.split("/")[-1])
            if series is None or series["user_id"] != uid:
                fail(404, "not_found")
            return 200, series_response(series)
        elif method == "POST" and path == "/reservations":
            res = interval(body)
            check_free([res])
            res = new_reservation(res, uid)
            STATE["reservations"][res["reference"]] = res
            record(res, "created")
            bump_restaurant(res["restaurant_id"])
            response = visible(res)
        elif method == "POST" and path == "/reservation-moves":
            moves = body.get("moves")
            if not isinstance(moves, list) or not 1 <= len(moves) <= 8 or any(not isinstance(m, dict) or not isinstance(m.get("reference"), str) for m in moves):
                fail()
            refs = [m["reference"] for m in moves]
            if len(set(refs)) != len(refs):
                fail()
            candidates = []
            for move in moves:
                res = owned(move["reference"], uid)
                if candidates and res["restaurant_id"] != candidates[0]["restaurant_id"]:
                    fail()
                candidates.append(amendment(res, move))
            check_free(candidates, refs)
            changed = []
            for res in candidates:
                old = STATE["reservations"][res["reference"]]
                if res["revision"] != old["revision"]:
                    record(res, "changed", old)
                    changed.append(res["reference"])
                STATE["reservations"][res["reference"]] = res
            if changed:
                update_series(changed)
                bump_restaurant(candidates[0]["restaurant_id"])
            response = {"reservations": [visible(r) for r in candidates]}
        elif method == "GET" and path == "/reservations":
            return 200, {"reservations": [visible(r) for r in sorted(STATE["reservations"].values(), key=lambda r: instant(r["starts_at"]), reverse=True) if r["user_id"] == uid]}
        elif path.startswith("/reservations/"):
            parts = path.split("/")
            res = owned(parts[2], uid)
            if method == "GET" and len(parts) == 3:
                return 200, visible(res)
            if method == "GET" and len(parts) == 4 and parts[3] == "history":
                return 200, {"reference": res["reference"], "entries": STATE["histories"][res["reference"]]}
            if method == "GET" and len(parts) == 4 and parts[3] == "decision":
                return 200, {k: res[k] for k in ("reference", "revision", "accepted_terms")}
            if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                if res["status"] != "cancelled":
                    cutoff(res)
                    res["status"] = "cancelled"
                    res["revision"] += 1
                    record(res, "cancelled")
                    update_series([res["reference"]], exception=False)
                    bump_restaurant(res["restaurant_id"])
                return 200, visible(res)
            if method == "PATCH" and len(parts) == 3:
                candidate = amendment(res, body)
                check_free([candidate], [res["reference"]])
                if candidate["revision"] != res["revision"]:
                    record(candidate, "changed", res)
                    update_series([res["reference"]])
                    bump_restaurant(res["restaurant_id"])
                STATE["reservations"][res["reference"]] = candidate
                return 200, visible(candidate)
            fail(404, "not_found")
        else:
            fail(404, "not_found")
        STATE["receipts"][receipt_key] = copy.deepcopy({"body": body, "response": response})
        return 201, response

    def handle_request(self):
        route = urlsplit(self.path).path
        if self.command == "GET" and route in ("/", "/login", "/signup", "/lookup", "/app.js", "/style.css"):
            filename = route[1:] if route in ("/app.js", "/style.css") else "index.html"
            payload = Path(__file__).with_name(filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[filename] + "; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        try:
            body = {}
            if self.command in ("POST", "PATCH", "PUT"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length)
                    body = json.loads(raw, parse_constant=lambda _: fail(400, "malformed_request")) if raw else {}
                except (ValueError, UnicodeError):
                    fail(400, "malformed_request")
                if not isinstance(body, dict):
                    fail(400, "malformed_request")
            url = urlsplit(self.path)
            with LOCK:
                status, response = self.dispatch(self.command, unquote(url.path), parse_qs(url.query, keep_blank_values=True), body)
                payload = json.dumps(response, ensure_ascii=False).encode() if response is not None else b""
        except APIError as error:
            status = error.status
            payload = json.dumps({"error": {"code": error.code, "message": error.code.replace("_", " ")}}).encode()
        except (KeyError, TypeError, ValueError, OverflowError):
            status = 422
            payload = b'{"error":{"code":"validation_failed","message":"Invalid request"}}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = handle_request


if __name__ == "__main__":
    ThreadingHTTPServer.request_queue_size = 128
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
