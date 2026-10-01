"""Stage 3 transaction and policy regressions against a running container."""
import concurrent.futures
import unittest
import test_contract
from test_contract import Contract, request


class PoliciesAndSeries(Contract):
    def setUp(self):
        super().setUp()
        snapshot = request('GET', '/_test/export')[1]
        r = snapshot['state']['restaurants']['r']
        r['manager_user_ids'] = ['u']
        r['combinable'] = [['a', 'b']]
        self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)
        self.hours = r['opening_hours']

    def policy(self, date='2030-01-01', key='policy', **changes):
        body = dict(effective_from=date, slot_minutes=30, reservation_duration_minutes=90,
                    cancellation_cutoff_minutes=0, opening_hours=self.hours, capacities={'a': 4, 'b': 4})
        body.update(changes)
        return request('POST', '/restaurants/r/policies', body, self.token, key)

    def history(self, ref):
        return request('GET', '/reservations/' + ref + '/history', token=self.token)[1]['entries']

    def test_policy_selection_snapshot_noop_and_stale(self):
        original = self.booking()[1]
        ref = original['reference']
        self.assertEqual(original['accepted_terms']['policy_version'], 0)
        self.assertEqual(self.policy(capacities={'a': 1, 'b': 4})[0], 201)
        noop = request('PATCH', '/reservations/' + ref, {'party_size': 2}, self.token)
        self.assertEqual(noop, (200, original))
        self.assertEqual(len(self.history(ref)), 1)
        self.assertEqual(request('PATCH', '/reservations/' + ref, {'starts_at_local': '2030-01-01T20:00'}, self.token)[0], 422)
        changed = request('PATCH', '/reservations/' + ref, {'table_id': 'b', 'expected_revision': 1}, self.token)[1]
        self.assertEqual(changed['revision'], 2)
        self.assertEqual(changed['accepted_terms']['policy_version'], 1)
        stale = request('PATCH', '/reservations/' + ref, {'party_size': False, 'expected_revision': 1}, self.token)
        self.assertEqual(stale[1]['error']['code'], 'stale_revision')
        entries = self.history(ref)
        self.assertEqual([e['accepted_terms']['policy_version'] for e in entries], [0, 1])
        self.assertEqual(self.booking(), (200, original))
        self.assertEqual(self.policy(key='older', date='2029-12-01')[1]['policy_version'], 2)
        decision = request('GET', '/availability?restaurant_id=r&date=2030-01-01&party_size=2&explain=true')[1]
        self.assertEqual(decision['slots'][0]['explain'][0]['policy_version'], 1)
        self.assertEqual(self.policy(key='same-date')[1]['policy_version'], 3)
        self.assertEqual(self.policy(key='same-date')[0], 200)
        self.assertEqual(self.policy(key='invalid', slot_minutes=True)[0], 422)
        self.assertEqual(self.policy(key='invalid')[1]['policy_version'], 4)

    def test_pair_history_and_revision_race(self):
        body = dict(restaurant_id='r', table_ids=['b', 'a'], party_size=6, starts_at_local='2030-01-01T19:00')
        pair = request('POST', '/reservations', body, self.token, 'pair')[1]
        ref = pair['reference']
        self.assertEqual(pair['table_ids'], ['a', 'b'])
        self.assertEqual(self.history(ref)[0]['changes'][0], {'field': 'table_ids', 'from': None, 'to': ['a', 'b']})
        self.assertEqual(request('PATCH', '/reservations/' + ref, {'table_ids': ['b', 'a']}, self.token), (200, pair))
        def amend(party):
            return request('PATCH', '/reservations/' + ref, {'party_size': party, 'expected_revision': 1}, self.token)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(amend, [4, 5]))
        self.assertEqual(sorted(s for s, _ in replies), [200, 409])
        self.assertEqual(len(self.history(ref)), 2)
        self.assertEqual(request('GET', '/reservations/' + ref + '/history')[0], 404)
        self.assertEqual(request('GET', '/reservations/' + ref + '/decision')[0], 404)

    def test_series_terms_batch_exceptions_cancel_and_receipt(self):
        anchor = self.booking()[1]
        self.policy(date='2030-01-08')
        body = dict(anchor_reference=anchor['reference'], count=3, interval_weeks=1)
        status, original = request('POST', '/series', body, self.token, 'series')
        self.assertEqual(status, 201)
        sid = original['series_id']
        occurrences = original['occurrences']
        self.assertEqual(occurrences[0]['reservation'], anchor)
        self.assertEqual([o['reservation']['accepted_terms']['policy_version'] for o in occurrences], [0, 1, 1])
        moves = {'moves': [{'reference': o['reference'], 'table_id': 'b', 'expected_revision': 1} for o in occurrences[:2]]}
        self.assertEqual(request('POST', '/reservation-moves', moves, self.token, 'moves')[0], 201)
        current = request('GET', '/series/' + sid, token=self.token)[1]
        self.assertEqual(current['revision'], 2)
        self.assertEqual([o['exception'] for o in current['occurrences']], [True, True, False])
        ref = occurrences[2]['reference']
        request('POST', '/reservations/' + ref + '/cancel', {}, self.token)
        request('POST', '/reservations/' + ref + '/cancel', {}, self.token)
        current = request('GET', '/series/' + sid, token=self.token)[1]
        self.assertEqual(current['revision'], 3)
        self.assertFalse(current['occurrences'][2]['exception'])
        self.assertEqual(request('POST', '/series', body, self.token, 'series'), (200, original))
        self.assertEqual(request('GET', '/series/' + sid)[0], 404)
        snapshot = request('GET', '/_test/export')[1]
        request('POST', '/_test/reset', {})
        self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)
        self.assertEqual(request('GET', '/series/' + sid, token=self.token)[1], current)
        self.assertEqual([e['event'] for e in self.history(ref)], ['created', 'cancelled'])

    def test_series_failure_has_no_partial_state(self):
        anchor = self.booking()[1]
        blocked = dict(restaurant_id='r', table_id='a', starts_at_local='2030-01-15T19:00', party_size=2)
        request('POST', '/reservations', blocked, self.token, 'blocked')
        before = request('GET', '/_test/export')[1]
        body = dict(anchor_reference=anchor['reference'], count=3, interval_weeks=1)
        response = request('POST', '/series', body, self.token, 'series')
        self.assertEqual(response[1]['error']['code'], 'table_unavailable')
        self.assertEqual(request('GET', '/_test/export')[1], before)
        body['count'] = 2
        self.assertEqual(request('POST', '/series', body, self.token, 'series')[0], 201)

    def test_explanations_report_both_failures(self):
        self.booking()
        data = request('GET', '/availability?restaurant_id=r&date=2030-01-01&party_size=8&explain=true')[1]
        slot = next(s for s in data['slots'] if s['starts_at_local'].endswith('19:00'))
        self.assertEqual(slot['explain'][0]['rules'], [{'rule': 'capacity', 'holds': False}, {'rule': 'no_overlap', 'holds': False}])
        for value in ('', 'false', '1'):
            self.assertEqual(request('GET', '/availability?restaurant_id=r&date=2030-01-01&party_size=2&explain=' + value)[0], 422)

    def test_dst_series_failure_rolls_back_everything(self):
        snapshot = request('GET', '/_test/export')[1]
        r = snapshot['state']['restaurants']['r']
        r['timezone'] = 'America/New_York'
        r['opening_hours'] = [{'weekday': 'sun', 'opens': '00:00', 'closes': '05:00'}]
        request('POST', '/_test/import', snapshot)
        body = dict(restaurant_id='r', table_id='a', party_size=2, starts_at_local='2030-03-03T02:00')
        anchor = request('POST', '/reservations', body, self.token, 'dst-anchor')[1]
        before = request('GET', '/_test/export')[1]
        reply = request('POST', '/series', dict(anchor_reference=anchor['reference'], count=2, interval_weeks=1), self.token, 'dst-series')
        self.assertEqual(reply[1]['error']['code'], 'invalid_local_time')
        self.assertEqual(request('GET', '/_test/export')[1], before)

    def test_import_both_previous_stages_preserves_retry_and_adoption(self):
        destination = test_contract.BASE
        for port in (18081, 18082):
            try:
                test_contract.BASE = 'http://127.0.0.1:' + str(port)
                Contract.setUp(self)
                anchor = self.booking()[1]
                snapshot = request('GET', '/_test/export')[1]
            finally:
                test_contract.BASE = destination
            self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)
            self.assertEqual(self.booking(), (200, anchor))
            current = request('GET', '/reservations/' + anchor['reference'], token=self.token)[1]
            self.assertEqual(current['revision'], 1)
            self.assertEqual(current['accepted_terms']['policy_version'], 0)
            self.assertEqual(len(self.history(anchor['reference'])), 1)
            series = request('POST', '/series', dict(anchor_reference=anchor['reference'], count=2, interval_weeks=1), self.token, 'adopt')
            self.assertEqual(series[0], 201)
            self.assertEqual(series[1]['occurrences'][0]['reservation'], current)


if __name__ == '__main__':
    unittest.main()
