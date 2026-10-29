"""Read-only Platform adapter: account fencing, credential hygiene and limits."""
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

from ai_limit import api_credits, openai_platform as p
from ai_limit._vendor import ccl_leveldb as db, ccl_simplesnappy as snappy


class PlatformBalanceTests(unittest.TestCase):
    def tearDown(self):
        api_credits._CACHE.clear()

    def test_latest_tombstone_does_not_resurrect_a_session(self):
        key = b'authorized-origin-key'
        rows = [types.SimpleNamespace(user_key=key, value=b'old', seq=1, state=db.KeyState.Live),
                types.SimpleNamespace(user_key=key, value=b'', seq=3, state=db.KeyState.Deleted),
                types.SimpleNamespace(user_key=key, value=b'older', seq=2, state=db.KeyState.Live)]
        self.assertEqual(p._latest_values(rows, {key}), {})

    def test_other_origins_are_not_decoded_or_retained(self):
        other = types.SimpleNamespace(user_key=b'other-origin', value=b'x' * (p.MAX_VALUE + 1), seq=1, state=db.KeyState.Live)
        self.assertEqual(p._latest_values([other], {b'allowed'}), {})

    def test_scan_and_decompression_are_bounded(self):
        import io
        row = types.SimpleNamespace(user_key=b'other', value=b'', seq=1, state=db.KeyState.Live)
        with mock.patch.object(p, 'MAX_RECORDS', 1), self.assertRaises(p.PlatformBalanceError):
            p._latest_values([row, row], {b'allowed'})
        size = 9 * 1024 * 1024
        varint = bytearray()
        while size > 127:
            varint.append((size & 127) | 128)
            size >>= 7
        varint.append(size)
        with self.assertRaises(ValueError):
            snappy.decompress(io.BytesIO(varint))

    def test_manifest_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); store = root/'Local Storage'/'leveldb'; store.mkdir(parents=True)
            (store/'CURRENT').write_text('../../secret')
            with mock.patch.object(p.providers, '_chromium_profile_cookie_files', return_value=[('Chosen', root/'Cookies')]):
                with self.assertRaises(p.PlatformBalanceError):p.read_platform_session('Chosen')

    def test_active_org_mismatch_never_exchanges_credentials(self):
        with mock.patch.object(p, 'read_platform_session', return_value=('fake-token', 'other-org')), mock.patch.object(p, '_request') as request:
            with self.assertRaises(p.PlatformBalanceError) as error:p.fetch_credit_grants('Chosen', 'target-org')
        self.assertTrue(error.exception.mismatch);request.assert_not_called()

    def test_server_membership_mismatch_never_reads_credit(self):
        login = {'user': {'orgs': {'data': [{'id': 'other-org'}]}, 'session': {'sensitive_id': 'sess-fake'}}}
        with mock.patch.object(p, 'read_platform_session', return_value=('fake-token', 'target-org')), mock.patch.object(p, '_request', return_value=login) as request:
            with self.assertRaises(p.PlatformBalanceError):p.fetch_credit_grants('Chosen', 'target-org')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], '/onboarding/login')

    def test_session_exchange_and_balance_are_only_allowed_requests(self):
        login = {'user': {'orgs': {'data': [{'id': 'target-org', 'title': 'Example'}]}, 'session': {'sensitive_id': 'sess-fake'}}}
        payload = {'total_available': 30, 'total_paid_available': 10}
        with mock.patch.object(p, 'read_platform_session', return_value=('fake-token', 'target-org')), mock.patch.object(p, '_request', side_effect=[login, payload]) as request:
            org, balance = p.fetch_credit_grants('Chosen', 'target-org')
        self.assertEqual(balance, payload);self.assertNotIn('session', org)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['/onboarding/login', '/billing/credit_grants'])
        self.assertEqual(request.call_args.kwargs['organization_id'], 'target-org')
        with self.assertRaises(p.PlatformBalanceError):p._request('/responses', 'fake', 1)
        with self.assertRaises(p.PlatformBalanceError):p._NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_exact_paid_field_and_promo_difference(self):
        raw = {'total_available': 519.6278318, 'total_paid_available': 19.6983553,
               'grants': {'data': [{'id': 'chatgpt_api_credits_promo_fixture', 'grant_amount': 500, 'used_amount': .0705235, 'effective_at': 100, 'expires_at': 2000000000}]}}
        result = p.normalize_credit_grants(raw)
        self.assertAlmostEqual(result['promotional_balance'], 499.9294765)
        self.assertAlmostEqual(result['purchased_balance'], 19.6983553)
        self.assertEqual(len(result['grants']), 1)

    def test_unclassified_promotions_have_no_invented_expiry(self):
        result = p.normalize_credit_grants({'total_available': 20, 'total_paid_available': 10, 'grants': {'data': []}})
        self.assertEqual(result['promotional_balance'], 10)
        self.assertIsNone(result['grants'])
        self.assertIsNone(p.normalize_credit_grants({'total_available': 20})['purchased_balance'])

    def test_revoking_permission_cannot_reuse_cached_balances(self):
        raw = {'profile': 'Chosen', 'organization_id': 'target-org', 'organization_name': 'Example', 'reuse_platform_session': True}
        payload = {'total_available': 20, 'total_paid_available': 10, 'grants': {'data': []}}
        with mock.patch.object(api_credits, '_profile_config', side_effect=lambda: {'openai': raw}), mock.patch.object(p, 'fetch_credit_grants', return_value=({'title': 'Example'}, payload)) as fetch:
            first = api_credits.live_api_credit_balances()['openai']
            raw['reuse_platform_session'] = False
            second = api_credits.live_api_credit_balances()['openai']
        self.assertEqual(first['purchased_balance'], 10)
        self.assertEqual(second['status'], 'unknown');self.assertIsNone(second['purchased_balance'])
        self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':unittest.main()
