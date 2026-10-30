"""Cloud-session credit stays separate from API balance and subscription quota."""
import unittest
from unittest import mock
from ai_limit.claude_limits import claude_cloud_session_credits
from test_widget_layout import menubar


def payload(**kwargs):
    raw={'limit_dollars':250,'remaining_dollars':248.538434,'used_dollars':1.461566,'resets_at':'2099-11-05T07:59:00+00:00','locked_reason':None}
    raw.update(kwargs)
    return {'iguana_necktie':raw}


class CloudCreditTests(unittest.TestCase):
    def test_normalizes_reported_dollars_and_expiry(self):
        x=claude_cloud_session_credits(payload())
        self.assertEqual(x['remaining'],248.538434)
        self.assertEqual(x['limit'],250)
        self.assertEqual(x['expires_at'],'2099-11-05T07:59:00+00:00')

    def test_missing_bucket_does_not_invent_grant_or_use_api_credit(self):
        self.assertIsNone(claude_cloud_session_credits({'extra_usage':{'remaining_dollars':250},'api_credit':{'balance':250}}))
        self.assertIsNone(claude_cloud_session_credits({'iguana_necktie':None}))

    def test_missing_invalid_amounts_stay_unknown(self):
        for value in (None,True,-1,float('nan'),float('inf'),251,10**400):
            self.assertIsNone(claude_cloud_session_credits(payload(remaining_dollars=value))['remaining'])
        self.assertIsNone(claude_cloud_session_credits(payload(resets_at='invalid'))['expires_at'])

    def _app(self,cloud):
        app=menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state={'lang':'en','services':['claude']}
        app._claude={'5h_left':96,'7d_left':63,'cloud_session_credits':cloud}
        return app

    def test_render_keeps_cloud_and_subscription_separate(self):
        rows=self._app(claude_cloud_session_credits(payload()))._widget_detail_rows()
        by_name={r['name']:r for r in rows}
        self.assertEqual(by_name['5h']['value'],'96%')
        self.assertEqual(by_name['Cloud credits']['value'],'$248.54 / $250')
        self.assertEqual(by_name['Cloud credits']['kind'],'credit')
        self.assertIn('Cloud expiry (local)',by_name)

    def test_expired_or_locked_credit_is_not_shown_as_spendable(self):
        rows=self._app(claude_cloud_session_credits(payload(resets_at='2000-01-01T00:00:00Z')))._widget_detail_rows()
        self.assertEqual(next(r['value'] for r in rows if r['name']=='Cloud credits'),'Expired')
        rows=self._app(claude_cloud_session_credits(payload(locked_reason='restricted')))._widget_detail_rows()
        self.assertEqual(next(r['value'] for r in rows if r['name']=='Cloud credits'),'Unavailable')

    def test_locked_api_credit_is_not_a_spendable_balance(self):
        from ai_limit import api_server
        with mock.patch.object(api_server,'live_claude_usage',return_value=payload(locked_reason='restricted')), mock.patch.object(api_server,'live_claude_plan',return_value='Max'):
            result=api_server._claude_payload()
        cloud=result['cloud_session_credits']
        self.assertFalse(cloud['spendable'])
        self.assertIsNone(cloud['remaining'])
        self.assertEqual(cloud['reported_remaining'],248.538434)
        self.assertTrue(result['available'])

    def test_collection_uses_the_existing_usage_request(self):
        with mock.patch.object(menubar,'live_claude_usage',return_value=payload()) as usage, mock.patch.object(menubar,'live_claude_plan',return_value='Max'):
            result=menubar._fetch_claude('en')
        usage.assert_called_once()
        self.assertEqual(result['cloud_session_credits']['remaining'],248.538434)
        self.assertIsNone(result['api_credit'])


if __name__=='__main__':unittest.main()
