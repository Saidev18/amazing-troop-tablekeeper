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
STATE = {"users": {}, "tokens": {}, "restaurants": {}, "reservations": {}, "receipts": {}}


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
    r = restaurant(rid)
    tables = {t["id"]: t for t in r["tables"]}
    if any(t not in tables for t in tids):
        fail(404, "not_found")
    if len(tids) == 2 and not any(set(pair) == set(tids) for pair in r.get("combinable", [])):
        fail(422, "combination_not_allowed")
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
    return {"restaurant_id": rid, "table_ids": tids, **({"table_id": tids[0]} if len(tids) == 1 else {}), "party_size": party, "starts_at_local": value, "starts_at": start.isoformat(), "ends_at": end.isoformat()}


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
    r = restaurant(res["restaurant_id"])
    if datetime.now(UTC) >= instant(res["starts_at"]) - timedelta(minutes=r["cancellation_cutoff_minutes"]):
        fail(409, "cutoff_passed")


def amendment(res, changes):
    if res["status"] == "cancelled":
        fail(409, "reservation_cancelled")
    cutoff(res)
    fields = {k: changes.get(k, res[k]) for k in ("party_size", "starts_at_local")}
    if "table_id" in changes or "table_ids" in changes:
        fields.update({k: changes[k] for k in ("table_id", "table_ids") if k in changes})
    else:
        fields["table_ids"] = table_ids(res)
    fields["restaurant_id"] = res["restaurant_id"]
    return {**{k: v for k, v in res.items() if k not in ("table_id", "table_ids")}, **interval(fields)}


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
    new = {"users": {}, "tokens": {}, "restaurants": {}, "reservations": {}, "receipts": {}}
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
    except Exception:
        STATE.clear()
        STATE.update(old)
        raise


def validate_import(body):
    try:
        assert body["track"] == "tablekeeper" and type(body["format_version"]) is int and body["format_version"] == 1
        state = body["state"]
        assert set(state) == set(STATE)
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
    for res in result["reservations"].values():
        res["table_ids"] = table_ids(res)
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
            return 200, restaurant(path.split("/")[-1])
        if method == "GET" and path == "/availability":
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
            r = restaurant(rid)
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
                            for table in r["tables"]:
                                candidate = {"restaurant_id": rid, "table_id": table["id"], "starts_at": start.isoformat(), "ends_at": end.isoformat()}
                                if table["capacity"] >= party and not any(b["status"] == "confirmed" and overlaps(candidate, b) for b in STATE["reservations"].values()):
                                    free.append(table["id"])
                            options = [{"table_ids": [t["id"]], "capacity": t["capacity"]} for t in r["tables"] if t["id"] in free]
                            capacities = {t["id"]: t["capacity"] for t in r["tables"]}
                            for pair in r.get("combinable", []):
                                candidate = {"restaurant_id": rid, "table_ids": pair, "starts_at": start.isoformat(), "ends_at": end.isoformat()}
                                capacity = sum(capacities[t] for t in pair)
                                if capacity >= party and not any(b["status"] == "confirmed" and overlaps(candidate, b) for b in STATE["reservations"].values()):
                                    options.append({"table_ids": pair, "capacity": capacity})
                            slots.append({"starts_at_local": value, "starts_at": start.isoformat(), "available_table_ids": free, "available_options": options})
                    except APIError as error:
                        if error.code != "invalid_local_time":
                            raise
                    cursor += timedelta(minutes=r["slot_minutes"])
            return 200, {"restaurant_id": rid, "date": date, "timezone": r["timezone"], "slots": slots}
        auth = self.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([^\s]+)", auth, re.I)
        uid = STATE["tokens"].get(match[1]) if match else None
        if uid is None:
            fail(401, "unauthenticated")
        receipt_key = None
        if method == "POST" and path in ("/reservations", "/reservation-moves"):
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
        if method == "POST" and path == "/reservations":
            res = interval(body)
            check_free([res])
            ref = secrets.token_hex(5).upper()
            while ref in STATE["reservations"]:
                ref = secrets.token_hex(5).upper()
            res.update(reservation_id=secrets.token_hex(16), reference=ref, user_id=uid, status="confirmed", created_at=datetime.now(UTC).isoformat())
            STATE["reservations"][ref] = res
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
            for res in candidates:
                STATE["reservations"][res["reference"]] = res
            response = {"reservations": [visible(r) for r in candidates]}
        elif method == "GET" and path == "/reservations":
            return 200, {"reservations": [visible(r) for r in sorted(STATE["reservations"].values(), key=lambda r: instant(r["starts_at"]), reverse=True) if r["user_id"] == uid]}
        elif path.startswith("/reservations/"):
            parts = path.split("/")
            res = owned(parts[2], uid)
            if method == "GET" and len(parts) == 3:
                return 200, visible(res)
            if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                if res["status"] != "cancelled":
                    cutoff(res)
                    res["status"] = "cancelled"
                return 200, visible(res)
            if method == "PATCH" and len(parts) == 3:
                candidate = amendment(res, body)
                check_free([candidate], [res["reference"]])
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
