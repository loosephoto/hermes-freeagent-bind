"""Read-only reconciliation must distinguish absent and corrupt pending state."""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'quota_reconcile.py'


class ReconcilePendingCorruption(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='fa-reconcile-pending-')
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / 'quota.json'
        self.env = dict(os.environ, FREEAGENT_STATE_DIR=tmp.name,
                        FREEAGENT_QUOTA_PATH=str(self.path), PYTHONUTF8='1')

    def run_list(self, row):
        original = json.dumps({'providers': {'nous': row}}).encode()
        self.path.write_bytes(original)
        result = subprocess.run([sys.executable, str(SCRIPT), '--json'], env=self.env,
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(original, self.path.read_bytes())
        return result

    def test_falsy_pending_values_are_reported_as_corrupt(self):
        for value in (None, [], '', False, 0):
            with self.subTest(value=value):
                result = self.run_list({'pending': value})
                self.assertNotEqual(0, result.returncode)
                self.assertIn('エラー:', result.stderr)
                self.assertIn('nous', result.stderr)
                self.assertEqual('', result.stdout)

    def test_absent_or_empty_dict_pending_lists_nothing(self):
        for row in ({}, {'pending': {}}):
            with self.subTest(row=row):
                result = self.run_list(row)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({'reservations': []}, json.loads(result.stdout))


if __name__ == '__main__':
    unittest.main()
