"""Additional Stage 4 HTTP transaction, planning and migration checks."""
import concurrent.futures
import copy
import itertools
import unittest
import test_contract
from test_contract import Contract, request


class RepairsAndSeries(Contract):
    def setUp(self):
        super().setUp()
        snapshot = request('GET', '/_test/export')[1]
        r = snapshot['state']['restaurants']['r']
        r['manager_user_ids'] = ['u']
        r['tables'].append({'id': 'c', 'capacity': 6, 'label': 'C'})
        r['combinable'] = [['a', 'b'], ['b', 'c']]
        other = copy.deepcopy(r)
        other['id'] = 'other'
        snapshot['state']['restaurants']['other'] = other
        self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)

    def preview(self, key='preview', table='a', rid='r', start='2030-01-01T18:00:00+00:00', end='2030-01-01T23:00:00+00:00'):
        return request('POST', '/restaurants/' + rid + '/replans', {'table_id': table, 'from': start, 'to': end}, self.token, key)

    def apply(self, plan, key='apply', rid='r'):
        return request('POST', '/restaurants/' + rid + '/replans/' + plan['plan_id'] + '/apply', {}, self.token, key)

    def series(self, count=3):
        anchor = self.booking()[1]
        status, series = request('POST', '/series', dict(anchor_reference=anchor['reference'], count=count, interval_weeks=1), self.token, 'series')
        self.assertEqual(status, 201)
        return series

    def amend(self, series, time='20:00', key='amend', revision=None):
        return request('POST', '/series/' + series['series_id'] + '/amend', dict(expected_revision=series['revision'] if revision is None else revision, from_index=0, local_time=time), self.token, key)

    def test_preview_optimizes_and_apply_records_closure(self):
        a, b = self.booking()[1], self.booking('b', 'b')[1]
        before = request('GET', '/_test/export')[1]['state']
        status, plan = self.preview()
        self.assertEqual(status, 201)
        self.assertEqual(plan['restaurant_revision'], 2)
        after = request('GET', '/_test/export')[1]['state']
        for key in ('reservations', 'histories', 'closures', 'restaurant_revisions'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(plan['moved_count'], 1)
        self.assertEqual(plan['unused_seats'], 6)
        assigned = {a['reference']: ['c'], b['reference']: ['b']}
        self.assertEqual({x['reference']: x['table_ids'] for x in plan['assignments']}, assigned)
        status, applied = self.apply(plan)
        self.assertEqual(status, 201)
        self.assertEqual(applied['restaurant_revision'], 3)
        moved = next(r for r in applied['reservations'] if r['reference'] == a['reference'])
        for key in ('starts_at', 'ends_at', 'accepted_terms', 'party_size', 'created_at'):
            self.assertEqual(moved[key], a[key])
        self.assertEqual(moved['revision'], 2)
        entries = request('GET', '/reservations/' + a['reference'] + '/history', token=self.token)[1]['entries']
        self.assertEqual(entries[-1]['event'], 'reassigned')
        self.assertEqual(entries[-1]['plan_id'], plan['plan_id'])
        self.assertEqual(entries[-1]['changes'], [{'field': 'table_ids', 'from': ['a'], 'to': ['c']}])
        self.assertEqual(self.apply(plan), (200, applied))
        self.assertEqual(self.apply(plan, 'different')[1]['error']['code'], 'plan_already_applied')
        self.assertEqual(self.booking(key='closed')[1]['error']['code'], 'table_unavailable')
        slot = request('GET', '/availability?restaurant_id=r&date=2030-01-01&party_size=2&explain=true')[1]['slots'][0]
        self.assertFalse(slot['explain'][0]['rules'][1]['holds'])
        self.assertFalse(any('a' in o['table_ids'] for o in slot['available_options']))

    def test_stale_and_restaurant_isolation(self):
        plan = self.preview()[1]
        other = self.preview(key='other', rid='other')[1]
        self.assertEqual(self.apply(other, 'other-apply', rid='other')[0], 201)
        self.assertEqual(self.apply(plan)[0], 201)
        plan = self.preview(key='new', table='b')[1]
        self.booking('b', 'new-book')
        before = request('GET', '/_test/export')[1]
        self.assertEqual(self.apply(plan, 'stale')[1]['error']['code'], 'stale_plan')
        self.assertEqual(request('GET', '/_test/export')[1], before)

    def test_planner_matches_exhaustive_objective(self):
        snapshot = request('GET', '/_test/export')[1]
        r = snapshot['state']['restaurants']['r']
        r['tables'] += [{'id': t, 'label': t, 'capacity': 2} for t in ('d', 'e')]
        request('POST', '/_test/import', snapshot)
        bookings = [self.booking(t, t)[1] for t in ('a', 'b', 'c')]
        bookings.sort(key=lambda b: b['reference'])
        options = [[t['id']] for t in r['tables']] + r['combinable']
        feasible = []
        for ranks in itertools.product(range(len(options)), repeat=len(bookings)):
            sets = [options[rank] for rank in ranks]
            if any('a' in ids for ids in sets):
                continue
            if any(set(sets[i]) & set(sets[j]) for i in range(3) for j in range(i)):
                continue
            capacity = [sum(b['accepted_terms']['capacities'][t] for t in ids) for b, ids in zip(bookings, sets)]
            if any(c < b['party_size'] for c, b in zip(capacity, bookings)):
                continue
            score = (sum(set(ids) != set(b['table_ids']) for ids, b in zip(sets, bookings)), sum(c - b['party_size'] for c, b in zip(capacity, bookings)), ranks)
            feasible.append(score)
        expected = min(feasible)
        plan = self.preview()[1]
        self.assertEqual((plan['moved_count'], plan['unused_seats'], tuple(options.index(a['table_ids']) for a in plan['assignments'])), expected)

    def test_closure_rejects_invalid_offset(self):
        self.assertEqual(self.preview(start='2030-01-01T18:00:00+01:99')[0], 422)

    def test_no_feasible_plan_changes_nothing_and_failed_key_reusable(self):
        self.booking()
        self.booking('b', 'b')
        self.booking('c', 'c')
        before = request('GET', '/_test/export')[1]
        self.assertEqual(self.preview()[1]['error']['code'], 'no_feasible_plan')
        self.assertEqual(request('GET', '/_test/export')[1], before)
        self.assertEqual(self.preview(start='2030-01-01T20:00:00Z')[0], 201)

    def test_series_amend_noop_race_and_repair_exceptions(self):
        series = self.series()
        noop = self.amend(series, time='19:00', key='noop')
        self.assertEqual(noop, (201, series))
        plan = self.preview()[1]
        self.assertEqual(self.apply(plan)[0], 201)
        current = request('GET', '/series/' + series['series_id'], token=self.token)[1]
        self.assertEqual(current['revision'], 2)
        self.assertTrue(all(not o['exception'] for o in current['occurrences']))
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda t: self.amend(current, time=t, key=t), ['20:00', '21:00']))
        self.assertEqual(sorted(s for s, _ in results), [201, 409])
        current = request('GET', '/series/' + series['series_id'], token=self.token)[1]
        self.assertEqual(current['revision'], 3)
        self.assertTrue(all(not o['exception'] for o in current['occurrences']))
        winning = next(r for s, r in results if s == 201)
        time = winning['occurrences'][0]['reservation']['starts_at_local'][11:]
        self.assertEqual(self.amend(series, time=time, key=time, revision=2), (200, winning))

    def test_series_amend_conflict_rolls_back(self):
        series = self.series()
        request('POST', '/reservations', dict(restaurant_id='r', table_id='a', party_size=2, starts_at_local='2030-01-15T20:00'), self.token, 'block')
        before = request('GET', '/_test/export')[1]
        self.assertEqual(self.amend(series)[1]['error']['code'], 'table_unavailable')
        self.assertEqual(request('GET', '/_test/export')[1], before)
        self.assertEqual(self.amend(series, time='21:00')[0], 201)

    def test_import_stage3_series_with_exceptions_and_cancellation(self):
        destination = test_contract.BASE
        try:
            test_contract.BASE = 'http://127.0.0.1:18083'
            Contract.setUp(self)
            series = self.series()
            refs = [o['reference'] for o in series['occurrences']]
            request('PATCH', '/reservations/' + refs[1], {'starts_at_local': '2030-01-09T19:00'}, self.token)
            request('POST', '/reservations/' + refs[2] + '/cancel', {}, self.token)
            snapshot = request('GET', '/_test/export')[1]
        finally:
            test_contract.BASE = destination
        self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)
        current = request('GET', '/series/' + series['series_id'], token=self.token)[1]
        status, amended = self.amend(current)
        self.assertEqual(status, 201)
        self.assertTrue(amended['occurrences'][0]['reservation']['starts_at_local'].endswith('T20:00'))
        self.assertEqual(amended['occurrences'][1]['reservation']['starts_at_local'], '2030-01-09T19:00')
        self.assertTrue(amended['occurrences'][1]['exception'])
        self.assertEqual(amended['occurrences'][2]['reservation']['status'], 'cancelled')
        body = dict(anchor_reference=refs[0], count=3, interval_weeks=1)
        self.assertEqual(request('POST', '/series', body, self.token, 'series'), (200, series))


if __name__ == '__main__':
    unittest.main()
