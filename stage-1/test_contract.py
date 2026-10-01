"""Additional HTTP checks for moves, snapshots and concurrent transactions.

Run: python test_contract.py http://127.0.0.1:18081
"""
import concurrent.futures
import copy
import json
import sys
import unittest
import urllib.error
import urllib.request

BASE = sys.argv.pop(1) if len(sys.argv) > 1 else "http://127.0.0.1:18081"


def request(method, path, body=None, token=None, key=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if key:
        headers["Idempotency-Key"] = key
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read()
        return response.status, json.loads(data) if data else None


class Contract(unittest.TestCase):
    def setUp(self):
        fixture = {"users": [{"id": "u", "email": "a@b", "password": "password", "display_name": "A"}], "restaurants": [{"id": "r", "name": "R", "timezone": "UTC", "slot_minutes": 30, "reservation_duration_minutes": 60, "cancellation_cutoff_minutes": 0, "opening_hours": [{"weekday": d, "opens": "18:00", "closes": "23:00"} for d in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]], "tables": [{"id": t, "capacity": 4, "label": t} for t in ("a", "b")]}], "reservations": []}
        self.assertEqual(request("POST", "/_test/reset", fixture)[0], 204)
        self.token = request("POST", "/auth/login", {"email": "a@b", "password": "password"})[1]["token"]

    def booking(self, table="a", key="book"):
        return request("POST", "/reservations", {"restaurant_id": "r", "table_id": table, "party_size": 2, "starts_at_local": "2030-01-01T19:00"}, self.token, key)

    def test_concurrent_retries_and_competitors(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            results = list(pool.map(lambda _: self.booking(), range(50)))
        self.assertEqual([s for s, _ in results].count(201), 1)
        self.assertEqual([s for s, _ in results].count(200), 49)
        self.assertTrue(all(r == results[0][1] for _, r in results))
        self.assertEqual(self.booking(key="competitor")[0], 409)

    def test_move_swap_rollback_and_snapshot_receipts(self):
        a, b = self.booking()[1], self.booking("b", "book-b")[1]
        moves = {"moves": [{"reference": a["reference"], "table_id": "b"}, {"reference": b["reference"], "table_id": "a"}]}
        status, receipt = request("POST", "/reservation-moves", moves, self.token, "swap")
        self.assertEqual(status, 201)
        bad = {"moves": [{"reference": a["reference"], "table_id": "a"}, {"reference": b["reference"], "party_size": 100}]}
        self.assertEqual(request("POST", "/reservation-moves", bad, self.token, "failed")[0], 422)
        self.assertEqual(request("GET", "/reservations/" + a["reference"], token=self.token)[1]["table_id"], "b")
        snapshot = request("GET", "/_test/export")[1]
        self.assertNotIn('"password":', json.dumps(snapshot))
        self.assertEqual(request("POST", "/_test/reset", {})[0], 204)
        self.assertEqual(request("POST", "/_test/import", snapshot)[0], 204)
        self.assertEqual(request("POST", "/reservation-moves", moves, self.token, "swap"), (200, receipt))
        self.assertEqual(self.booking(), (200, a))
        self.assertEqual(request("POST", "/auth/login", {"email": "a@b", "password": "password"})[0], 200)
        self.assertEqual(request("POST", "/reservation-moves", moves, self.token, "failed")[0], 201)
        invalid = copy.deepcopy(snapshot)
        invalid["state"]["users"]["u"]["password_hash"] = "invalid"
        before = request("GET", "/_test/export")[1]
        self.assertEqual(request("POST", "/_test/import", invalid)[0], 422)
        self.assertEqual(request("GET", "/_test/export")[1], before)


if __name__ == "__main__":
    unittest.main()
