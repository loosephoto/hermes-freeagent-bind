"""Regressions for explicit recovery of uncertain quota reservations."""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'quota_reconcile.py'

class QuotaReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-reconcile-')
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'quota.json'
        self.now = time.time()
        self.original = {'version': 1, 'providers': {'nous': {
            'day': datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).strftime('%Y-%m-%d'),
            'day_req': 2, 'day_tok': 0, 'min_req': 2, 'min_tok': 0,
            'minute_events': [[self.now, 2, 0]],
            'pending': {'orphan': {'tokens': 17, 'at': self.now-600},
                        'other': {'tokens': 9, 'at': self.now}}}}}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')
        self.env = os.environ.copy()
        self.env.update(PYTHONPATH=str(ROOT/'src'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                        FREEAGENT_STATE_DIR=self.tmp.name, FREEAGENT_QUOTA_PATH=str(self.path))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=self.env,
                              text=True, encoding='utf-8', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=15)

    def read(self):
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_listing_is_read_only_and_lists_exact_reservations(self):
        before = self.path.read_bytes()
        p = self.run_cli('--json')
        self.assertEqual(0, p.returncode, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual({'orphan', 'other'}, {r['reservation_id'] for r in data['reservations']})
        self.assertEqual(before, self.path.read_bytes())

    def test_settle_requires_explicit_inactive_confirmation(self):
        before = self.path.read_bytes()
        p = self.run_cli('--settle', 'nous', 'orphan', '--apply')
        self.assertNotEqual(0, p.returncode)
        self.assertEqual(before, self.path.read_bytes())

    def test_preview_never_mutates(self):
        before = self.path.read_bytes()
        p = self.run_cli('--settle', 'nous', 'orphan', '--confirm-inactive', '--json')
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertFalse(json.loads(p.stdout)['applied'])
        self.assertEqual(before, self.path.read_bytes())

    def test_settle_charges_estimate_and_preserves_other_reservations(self):
        p = self.run_cli('--settle', 'nous', 'orphan', '--confirm-inactive', '--apply', '--json')
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertEqual(17, json.loads(p.stdout)['charged_tokens'])
        row = self.read()['providers']['nous']
        self.assertEqual({'other'}, set(row['pending']))
        self.assertEqual(17, row['day_tok'])
        self.assertEqual(17, row['min_tok'])
        self.assertEqual(2, row['day_req'])

    def test_explicit_zero_releases_without_charging(self):
        p = self.run_cli('--settle', 'nous', 'orphan', '--actual-tokens', '0',
                         '--confirm-inactive', '--apply', '--json')
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertEqual(0, json.loads(p.stdout)['charged_tokens'])
        row = self.read()['providers']['nous']
        self.assertEqual(0, row['day_tok'])
        self.assertEqual({'other'}, set(row['pending']))

    def test_concurrent_settlement_charges_only_once(self):
        cmd = [sys.executable, str(SCRIPT), '--settle', 'nous', 'orphan',
               '--confirm-inactive', '--apply', '--json']
        processes = [subprocess.Popen(cmd, cwd=ROOT, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE) for _ in range(2)]
        outcomes = [p.communicate(timeout=15) + (p.returncode,) for p in processes]
        self.assertEqual([0, 1], sorted(item[2] for item in outcomes))
        row = self.read()['providers']['nous']
        self.assertEqual(17, row['day_tok'])
        self.assertEqual(17, row['min_tok'])
        self.assertEqual({'other'}, set(row['pending']))

    def test_unknown_and_negative_values_do_not_modify_state(self):
        before = self.path.read_bytes()
        for args in [('--settle','nous','missing','--confirm-inactive','--apply'),
                     ('--settle','other','orphan','--confirm-inactive','--apply'),
                     ('--settle','nous','orphan','--actual-tokens','-1','--confirm-inactive','--apply')]:
            p = self.run_cli(*args)
            self.assertNotEqual(0, p.returncode)
            self.assertEqual(before, self.path.read_bytes())

if __name__ == '__main__':
    unittest.main()
