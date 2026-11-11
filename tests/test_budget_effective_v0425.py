"""予算の判定は「実効の予算」で行い、超過 0 件でも黙らない（v0.42.5）.

固定する契約:
  * `_is_over_budget` は**引き上げ後**の `max_tokens` と比べる（64 → 512 になった行を
    「モデルが予算を無視した」と誤判定しない）。
  * `diagnose` は検査した行数と「超過 0 件」を注記に出す（黙って空にしない）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


class EffectiveBudget(unittest.TestCase):
    def test_bumped_budget_is_not_a_violation(self):
        # 64 を要求 → 空応答 → 512 に引き上げて成功（実測の cohere 行と同じ形）
        self.assertFalse(S._is_over_budget({"max_tokens": 512, "tokens": {"completion": 510}}))
        self.assertFalse(S._is_over_budget({"max_tokens": 512, "tokens": {"completion": 486}}))

    def test_real_violation_is_still_detected(self):
        self.assertTrue(S._is_over_budget({"max_tokens": 128, "tokens": {"completion": 400}}))


class DiagnoseNote(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-budget-note-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(self._restore)

    def _restore(self):
        if self._orig is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig

    def test_zero_violations_are_reported_with_the_checked_count(self):
        path = S.trace_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = [{"run": "r1", "tool": "freeagent_panel", "kind": "panel", "ref": "m/a",
                 "served_by": "m/a", "ok": True, "max_tokens": 512,
                 "tokens": {"prompt": 10, "completion": 300}, "ts": S.now_ts()} for _ in range(3)]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        data = S.tool_diagnose({"days": 7})
        self.assertEqual(data["budget"]["models"], [])
        self.assertTrue(any("超過 0 件" in note for note in data["notes"]),
                        data["notes"])
        self.assertIn("実効", " ".join(data["notes"]))


if __name__ == "__main__":
    unittest.main()
