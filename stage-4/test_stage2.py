"""Extra Stage 2 API and browser recovery checks; requires Playwright for tests only."""
import unittest
from test_contract import BASE, Contract, request
from playwright.sync_api import sync_playwright


class StageTwo(Contract):
    def setUp(self):
        super().setUp()
        snapshot = request('GET', '/_test/export')[1]
        snapshot['state']['restaurants']['r']['combinable'] = [['a', 'b']]
        self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)

    def test_combination_conflict_amend_cancel(self):
        body = {'restaurant_id': 'r', 'table_ids': ['a', 'b'], 'party_size': 8, 'starts_at_local': '2030-01-01T19:00'}
        status, res = request('POST', '/reservations', body, self.token, 'pair')
        self.assertEqual(status, 201)
        self.assertNotIn('table_id', res)
        self.assertEqual(self.booking(key='conflict')[0], 409)
        status, res = request('PATCH', '/reservations/' + res['reference'], {'table_id': 'a', 'party_size': 4}, self.token)
        self.assertEqual(status, 200)
        self.assertEqual(res['table_ids'], ['a'])
        self.assertEqual(self.booking('b', 'freed')[0], 201)
        self.assertEqual(request('POST', '/reservations/' + res['reference'] + '/cancel', {}, self.token)[0], 200)
        self.assertEqual(self.booking('a', 'cancelled')[0], 201)

    def test_browser_lost_response_and_mobile(self):
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chromium')
            page = browser.new_page(viewport={'width': 375, 'height': 850})
            page.goto(BASE + '/login')
            page.get_by_test_id('login-email').fill('a@b')
            page.get_by_test_id('login-password').fill('password')
            page.get_by_test_id('login-submit').click()
            page.wait_for_url(BASE + '/')
            page.get_by_test_id('restaurant-select').select_option('r')
            page.get_by_test_id('date-input').fill('2030-01-01')
            page.get_by_test_id('party-size-input').fill('8')
            page.get_by_test_id('search-button').click()
            page.get_by_test_id('slot-a+b-19:00').click()
            requests = []
            def lost(route):
                requests.append((route.request.post_data, route.request.headers['idempotency-key']))
                route.fetch()
                route.abort()
            page.route('**/reservations', lost, times=1)
            page.get_by_test_id('booking-submit').click()
            page.get_by_test_id('booking-uncertain').wait_for()
            self.assertEqual(page.get_by_test_id('booking-error').count(), 0)
            self.assertEqual(page.get_by_test_id('confirmation').count(), 0)
            # Upgrade replacement between requests preserves the live form and receipt.
            snapshot = request('GET', '/_test/export')[1]
            request('POST', '/_test/reset', {})
            self.assertEqual(request('POST', '/_test/import', snapshot)[0], 204)
            def retry(route):
                requests.append((route.request.post_data, route.request.headers['idempotency-key']))
                route.continue_()
            page.route('**/reservations', retry, times=1)
            page.get_by_test_id('booking-submit').click()
            page.get_by_test_id('confirmation').wait_for()
            self.assertEqual(requests[0], requests[1])
            self.assertEqual(page.get_by_test_id('booking-uncertain').count(), 0)
            self.assertEqual(len(request('GET', '/reservations', token=self.token)[1]['reservations']), 1)
            self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
            page.screenshot(path='/tmp/tablekeeper-stage2-mobile.png', full_page=True)
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.screenshot(path='/tmp/tablekeeper-stage2-desktop.png', full_page=True)
            browser.close()

    def test_late_search_cannot_replace_new_selection(self):
        import copy
        snapshot = request('GET', '/_test/export')[1]
        second = copy.deepcopy(snapshot['state']['restaurants']['r'])
        second['id'], second['name'] = 'second', 'Second restaurant'
        snapshot['state']['restaurants']['second'] = second
        request('POST', '/_test/import', snapshot)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chromium')
            page = browser.new_page()
            page.goto(BASE + '/')
            page.get_by_test_id('restaurant-select').select_option('r')
            page.get_by_test_id('date-input').fill('2030-01-01')
            held = []
            def hold(route):
                held.append((route, route.fetch()))
            page.route('**/availability?restaurant_id=r&**', hold, times=1)
            page.get_by_test_id('search-button').click()
            page.wait_for_timeout(100)
            page.get_by_test_id('restaurant-select').select_option('second')
            page.get_by_test_id('search-button').click()
            page.get_by_test_id('availability-grid').wait_for()
            self.assertIn('Second restaurant', page.locator('#results').inner_text())
            self.assertEqual(len(held), 1)
            held[0][0].fulfill(response=held[0][1])
            page.wait_for_timeout(100)
            self.assertIn('Second restaurant', page.locator('#results').inner_text())
            browser.close()


if __name__ == '__main__':
    unittest.main()
