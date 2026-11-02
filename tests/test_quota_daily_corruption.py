"""Daily quota counters must not silently turn corrupt state into capacity."""
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from freeagent_bind import server as S


class DailyQuotaCorruption(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='fa-quota-daily-')
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, 'quota.json')
        env = mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp.name,
                                          'FREEAGENT_QUOTA_PATH': self.path})
        env.start()
        self.addCleanup(env.stop)
        for name, value in [('QUOTA_ENABLED', True),
                            ('_QUOTA_LIMITS', {'nous': {'rpd': 20, 'tpd': 100}})]:
            patch = mock.patch.object(S, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        S._QUOTA.clear()
        S._QUOTA_LOADED = False

    def write_row(self, row):
        original = json.dumps({'providers': {'nous': row}}).encode()
        with open(self.path, 'wb') as stream:
            stream.write(original)
        S._QUOTA.clear()
        S._QUOTA_LOADED = False
        return original

    def test_current_day_invalid_counters_fail_closed_without_writing(self):
        today = datetime.datetime.fromtimestamp(S.now_ts(), datetime.timezone.utc).strftime('%Y-%m-%d')
        gate = mock.Mock()
        gate.acquire.return_value = 0
        for field in ('day_req', 'day_tok'):
            for value in (None, -1, True, 1.5, '4', 'missing'):
                with self.subTest(field=field, value=value):
                    row = {'day': today, 'day_req': 2, 'day_tok': 5}
                    if value == 'missing':
                        del row[field]
                    else:
                        row[field] = value
                    original = self.write_row(row)
                    with mock.patch.object(S, 'PROVIDER_GATE', gate), \
                         mock.patch.object(S, '_call_once', return_value={'text': 'upstream', 'tokens': 1}) as upstream:
                        with self.assertRaises(S.QuotaExhaustedError):
                            S._gated_call_once('nous', 'fake', 'x', '', 9, None)
                        upstream.assert_not_called()
                    with open(self.path, 'rb') as stream:
                        self.assertEqual(original, stream.read())
                    with self.assertRaises(S.QuotaExhaustedError):
                        S.quota_add_tokens('nous', 3, 'reservation')
                    with open(self.path, 'rb') as stream:
                        self.assertEqual(original, stream.read())

    def test_new_and_expired_day_initialize_counters(self):
        for row in ({}, {'day': '2000-01-01', 'day_req': -1, 'day_tok': None}):
            with self.subTest(row=row):
                self.write_row(row)
                self.assertIsNone(S.quota_reserve('nous'))
                with open(self.path, encoding='utf-8') as stream:
                    current = json.load(stream)['providers']['nous']
                self.assertEqual(1, current['day_req'])
                self.assertEqual(0, current['day_tok'])


if __name__ == '__main__':
    unittest.main()
